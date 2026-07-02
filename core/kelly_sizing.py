"""Per-cell Kelly-criterion position sizing.

Why this exists: the bot sizes every trade off a fixed ``signals.default_risk_percent``
regardless of whether the (symbol | setup | regime | session) cell has a proven
edge. The user asked to size instead by *expected outcome* — i.e. the Kelly
fraction of each cell's own clean forward-test stats. Kelly is an edge detector:
``f* = p * (1 - 1/PF)`` where ``p`` is the win rate and ``PF`` the profit factor.
``f* <= 0`` means *no edge* (the bot's own global result is ``f* = -0.257`` — an
86% win rate is worthless when PF < 1). So Kelly is gated: only cells with
enough clean trades (``min_n``), a real edge (``PF > 1``) and a statistically
positive expectancy (bootstrap ``ci95`` lower bound > 0) get a Kelly fraction;
everything else falls back to the default risk percent so culturing keeps
collecting data. We trade a *fraction* (default quarter-Kelly) of the full
fraction and cap it at ``max_fraction`` so a single hot cell can't blow the
account.

Honest framing (persists): no cell in the current ledger passes the gate, so
today this changes nothing — it is a correctness/safety upgrade that recognizes
a proven edge the *moment* a cell clears the significance bar, sizing it up
(up to ``max_fraction``) rather than treating a winner identically to a loser.
"""

from __future__ import annotations

import math
from typing import Any

from core.strategy_policy import culturing_cell_key
from core.utils import read_json_state


def kelly_fraction(cell_stats: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Kelly sizing for one ledger cell.

    ``cell_stats`` is a per-cell dict from ``state/forward_test_ledger.json``
    (``n``, ``win_rate_pct``, ``profit_factor``, ``ci95``).
    ``cfg`` is the ``signals.kelly_sizing`` config block.

    Returns a dict with ``fraction`` (the risk-% to bet), ``f_star`` (full
    Kelly), ``f_quarter`` (fractional Kelly), ``pf``, ``p``, ``gated`` (True
    when we fell back to the default — i.e. no proven edge) and ``reason``.
    """
    min_n = int(cfg.get("min_n", 8))
    frac = float(cfg.get("kelly_fraction", 0.25) or 0.25)
    max_fraction = float(cfg.get("max_fraction", 5.0) or 5.0)
    default_frac = float(cfg.get("fallback_fraction", 2.5) or 2.5)

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

    # Full Kelly: f* = p * (1 - 1/PF). PF <= 1 -> f* <= 0 (no edge).
    # An infinite PF (no losing trades yet) gives 1/PF = 0 -> f* = p; the
    # significance + min_n gates stop us sizing off a thin all-win cell, and
    # the cap stops an absurd fraction even when it passes.
    if math.isfinite(pf) and pf > 1.0:
        f_star = p * (1.0 - 1.0 / pf)
    elif pf == math.inf:
        f_star = p
    else:
        f_star = 0.0

    f_quarter = f_star * frac
    fraction = min(f_quarter * 100.0, max_fraction)

    # --- Gates: only size up a *proven* edge ------------------------------- #
    if n < min_n:
        return _gated(default_frac, f_star, f_quarter, pf, p, n,
                      f"thin (n={n}<{min_n})")
    if not (math.isfinite(pf) and pf > 1.0) and pf != math.inf:
        return _gated(default_frac, f_star, f_quarter, pf, p, n,
                      f"no edge (PF<={pf:.2f})")
    if f_star <= 0.0:
        return _gated(default_frac, f_star, f_quarter, pf, p, n,
                      "non-positive Kelly")
    if ci95_lo <= 0.0:
        return _gated(default_frac, f_star, f_quarter, pf, p, n,
                      f"not significant (ci95_lo={ci95_lo:.3f})")
    if fraction <= 0.0:
        return _gated(default_frac, f_star, f_quarter, pf, p, n,
                      "fraction clamped to 0")

    return {
        "fraction": round(fraction, 4),
        "f_star": round(f_star, 4),
        "f_quarter": round(f_quarter, 4),
        "pf": (math.inf if pf == math.inf else round(pf, 4)),
        "p": round(p, 4),
        "n": n,
        "gated": False,
        "reason": "kelly",
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
    """Resolve the Kelly risk-% for a candidate signal.

    Builds the *same* culturing cell key the verifier/ledger use
    (``setup | regime | align | session``), looks the cell up in
    ``state/forward_test_ledger.json``, and returns the Kelly fraction when the
    cell passes the gates — otherwise falls back to ``default_risk_pct`` so
    trading continues while culturing collects data.

    Returns ``{fraction, f_star, f_quarter, pf, p, n, gated, reason, cell,
    symbol}`` — ``fraction`` is the risk-% the broker should use.
    """
    sig_cfg = config.get("signals", {}) or {}
    cfg = sig_cfg.get("kelly_sizing", {}) or {}
    if not cfg.get("enabled", False):
        return {
            "fraction": round(float(default_risk_pct), 4),
            "gated": True,
            "reason": "disabled",
            "cell": None,
            "symbol": signal.get("symbol"),
        }

    fallback = float(cfg.get("fallback_fraction", default_risk_pct) or default_risk_pct)

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
    symbol = signal.get("symbol")

    ledger = read_json_state("forward_test_ledger.json", default={}) or {}
    cells = ledger.get("cells", {}) if isinstance(ledger, dict) else {}
    sym_cells = cells.get(symbol, {}) if isinstance(cells, dict) else {}
    cell_stats = sym_cells.get(cell) if isinstance(sym_cells, dict) else None

    if not cell_stats:
        res = kelly_fraction({}, cfg)  # n=0 -> thin -> fallback
        res["fraction"] = round(fallback, 4)
        res["reason"] = "no cell data"
        res["cell"] = cell
        res["symbol"] = symbol
        return res

    res = kelly_fraction(cell_stats, cfg)
    if res["gated"]:
        res["fraction"] = round(fallback, 4)
    res["cell"] = cell
    res["symbol"] = symbol
    return res