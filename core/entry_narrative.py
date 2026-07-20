"""Plain-English entry narrative for every trade.

Goal (user request): every trade must carry a human-readable reason it
entered with, describing the actual technical trigger on the entry bar —
e.g. "EMA20 slope up on M5 + stochastic bullish cross + volume 1.6x -> entered
BUY trend_continuation" — alongside that trade's profitability.

Honesty: the narrative is composed ONLY from indicators the FeatureEngine
actually computes (EMA20 slope -> trend, Bollinger bands, Stochastic cross,
ATR, volume ratio, support/resistance, candle rejection, breakout/retest,
volatility regime) and the setup the DecisionEngine chose. The bot does NOT
compute ALMA or doji patterns, so those words never appear here. If you want
ALMA/doji, that is a FeatureEngine addition, not a narrative change.

Used by DecisionEngine to stamp ``entry_narrative`` on every signal, which
trade_enrichment then carries onto the closed-trade record so the evaluator
and edge DB can show reason + profitability together.
"""

from __future__ import annotations

from typing import Any

from core.setup_library import get_setup


def _fmt_price(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "?"
    if f >= 1000:
        return f"{f:,.1f}"
    if f >= 10:
        return f"{f:.2f}"
    return f"{f:.5f}"


def _trigger_clauses(feat: dict[str, Any]) -> list[str]:
    """Specific technical events that fired on the entry bar (all real)."""
    clauses: list[str] = []

    sc = feat.get("stoch_cross")
    if sc == "bullish_cross":
        clauses.append("stochastic bullish cross (K crossed above D)")
    elif sc == "bearish_cross":
        clauses.append("stochastic bearish cross (K crossed below D)")

    br = feat.get("breakout")
    if br == "breakout":
        clauses.append(f"close broke resistance {_fmt_price(feat.get('resistance'))}")
    elif br == "breakdown":
        clauses.append(f"close broke support {_fmt_price(feat.get('support'))}")
    elif br == "breakout_retest":
        clauses.append(f"retest of broken resistance {_fmt_price(feat.get('resistance'))}")
    elif br == "breakdown_retest":
        clauses.append(f"retest of broken support {_fmt_price(feat.get('support'))}")

    rej = feat.get("rejection")
    if rej == "bullish_rejection":
        clauses.append("bullish rejection wick")
    elif rej == "bearish_rejection":
        clauses.append("bearish rejection wick")

    bbp = feat.get("bb_position")
    if bbp is not None:
        try:
            p = float(bbp)
        except (TypeError, ValueError):
            p = None
        if p is not None:
            if p <= 0.12:
                clauses.append(f"price tagging BB lower (pos {p:.2f})")
            elif p >= 0.88:
                clauses.append(f"price tagging BB upper (pos {p:.2f})")
            elif p < 0.42:
                clauses.append(f"price below BB middle (pos {p:.2f})")
            elif p > 0.58:
                clauses.append(f"price above BB middle (pos {p:.2f})")
    return clauses


def _bias_clause(feat: dict[str, Any]) -> str:
    """The directional bias the entry was taken with."""
    m5 = feat.get("m5_trend", "neutral")
    m15 = feat.get("m15_trend", "neutral")
    aligned = feat.get("timeframe_alignment", False)
    if aligned:
        return f"EMA20 slope {m5} on M5, M15 aligned ({m15})"
    return f"EMA20 slope {m5} on M5, M15 {m15} (not aligned)"


def build_entry_narrative(
    setup_type: str,
    side: str,
    feat: dict[str, Any],
    market_context: dict[str, Any] | None = None,
) -> str:
    """Compose a one-sentence entry reason from real per-bar features.

    Shape: ``"<SIDE> <setup>: <bias>; <triggers...>. <regime/session>."``
    Every clause is derived from a computed feature value, never fabricated.
    """
    side = (side or "").upper()
    mc = market_context or {}
    regime = (mc.get("market_regime") or {}).get("primary")
    bias = (mc.get("market_regime") or {}).get("bias")
    session = mc.get("session")

    defn = get_setup(setup_type)
    setup_label = defn.display_name if defn else (setup_type or "setup")

    clauses: list[str] = [_bias_clause(feat)]
    clauses.extend(_trigger_clauses(feat))

    vr = feat.get("volume_ratio")
    try:
        if vr is not None and float(vr) >= 1.3:
            clauses.append(f"volume {float(vr):.1f}x avg")
    except (TypeError, ValueError):
        pass

    body = "; ".join(c for c in clauses if c)

    # Regime/session tail (context the entry was taken in).
    tail_bits: list[str] = []
    if regime:
        tail_bits.append(f"regime {regime}")
    if bias and bias not in ("neutral", "none", "", None):
        tail_bits.append(f"bias {bias}")
    if session:
        tail_bits.append(f"session {session}")
    tail = ", ".join(tail_bits)

    hint = ""
    if defn and defn.entry_hints and not clauses[1:]:
        # No specific trigger fired -> fall back to the setup's own entry hints
        # so the reason still describes what the setup looks for.
        hint = " (setup looks for: " + "; ".join(defn.entry_hints[:2]) + ")"

    sentence = f"{side} {setup_label}: {body}{hint}."
    if tail:
        sentence += f" {tail}."
    return sentence


_EXIT_REASON_TEXT = {
    "take_profit": "full take-profit (TP1) hit",
    "stop_loss": "initial stop hit",
    "break_even_stop": "break-even stop hit (entry protected)",
    "trailing_stop": "trailing stop hit (profit locked)",
}


def r_multiple(side: str, entry: float, sl: float, exit_price: float) -> float:
    """Capital-independent R of a closed trade (size cancels)."""
    try:
        risk = abs(float(entry) - float(sl))
        if risk <= 0:
            return 0.0
        direction = 1.0 if (side or "").upper() == "BUY" else -1.0
        return (float(exit_price) - float(entry)) * direction / risk
    except (TypeError, ValueError):
        return 0.0


def build_exit_narrative(
    side: str,
    entry: float,
    sl: float,
    tp1: float,
    exit_price: float,
    exit_reason: str | None,
    pnl: float,
) -> str:
    """One-sentence exit reason tied to the realised R and $ result."""
    r = r_multiple(side, entry, sl, exit_price)
    try:
        pnl_s = float(pnl)
    except (TypeError, ValueError):
        pnl_s = 0.0
    text = _EXIT_REASON_TEXT.get(exit_reason or "", exit_reason or "closed")
    return f"Exit {_fmt_price(exit_price)} ({r:+.2f}R, ${pnl_s:+.2f}) — {text}."