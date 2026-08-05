"""Per-cell quarter-Kelly position sizing.

Sizes each trade from culturing stats: ``f* = p * (1 - 1/PF)``, then bets
``kelly_fraction`` of ``f*`` (default 0.25 = quarter-Kelly), capped at
``max_fraction``. When the exact (setup|regime|session) cell is thin, rolls up
to symbol-level stats before falling back to ``fallback_fraction``.
"""

from __future__ import annotations

import math
from typing import Any

from core.strategy_policy import culturing_cell_key
from core.utils import read_json_state


# The normal profiles retain a 5% hard ceiling. A deliberately selected demo
# experiment may opt into the existing full-Kelly engine with
# ``risk.allow_full_kelly: true``; this is never enabled by the base config.
HARD_MAX_RISK_PERCENT = 5.0


def clamp_risk_percent(value: float, config: dict[str, Any] | None = None) -> float:
    """Clamp requested per-trade risk to the configured cap and hard 5% ceiling."""
    try:
        requested = float(value)
    except (TypeError, ValueError):
        requested = 0.0
    if not math.isfinite(requested):
        requested = 0.0
    risk_cfg = (config or {}).get("risk", {}) or {}
    try:
        configured_cap = float(risk_cfg.get("max_risk_per_trade_pct", HARD_MAX_RISK_PERCENT))
    except (TypeError, ValueError):
        configured_cap = HARD_MAX_RISK_PERCENT
    if not math.isfinite(configured_cap) or configured_cap <= 0:
        configured_cap = HARD_MAX_RISK_PERCENT
    allow_full = bool(risk_cfg.get("allow_full_kelly", False))
    cap = configured_cap if allow_full else min(HARD_MAX_RISK_PERCENT, configured_cap)
    return round(max(0.0, min(requested, cap)), 4)


def aggregate_symbol_stats(sym_cells: dict[str, Any]) -> dict[str, Any]:
    """Weighted rollup of all culturing cells for one symbol."""
    n_total = 0
    win_weighted = 0.0
    pf_num = 0.0
    pf_den = 0.0
    exp_num = 0.0

    for stats in sym_cells.values():
        if not isinstance(stats, dict):
            continue
        n = int(stats.get("n", 0) or 0)
        if n <= 0:
            continue
        wr = float(stats.get("win_rate_pct", 0.0) or 0.0)
        n_total += n
        win_weighted += n * wr
        exp = float(stats.get("expectancy_r", 0.0) or 0.0)
        exp_num += n * exp
        pf_raw = stats.get("profit_factor")
        try:
            pf_val = float(pf_raw)
        except (TypeError, ValueError):
            pf_val = 0.0
        if math.isfinite(pf_val) and pf_val > 0:
            pf_num += n * pf_val
            pf_den += n

    if n_total <= 0:
        return {}

    pf = (pf_num / pf_den) if pf_den > 0 else 0.0
    return {
        "n": n_total,
        "win_rate_pct": win_weighted / n_total,
        "profit_factor": pf if pf > 0 else None,
        "expectancy_r": exp_num / n_total,
        "ci95": [0.0, 0.0],
    }


def kelly_fraction(cell_stats: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Kelly sizing for one ledger cell or symbol rollup.

    Returns ``fraction`` (risk-%), ``f_star``, ``f_quarter``, ``gated``, ``reason``.
    """
    min_n = int(cfg.get("min_n", 8))
    frac = float(cfg.get("kelly_fraction", 0.25) or 0.25)
    allow_full = bool(cfg.get("allow_full_kelly_risk", False))
    max_fraction_raw = float(cfg.get("max_fraction", 5.0) or 5.0)
    default_fraction_raw = float(cfg.get("fallback_fraction", 2.5) or 2.5)
    max_fraction = max_fraction_raw if allow_full else min(max_fraction_raw, HARD_MAX_RISK_PERCENT)
    default_frac = default_fraction_raw if allow_full else min(default_fraction_raw, HARD_MAX_RISK_PERCENT)
    criteria_mode = bool(cfg.get("criteria_mode", False))

    n = int(cell_stats.get("n", 0) or 0)
    p = float(cell_stats.get("win_rate_pct", 0.0) or 0.0) / 100.0
    p = max(0.0, min(1.0, p))
    pf = cell_stats.get("profit_factor")
    try:
        pf = float(pf)
    except (TypeError, ValueError):
        pf = 0.0

    ci95 = cell_stats.get("ci95") or [0.0, 0.0]
    try:
        ci95_lo = float(ci95[0])
    except (TypeError, ValueError, IndexError):
        ci95_lo = 0.0

    if math.isfinite(pf) and pf > 1.0:
        f_star = p * (1.0 - 1.0 / pf)
    elif pf == math.inf:
        f_star = p
    else:
        f_star = 0.0

    f_quarter = f_star * frac
    fraction = min(f_quarter * 100.0, max_fraction)

    if n < min_n:
        return _gated(default_frac, f_star, f_quarter, pf, p, n, f"thin (n={n}<{min_n})")
    if not (math.isfinite(pf) and pf > 1.0) and pf != math.inf:
        return _gated(default_frac, f_star, f_quarter, pf, p, n, f"no edge (PF<={pf:.2f})")
    if f_star <= 0.0:
        return _gated(default_frac, f_star, f_quarter, pf, p, n, "non-positive Kelly")
    if not criteria_mode and ci95_lo <= 0.0:
        return _gated(
            default_frac, f_star, f_quarter, pf, p, n,
            f"not significant (ci95_lo={ci95_lo:.3f})",
        )
    if fraction <= 0.0:
        return _gated(default_frac, f_star, f_quarter, pf, p, n, "fraction clamped to 0")

    if frac >= 0.99:
        reason = "full_kelly" if criteria_mode else "kelly"
    elif frac <= 0.26:
        reason = "quarter_kelly" if criteria_mode else "kelly"
    else:
        reason = f"kelly_{int(round(frac * 100))}pct" if criteria_mode else "kelly"
    return {
        "fraction": round(fraction, 4),
        "f_star": round(f_star, 4),
        "f_quarter": round(f_quarter, 4),
        "pf": (math.inf if pf == math.inf else round(pf, 4)),
        "p": round(p, 4),
        "n": n,
        "gated": False,
        "reason": reason,
        "kelly_fraction": frac,
    }


def _gated(default_frac, f_star, f_quarter, pf, p, n, reason):
    return {
        "fraction": round(default_frac, 4),
        "f_star": round(f_star, 4),
        "f_quarter": round(f_quarter, 4),
        "pf": (math.inf if pf == math.inf else round(pf, 4)),
        "p": round(p, 4),
        "n": n,
        "gated": True,
        "reason": reason,
    }


def kelly_for_signal(
    signal: dict[str, Any],
    config: dict[str, Any],
    default_risk_pct: float,
) -> dict[str, Any]:
    """Resolve quarter-Kelly risk-% for a candidate signal."""
    sig_cfg = config.get("signals", {}) or {}
    cfg = dict(sig_cfg.get("kelly_sizing", {}) or {})
    symbol = signal.get("symbol")

    if not cfg.get("enabled", False):
        return {
            "fraction": clamp_risk_percent(default_risk_pct, config),
            "gated": True,
            "reason": "disabled",
            "cell": None,
            "symbol": symbol,
        }

    fallback = clamp_risk_percent(
        float(cfg.get("fallback_fraction", default_risk_pct) or default_risk_pct),
        config,
    )

    mc = signal.get("market_context") or {}
    if not isinstance(mc, dict):
        mc = {}
    reg = mc.get("market_regime") or {}
    if not isinstance(reg, dict):
        reg = {}
    cell = culturing_cell_key(
        signal.get("setup_type"),
        reg.get("primary"),
        reg.get("bias"),
        signal.get("side"),
        mc.get("session"),
    )

    ledger = read_json_state("forward_test_ledger.json", default={}) or {}
    cells = ledger.get("cells", {}) if isinstance(ledger, dict) else {}
    sym_cells = cells.get(symbol, {}) if isinstance(cells, dict) else {}
    cell_stats = sym_cells.get(cell) if isinstance(sym_cells, dict) else None

    if cell_stats:
        res = kelly_fraction(cell_stats, cfg)
        if not res["gated"]:
            res["cell"] = cell
            res["symbol"] = symbol
            return res

    if cfg.get("symbol_aggregate_fallback", True) and sym_cells:
        agg = aggregate_symbol_stats(sym_cells)
        if agg:
            agg_res = kelly_fraction(agg, cfg)
            if not agg_res["gated"]:
                agg_res["cell"] = cell
                agg_res["symbol"] = symbol
                agg_res["reason"] = "symbol_quarter_kelly"
                agg_res["aggregate_n"] = agg["n"]
                return agg_res

    if cell_stats:
        res = kelly_fraction(cell_stats, cfg)
    else:
        res = kelly_fraction({}, cfg)
    res["fraction"] = clamp_risk_percent(fallback, config)
    if not cell_stats:
        res["reason"] = "no cell data"
    res["cell"] = cell
    res["symbol"] = symbol
    return res


def resolve_risk_percent(
    signal: dict[str, Any],
    config: dict[str, Any],
    default_risk_pct: float | None = None,
) -> tuple[float, dict[str, Any]]:
    """Return (risk_percent, kelly_meta) for sizing / exposure checks."""
    default = default_risk_pct
    if default is None:
        default = float(config.get("signals", {}).get("default_risk_percent", 1))
    kelly = kelly_for_signal(signal, config, float(default))
    fraction = float(kelly.get("fraction", default))
    # Conviction sizing: a professional risks more on A-grade confluence and
    # less on marginal setups. The multiplier is <= 1.0 by default, so this can
    # only trim risk below the Kelly/base level unless the operator opts into
    # upsizing the top tier (conviction.size_by_grade). Downstream per-trade and
    # exposure caps still clamp the final size.
    conv_mult = signal.get("conviction_size_mult")
    if conv_mult is not None:
        try:
            fraction *= float(conv_mult)
            kelly = {**kelly, "conviction_size_mult": float(conv_mult)}
        except (TypeError, ValueError):
            pass
    fraction = clamp_risk_percent(fraction, config)
    risk_cfg = config.get("risk") or {}
    configured_cap = float(risk_cfg.get("max_risk_per_trade_pct", HARD_MAX_RISK_PERCENT) or HARD_MAX_RISK_PERCENT)
    risk_cap = configured_cap if bool(risk_cfg.get("allow_full_kelly", False)) else min(
        HARD_MAX_RISK_PERCENT, configured_cap,
    )
    kelly = {**kelly, "fraction": fraction, "risk_cap_percent": risk_cap}
    return fraction, kelly