"""Setup trigger catalog — all 8 arena strategies with tunable conditions.

Single source of truth for:
  * SetupClassifier threshold params (read via ``trigger_params``)
  * Arena trigger logging (``snapshot_trigger_context``)
  * TUI / evaluator / optimizer (``export_catalog``, ``SETUP_ORDER``)
"""

from __future__ import annotations

from typing import Any

from core.setup_library import SETUP_LIBRARY
from core.utils import utc_now_iso, write_json_state

SETUP_ORDER = (
    "trend_continuation",
    "pullback",
    "breakout",
    "compression_breakout",
    "range_fade",
    "liquidity_sweep",
    "mean_reversion",
    "false_breakout",
)

# Default trigger params — overridden by config intelligence.setup_triggers.
_TRIGGER_DEFAULTS: dict[str, dict[str, Any]] = {
    "trend_continuation": {
        "regime": "trending",
        "move_type": "continuation",
    },
    "pullback": {
        "move_types": ("pullback",),
        "retest_breakouts": ("breakout_retest", "breakdown_retest"),
        "allow_compression_setup": True,
    },
    "breakout": {
        "breakout_states": ("breakout", "breakdown"),
    },
    "compression_breakout": {
        "phase": "compression",
        "breakout_states": ("breakout", "breakdown"),
    },
    "range_fade": {
        "regime": "ranging",
        "boundary_pct": 0.0015,
    },
    "liquidity_sweep": {
        "min_liquidity": 0.65,
        "rejections": ("bullish_rejection", "bearish_rejection"),
    },
    "mean_reversion": {
        "bb_upper": 0.92,
        "bb_lower": 0.08,
    },
    "false_breakout": {
        "bb_upper": 0.85,
        "bb_lower": 0.15,
    },
}

# Human-readable trigger rules (for TUI + eval reports).
_TRIGGER_RULES: dict[str, list[str]] = {
    "trend_continuation": [
        "regime=trending AND move_type=continuation",
        "M5 trend bullish → BUY / bearish → SELL",
        "confidence = avg(trend, momentum) evidence",
    ],
    "pullback": [
        "move_type=pullback OR breakout retest OR compression_setup in compression phase",
        "M5 trend defines side (bullish=BUY, bearish=SELL)",
        "confidence = avg(trend, structure) evidence",
    ],
    "breakout": [
        "breakout state = breakout|breakdown",
        "confidence = avg(structure, volume, momentum)",
    ],
    "compression_breakout": [
        "phase=compression AND breakout|breakdown",
        "confidence = avg(volatility, structure, volume)",
    ],
    "range_fade": [
        "regime=ranging",
        "price within boundary_pct of support → BUY / resistance → SELL",
        "confidence = liquidity evidence",
    ],
    "liquidity_sweep": [
        "rejection = bullish|bearish AND liquidity >= min_liquidity",
        "confidence = avg(liquidity, volume)",
    ],
    "mean_reversion": [
        "bb_position >= bb_upper → SELL / <= bb_lower → BUY",
        "confidence = avg(structure, liquidity)",
    ],
    "false_breakout": [
        "bearish_rejection + bb_position > bb_upper → SELL",
        "bullish_rejection + bb_position < bb_lower → BUY",
        "confidence = structure evidence",
    ],
}


def _intel_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("intelligence", {}) if isinstance(config.get("intelligence"), dict) else {}


def trigger_params(config: dict[str, Any], setup_type: str) -> dict[str, Any]:
    """Resolved tunable params for a setup (defaults + config overrides)."""
    base = dict(_TRIGGER_DEFAULTS.get(setup_type, {}))
    block = _intel_cfg(config).get("setup_triggers", {})
    if isinstance(block, dict):
        overrides = block.get(setup_type)
        if isinstance(overrides, dict):
            base.update(overrides)
        global_ov = block.get("global")
        if isinstance(global_ov, dict):
            for k, v in global_ov.items():
                base.setdefault(k, v)
    return base


def trigger_rules(setup_type: str) -> list[str]:
    return list(_TRIGGER_RULES.get(setup_type, []))


def describe_trigger(setup_type: str, config: dict[str, Any] | None = None) -> str:
    """One-line trigger summary for TUI."""
    rules = _TRIGGER_RULES.get(setup_type, [])
    if not rules:
        return setup_type
    line = rules[0]
    if config:
        params = trigger_params(config, setup_type)
        if setup_type == "mean_reversion" and params:
            line = f"bb>={params.get('bb_upper', 0.92)} SELL / bb<={params.get('bb_lower', 0.08)} BUY"
        elif setup_type == "range_fade":
            line = f"ranging + within {params.get('boundary_pct', 0.0015)*100:.2f}% of S/R"
        elif setup_type == "liquidity_sweep":
            line = f"rejection + liquidity>={params.get('min_liquidity', 0.65)}"
    return line


def snapshot_trigger_context(
    setup: dict[str, Any],
    feat: dict[str, Any],
    ctx: dict[str, Any],
    ev: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Feature/context slice stamped on arena trigger rows for eval."""
    params = trigger_params(config or {}, setup.get("setup_type", ""))
    return {
        "setup_confidence": setup.get("setup_confidence"),
        "reason": setup.get("reason"),
        "params": params,
        "feat": {
            "m5_trend": feat.get("m5_trend"),
            "breakout": feat.get("breakout"),
            "bb_position": feat.get("bb_position"),
            "rejection": feat.get("rejection"),
            "price": feat.get("price"),
            "support": feat.get("support"),
            "resistance": feat.get("resistance"),
        },
        "ctx": {
            "regime": ctx.get("regime"),
            "phase": ctx.get("phase"),
            "move_type": ctx.get("move_type"),
            "session": ctx.get("session"),
            "market_regime": (ctx.get("market_regime") or {}).get("primary"),
        },
        "evidence": {k: round(float(v), 3) if isinstance(v, (int, float)) else v for k, v in (ev or {}).items()},
    }


def export_catalog(config: dict[str, Any]) -> dict[str, Any]:
    """Full strategy + trigger catalog for evaluator / optimizer."""
    setups: list[dict[str, Any]] = []
    for name in SETUP_ORDER:
        defn = SETUP_LIBRARY.get(name)
        params = trigger_params(config, name)
        setups.append({
            "setup_type": name,
            "display_name": defn.display_name if defn else name,
            "description": defn.description if defn else "",
            "allowed_regimes": list(defn.allowed_regimes) if defn else [],
            "blocked_regimes": list(defn.blocked_regimes) if defn else [],
            "min_confidence": defn.min_confidence if defn else 0.5,
            "min_rr": defn.min_rr if defn else 1.2,
            "entry_hints": list(defn.entry_hints) if defn else [],
            "exit_hints": list(defn.exit_hints) if defn else [],
            "trigger_rules": trigger_rules(name),
            "trigger_summary": describe_trigger(name, config),
            "trigger_params": params,
        })
    arena = config.get("strategy_arena", {}) if isinstance(config.get("strategy_arena"), dict) else {}
    return {
        "timestamp": utc_now_iso(),
        "setup_count": len(setups),
        "setups": setups,
        "arena_symbols": list(arena.get("symbols") or config.get("mt5", {}).get("symbols") or []),
        "emit_all_setups": bool(arena.get("emit_all_setups", True)),
    }


def write_setup_catalog(config: dict[str, Any]) -> dict[str, Any]:
    catalog = export_catalog(config)
    write_json_state("setup_catalog.json", catalog)
    return catalog