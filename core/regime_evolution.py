"""Regime-aware policy progression — adapt gates/setup bias to market state.

Pure mapping + evolution from closed trades (no MT5). Durable state:
``state/regime_evolution.json``. Consumed by verifier / evaluation / decision
so different regimes get different effective settings and promote/demote
setups as outcomes accumulate (min_n floors).
"""

from __future__ import annotations

from typing import Any

from core.utils import read_json_state, utc_now_iso, write_json_state

STATE_FILE = "regime_evolution.json"

# Labels from core.market_regime.REGIME_LABELS + common aliases on trades.
KNOWN_REGIMES = frozenset({
    "strong_trend",
    "weak_trend",
    "range",
    "compression",
    "expansion",
    "volatility_spike",
    "accumulation",
    "distribution",
    "transitional",
    "unknown",
})

_REGIME_ALIASES = {
    "trending": "weak_trend",
    "trend": "weak_trend",
    "strongtrend": "strong_trend",
    "weaktrend": "weak_trend",
    "ranging": "range",
    "range": "range",
    "sideways": "range",
    "mean_reverting": "range",
    "compress": "compression",
    "expand": "expansion",
    "volatile": "volatility_spike",
    "vol_spike": "volatility_spike",
    "transition": "transitional",
}


def normalize_regime(label: str | None) -> str:
    """Canonical regime key for policy lookup."""
    s = str(label or "unknown").strip().lower().replace(" ", "_").replace("-", "_")
    if s in _REGIME_ALIASES:
        return _REGIME_ALIASES[s]
    if s in KNOWN_REGIMES:
        return s
    # soft match
    if "trend" in s and "strong" in s:
        return "strong_trend"
    if "trend" in s:
        return "weak_trend"
    if "range" in s or "rang" in s:
        return "range"
    if "compress" in s:
        return "compression"
    if "expand" in s:
        return "expansion"
    if "volat" in s or "spike" in s:
        return "volatility_spike"
    if "trans" in s:
        return "transitional"
    return "unknown"


def base_regime_policy(regime: str | None) -> dict[str, Any]:
    """Static regime → setup bias + gate deltas (distinct per regime family)."""
    r = normalize_regime(regime)
    if r == "strong_trend":
        return {
            "regime": r,
            "preferred_setups": ["trend_continuation", "pullback"],
            "demoted_setups": ["mean_reversion", "range_fade"],
            "min_confidence_delta": -5,
            "min_rr_delta": 0.0,
            "skip": False,
        }
    if r == "weak_trend":
        return {
            "regime": r,
            "preferred_setups": ["pullback", "trend_continuation"],
            "demoted_setups": ["range_fade"],
            "min_confidence_delta": 0,
            "min_rr_delta": 0.05,
            "skip": False,
        }
    if r in ("range", "accumulation", "distribution"):
        return {
            "regime": r,
            "preferred_setups": ["pullback", "mean_reversion", "range_fade"],
            "demoted_setups": ["trend_continuation", "breakout"],
            "min_confidence_delta": 5,
            "min_rr_delta": 0.10,
            "skip": False,
        }
    if r == "compression":
        return {
            "regime": r,
            "preferred_setups": ["breakout", "pullback"],
            "demoted_setups": ["mean_reversion", "trend_continuation"],
            "min_confidence_delta": 8,
            "min_rr_delta": 0.15,
            "skip": False,
        }
    if r == "expansion":
        return {
            "regime": r,
            "preferred_setups": ["trend_continuation", "pullback"],
            "demoted_setups": ["range_fade"],
            "min_confidence_delta": 0,
            "min_rr_delta": 0.05,
            "skip": False,
        }
    if r == "volatility_spike":
        return {
            "regime": r,
            "preferred_setups": [],
            "demoted_setups": ["breakout", "trend_continuation", "pullback"],
            "min_confidence_delta": 20,
            "min_rr_delta": 0.30,
            "skip": True,
        }
    if r == "transitional":
        return {
            "regime": r,
            "preferred_setups": ["pullback"],
            "demoted_setups": ["breakout", "trend_continuation"],
            "min_confidence_delta": 12,
            "min_rr_delta": 0.20,
            "skip": False,
        }
    return {
        "regime": r,
        "preferred_setups": ["pullback"],
        "demoted_setups": [],
        "min_confidence_delta": 0,
        "min_rr_delta": 0.0,
        "skip": False,
    }


def regime_cell_key(symbol: str, regime: str | None, setup: str | None = None) -> str:
    sym = str(symbol or "?")
    reg = normalize_regime(regime)
    if setup:
        return f"{sym}|{reg}|{setup}"
    return f"{sym}|{reg}"


def _trade_regime(trade: dict[str, Any]) -> str:
    if trade.get("regime_primary"):
        return normalize_regime(str(trade.get("regime_primary")))
    mc = trade.get("market_context") if isinstance(trade.get("market_context"), dict) else {}
    mr = mc.get("market_regime") if isinstance(mc.get("market_regime"), dict) else {}
    if mr.get("primary"):
        return normalize_regime(str(mr.get("primary")))
    if trade.get("regime"):
        return normalize_regime(str(trade.get("regime")))
    meta = trade.get("signal_meta") if isinstance(trade.get("signal_meta"), dict) else {}
    if meta.get("regime_primary"):
        return normalize_regime(str(meta.get("regime_primary")))
    return "unknown"


def _trade_setup(trade: dict[str, Any]) -> str:
    return str(trade.get("setup") or trade.get("setup_type") or "unknown")


def _trade_r(trade: dict[str, Any]) -> float | None:
    if trade.get("r_multiple") is not None:
        try:
            return float(trade["r_multiple"])
        except (TypeError, ValueError):
            pass
    # crude fallback from win/loss label
    res = str(trade.get("result") or "").lower()
    if res == "win":
        return 0.5
    if res == "loss":
        return -1.0
    try:
        pnl = float(trade.get("pnl") or 0)
    except (TypeError, ValueError):
        return None
    if pnl > 0:
        return 0.3
    if pnl < 0:
        return -0.8
    return 0.0


def evolve_from_trades(
    trades: list[dict[str, Any]],
    *,
    min_n: int = 6,
    promote_exp: float = 0.05,
    demote_exp: float = 0.0,
) -> dict[str, Any]:
    """Build durable evolution state from closed trades.

    Cells: ``symbol|regime`` (aggregate) and ``symbol|regime|setup`` (setup fit).
    Actions: promote / demote / hold when n >= min_n.
    """
    buckets: dict[str, list[float]] = {}
    for t in trades or []:
        if t.get("archive_polluted"):
            continue
        sym = str(t.get("symbol") or "")
        if not sym:
            continue
        reg = _trade_regime(t)
        setup = _trade_setup(t)
        r = _trade_r(t)
        if r is None:
            continue
        for key in (
            regime_cell_key(sym, reg),
            regime_cell_key(sym, reg, setup),
        ):
            buckets.setdefault(key, []).append(r)

    cells: dict[str, Any] = {}
    for key, rs in buckets.items():
        n = len(rs)
        mean_r = sum(rs) / n if n else 0.0
        wins = sum(1 for x in rs if x > 0)
        wr = 100.0 * wins / n if n else 0.0
        parts = key.split("|")
        symbol = parts[0] if parts else "?"
        regime = parts[1] if len(parts) > 1 else "unknown"
        setup = parts[2] if len(parts) > 2 else None

        action = "hold"
        conf_delta = 0
        rr_delta = 0.0
        if n >= min_n:
            if mean_r > promote_exp:
                action = "promote"
                conf_delta = -4
                rr_delta = -0.05
            elif mean_r <= demote_exp:
                action = "demote"
                conf_delta = 10
                rr_delta = 0.15

        cells[key] = {
            "symbol": symbol,
            "regime": regime,
            "setup": setup,
            "n": n,
            "expectancy_r": round(mean_r, 4),
            "win_rate_pct": round(wr, 2),
            "action": action,
            "min_confidence_delta": conf_delta,
            "min_rr_delta": rr_delta,
            "eligible": n >= min_n,
        }

    # Per-regime preferred setups from promote actions
    regime_setup_rank: dict[str, dict[str, float]] = {}
    for key, cell in cells.items():
        if not cell.get("setup") or not cell.get("eligible"):
            continue
        rk = f"{cell['symbol']}|{cell['regime']}"
        regime_setup_rank.setdefault(rk, {})
        # score by expectancy
        regime_setup_rank[rk][str(cell["setup"])] = float(cell.get("expectancy_r") or 0)

    preferred: dict[str, list[str]] = {}
    demoted: dict[str, list[str]] = {}
    for rk, setups in regime_setup_rank.items():
        ordered = sorted(setups.items(), key=lambda kv: -kv[1])
        preferred[rk] = [s for s, e in ordered if e > promote_exp]
        demoted[rk] = [s for s, e in ordered if e <= demote_exp]

    return {
        "schema": 1,
        "updated_at": utc_now_iso(),
        "min_n": min_n,
        "promote_exp": promote_exp,
        "demote_exp": demote_exp,
        "cells": cells,
        "preferred_setups_by_regime": preferred,
        "demoted_setups_by_regime": demoted,
        "n_cells": len(cells),
        "n_promoted": sum(1 for c in cells.values() if c.get("action") == "promote"),
        "n_demoted": sum(1 for c in cells.values() if c.get("action") == "demote"),
    }


def regime_evolution_enabled(config: dict[str, Any] | None) -> bool:
    """Opt-in via adaptation.regime_evolution.enabled (default True when block present or absent)."""
    cfg = config or {}
    acfg = (cfg.get("adaptation") or {}).get("regime_evolution")
    if acfg is None:
        return True
    if isinstance(acfg, dict):
        return bool(acfg.get("enabled", True))
    return bool(acfg)


def load_regime_evolution() -> dict[str, Any]:
    doc = read_json_state(STATE_FILE, default={}) or {}
    return doc if isinstance(doc, dict) else {}


def refresh_regime_evolution(
    config: dict[str, Any],
    trades: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evolve from trade_log (or provided trades) and write durable state."""
    acfg = (config.get("adaptation") or {}).get("regime_evolution") or {}
    if isinstance(acfg, dict) and not bool(acfg.get("enabled", True)):
        return load_regime_evolution() or {
            "schema": 1,
            "enabled": False,
            "cells": {},
            "n_cells": 0,
            "n_promoted": 0,
            "n_demoted": 0,
        }
    min_n = int(acfg.get("min_n") or (config.get("culturing") or {}).get("min_n") or 6)
    promote_exp = float(acfg.get("promote_exp", 0.05))
    demote_exp = float(acfg.get("demote_exp", 0.0))

    if trades is None:
        tl = read_json_state("trade_log.json", default={}) or {}
        trades = list(tl.get("trades") or []) if isinstance(tl, dict) else []

    state = evolve_from_trades(
        trades,
        min_n=min_n,
        promote_exp=promote_exp,
        demote_exp=demote_exp,
    )
    state["enabled"] = True
    write_json_state(STATE_FILE, state)
    return state


def effective_regime_settings(
    config: dict[str, Any],
    *,
    symbol: str,
    regime: str | None,
    setup: str | None = None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge base map + config.regime_overrides + evolved cell into effective gates.

    Returns distinct settings for different regimes (acceptance criterion 1/3).
    """
    r = normalize_regime(regime)
    base = base_regime_policy(r)
    signals = config.get("signals") or {}
    base_conf = float(signals.get("min_confidence") or 50)
    base_rr = float(signals.get("min_risk_reward") or 1.0)

    # Static YAML overrides (existing knobs)
    ov = (signals.get("regime_overrides") or {}).get(r) or {}
    if isinstance(ov, dict) and ov:
        if "min_confidence" in ov:
            base_conf = float(ov["min_confidence"])
        if "min_risk_reward" in ov:
            base_rr = float(ov["min_risk_reward"])
        if ov.get("skip"):
            base["skip"] = True

    conf = base_conf + float(base.get("min_confidence_delta") or 0)
    rr = base_rr + float(base.get("min_rr_delta") or 0)
    preferred = list(base.get("preferred_setups") or [])
    demoted = list(base.get("demoted_setups") or [])
    skip = bool(base.get("skip"))
    source = "base_map"

    st = state if state is not None else load_regime_evolution()
    cells = (st or {}).get("cells") or {}
    rk = regime_cell_key(symbol, r)
    sk = regime_cell_key(symbol, r, setup) if setup else None

    # Evolved preferred/demoted lists for symbol|regime
    pref_map = (st or {}).get("preferred_setups_by_regime") or {}
    dem_map = (st or {}).get("demoted_setups_by_regime") or {}
    if rk in pref_map and pref_map[rk]:
        preferred = list(pref_map[rk])
        source = "evolved"
    if rk in dem_map and dem_map[rk]:
        demoted = list(dem_map[rk])
        source = "evolved"

    # Cell-level gate deltas — apply the most-specific eligible cell only.
    # Prefer symbol|regime|setup; fall back to symbol|regime aggregate.
    # (Applying both doubled promote/demote conf/RR adjustments.)
    cell = None
    if sk:
        c_sk = cells.get(sk)
        if isinstance(c_sk, dict) and c_sk.get("eligible"):
            cell = c_sk
    if cell is None:
        c_rk = cells.get(rk)
        if isinstance(c_rk, dict) and c_rk.get("eligible"):
            cell = c_rk
    if cell is not None:
        conf += float(cell.get("min_confidence_delta") or 0)
        rr += float(cell.get("min_rr_delta") or 0)
        source = "evolved"
        if cell.get("action") == "demote" and setup and cell.get("setup") == setup:
            if setup not in demoted:
                demoted.append(setup)

    setup_s = str(setup or "")
    setup_preferred = bool(setup_s and setup_s in preferred)
    setup_demoted = bool(setup_s and setup_s in demoted)
    if setup_demoted:
        conf += 8
        rr += 0.05
    if setup_preferred:
        conf -= 3

    conf = max(1.0, min(99.0, conf))
    rr = max(0.1, min(5.0, rr))

    return {
        "regime": r,
        "symbol": symbol,
        "setup": setup_s or None,
        "min_confidence": conf,
        "min_risk_reward": rr,
        "preferred_setups": preferred,
        "demoted_setups": demoted,
        "setup_preferred": setup_preferred,
        "setup_demoted": setup_demoted,
        "skip": skip,
        "source": source,
        "base_confidence_delta": base.get("min_confidence_delta"),
        "base_rr_delta": base.get("min_rr_delta"),
    }
