"""Symbol-specific strategy policy and setup normalization helpers."""

from __future__ import annotations

from typing import Any

from core.setup_library import SETUP_LIBRARY

KNOWN_SETUPS = set(SETUP_LIBRARY.keys())

# 2026-07-31 — broker-comment truncation repair. MT5 order comments are
# truncated to 16 chars at the broker; a comment written as "qagent_donchian_breakout"
# comes back as "qagent_donchian_" → extracted setup "donchian_" (9 chars). This
# splits ONE setup across TWO cells in regime_evolution / forward_test_ledger
# (e.g. "ma_crosso" n=14 vs "ma_crossover" n=13; "donchian_" n=15 vs
# "donchian_breakout" n=151) so neither cell reaches a robust sample and the
# data-driven veto/gate can't act on the full evidence. The diversification
# stream names (donchian_breakout, ma_crossover, atr_expansion, liquidity) are
# NOT in SETUP_LIBRARY, so they are listed here so their full forms pass through
# unchanged and their truncated prefixes can be repaired by prefix-match below.
_CANONICAL_SETUP_NAMES = KNOWN_SETUPS | {
    "donchian_breakout",
    "ma_crossover",
    "atr_expansion",
    "liquidity",
}

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

    # 2026-07-31 — repair broker-comment truncation. If the raw label is a
    # strict prefix of exactly one canonical setup name (and is long enough
    # not to be a coincidental short prefix), resolve it to the full name.
    # This unifies the split cells (ma_crosso→ma_crossover, donchian_→
    # donchian_breakout, mean_reve→mean_reversion, false_bre→false_breakout)
    # so the culturing ledger and evolution cells accumulate evidence on one
    # key instead of two. Ambiguous (multi-match) or short prefixes are left
    # unchanged rather than guessed.
    if raw and len(raw) >= 8 and raw not in _CANONICAL_SETUP_NAMES:
        _matches = [s for s in _CANONICAL_SETUP_NAMES if s.startswith(raw)]
        if len(_matches) == 1:
            return _matches[0]

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
    meta = trade.get("signal_meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    mc = trade.get("market_context") or meta.get("market_context") or {}
    if not isinstance(mc, dict):
        mc = {}
    reg = mc.get("market_regime") or {}
    if not isinstance(reg, dict):
        reg = {}
    setup = normalize_setup_type(
        trade.get("setup_type") or meta.get("setup_type"),
        meta=meta,
        market_context=mc,
    )
    return culturing_cell_key(
        setup,
        reg.get("primary") or meta.get("regime_primary") or mc.get("regime"),
        reg.get("bias") or meta.get("regime_bias"),
        trade.get("side") or meta.get("side"),
        mc.get("session") or meta.get("session"),
    )
