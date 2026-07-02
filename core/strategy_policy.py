"""Symbol-specific strategy policy and setup normalization helpers."""

from __future__ import annotations

from typing import Any

from core.setup_library import SETUP_LIBRARY

KNOWN_SETUPS = set(SETUP_LIBRARY.keys())
MOVE_TYPE_SETUP_MAP = {
    "pullback": "pullback",
    "continuation": "trend_continuation",
    "compression": "compression_breakout",
    "range": "range_fade",
}

# USER-AUTHORIZED 2026-07-01: per-symbol policy is now PURE DATA-DRIVEN.
# DEFAULT_SYMBOL_RULES is intentionally EMPTY -> ``symbol_rule`` returns {} for
# every symbol, so ``setup_allowed`` is always True (permissive), no manual
# preferred_setups/sessions bias the ranker, and per-symbol min-conf/RR
# overrides are off (the global signals.min_confidence + regime_overrides
# apply uniformly). No manual rule pre-blocks any setup. Instead the live
# forward-test ledger (loops/forward_test_loop.py) records clean per-cell
# outcomes and writes state/symbol_policy_live.json, which the verifier reads
# to VETO cells that clean live data proves lose (n >= min_n AND negative
# realized expectancy AND win-rate below floor). This is the user's "narrower
# over time, data-driven, no manual rules" direction. To re-introduce a manual
# per-symbol rule later, add it under config: signals.symbol_rules.<symbol>
# (``symbol_rule`` merges it on top of this empty default).
DEFAULT_SYMBOL_RULES: dict[str, dict[str, Any]] = {}


def symbol_rule(config: dict[str, Any], symbol: str) -> dict[str, Any]:
    """Return merged default + config rule for a symbol."""
    signals = config.get("signals", {}) or {}
    rule = dict(DEFAULT_SYMBOL_RULES.get(symbol, {}))
    custom = signals.get("symbol_rules", {}) or {}
    if symbol in custom and isinstance(custom[symbol], dict):
        rule.update(custom[symbol])
    return rule


def normalize_setup_type(
    setup_type: str | None,
    *,
    meta: dict[str, Any] | None = None,
    market_context: dict[str, Any] | None = None,
) -> str:
    """Normalize polluted close comments back to an actionable setup label."""
    raw = (setup_type or "").strip()
    if raw in KNOWN_SETUPS:
        return raw

    meta = meta or {}
    market_context = market_context or {}
    nested = market_context.get("market_regime", {}) if isinstance(market_context, dict) else {}
    move_type = (
        market_context.get("move_type")
        or nested.get("move_type")
        or meta.get("move_type")
        or nested.get("phase")
    )
    mapped = MOVE_TYPE_SETUP_MAP.get(str(move_type or "").strip())
    if mapped:
        return mapped

    if meta.get("setup_type") in KNOWN_SETUPS:
        return str(meta["setup_type"])
    if raw.startswith("["):
        return "unknown"
    return raw or "unknown"


def setup_allowed(symbol_rule_data: dict[str, Any], setup_type: str) -> bool:
    allowed = symbol_rule_data.get("allowed_setups")
    if allowed:
        return setup_type in set(allowed)
    return True


def preferred_setup_rank(symbol_rule_data: dict[str, Any], setup_type: str) -> int:
    preferred = symbol_rule_data.get("preferred_setups") or []
    try:
        return list(preferred).index(setup_type)
    except ValueError:
        return len(preferred) + 1


def threshold_overrides(
    symbol_rule_data: dict[str, Any],
    *,
    base_confidence: float,
    base_risk_reward: float,
) -> tuple[float, float]:
    min_conf = float(symbol_rule_data.get("min_confidence", base_confidence))
    min_rr = float(symbol_rule_data.get("min_risk_reward", base_risk_reward))
    return max(base_confidence, min_conf), max(base_risk_reward, min_rr)


# --------------------------------------------------------------------------- #
# Data-driven culturing cell key.                                              #
# --------------------------------------------------------------------------- #
# The forward-test ledger buckets closed trades by (symbol, cell) and the
# verifier queries the veto with the SAME cell construction, so a vetoed cell
# maps 1:1 to a live candidate signal. Cell = setup | regime_primary |
# bias-aligned-with-side | session. Mirrors condition_templates._cell_key plus
# the session axis the user asked strategy policy to split on.


def culturing_cell_key(
    setup_type: str | None,
    regime_primary: str | None,
    regime_bias: str | None,
    side: str | None,
    session: str | None,
) -> str:
    """Cell key shared by the forward-test ledger and the verifier veto."""
    aligned = (
        (regime_bias in ("bullish", "up") and side == "BUY")
        or (regime_bias in ("bearish", "down") and side == "SELL")
    )
    return (
        f"{setup_type or 'unknown'}|{regime_primary or '?'}|"
        f"{'align' if aligned else 'counter'}|{session or '?'}"
    )


def culturing_cell_from_trade(trade: dict[str, Any]) -> str:
    """Cell key for a closed-trade record (has market_context embedded)."""
    mc = trade.get("market_context") or {}
    if not isinstance(mc, dict):
        mc = {}
    reg = mc.get("market_regime") or {}
    if not isinstance(reg, dict):
        reg = {}
    return culturing_cell_key(
        trade.get("setup_type"),
        reg.get("primary"),
        reg.get("bias"),
        trade.get("side"),
        mc.get("session"),
    )
