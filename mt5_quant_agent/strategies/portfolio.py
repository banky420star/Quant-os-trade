"""Strategy portfolio orchestrator — risk-parity + meta-decision.

The bot now runs four parallel signal streams:
  1. Donchian Breakout    (trend-following, all symbols)
  2. Bollinger Reversion (FX only)     (mean-reversion, contrarian)
  3. ATR Expansion       (volatility-triggered breakout, all symbols)
  4. Decision Engine     (the existing 7-engine confidence-tree)

Each stream has a `risk_share_pct` summing to 1.0. The meta-decision picks the
best candidate per symbol per cycle using:

    score = confidence_pct * regime_fit_pct * bias_alignment_boost

For risk parity sizing, the existing position_sizing pipeline scales the
candidate's risk_pct by ``stream.risk_share_pct`` (so donchian of total 0.40%
risk gets 0.10% effective). Per-symbol + per-stream caps are enforced by mark.

Public API:
    FX_SYMBOLS          list[str]
    RISK_PARITY_DEFAULTS  dict[str, float]      # stream -> share
    regime_affinity(strategy, regime_ctx) -> float (0..1)
    bias_aligned(strategy, signal, m5_trend) -> bool
    score_candidate(candidate, regime_ctx)  -> float
    meta_decide(candidates, regime_ctx)     -> chosen per symbol
    risk_parity_weight(stream) -> float
    annotate_candidate_for_risk_parity(candidate) -> candidate (in-place stamp)
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

# FX pairs in the configured universe — Bollinger reversion is restricted to FX.
FX_SYMBOLS: tuple[str, ...] = (
    "EURUSDm", "GBPUSDm", "USDJPYm", "USDCHFm", "AUDUSDm",
)


# Capital-risk share per strategy stream. Must sum to 1.0
RISK_PARITY_DEFAULTS: dict[str, float] = {
    "donchian_breakout":   0.25,
    "bollinger_reversion": 0.25,
    "atr_expansion":       0.25,
    "decision_engine":     0.25,
}


# Per-symbol per-stream cap (max open trades per stream per symbol).
PER_STREAM_PER_SYMBOL_CAP: int = 2


# Regime affinity table — each strategy's affinity 0..1 for each regime primary.
REGIME_AFFINITY: dict[str, dict[str, float]] = {
    "donchian_breakout": {
        "strong_trend":     1.00,
        "weak_trend":       0.80,
        "expansion":        0.90,
        "compression":      0.20,
        "range":            0.15,
        "accumulation":     0.30,
        "distribution":     0.50,
        "volatility_spike": 0.55,
        "transitional":     0.40,
    },
    "bollinger_reversion": {
        "strong_trend":     0.20,
        "weak_trend":       0.40,
        "expansion":        0.30,
        "compression":      0.95,
        "range":            0.95,
        "accumulation":     0.85,
        "distribution":     0.70,
        "volatility_spike": 0.15,
        "transitional":     0.50,
    },
    "atr_expansion": {
        "strong_trend":     0.85,
        "weak_trend":       0.65,
        "expansion":        1.00,
        "compression":      0.10,
        "range":            0.20,
        "accumulation":     0.30,
        "distribution":     0.65,
        "volatility_spike": 0.95,
        "transitional":     0.45,
    },
    "decision_engine": {
        # Decision engine matches whatever confidence_tree gives; broad regime OK
        "strong_trend":     0.70,
        "weak_trend":       0.70,
        "expansion":        0.65,
        "compression":      0.60,
        "range":            0.65,
        "accumulation":     0.60,
        "distribution":     0.60,
        "volatility_spike": 0.30,
        "transitional":     0.55,
    },
}


def is_fx_symbol(symbol: str) -> bool:
    return symbol in FX_SYMBOLS


def risk_parity_weight(stream: str, config: dict[str, Any] | None = None) -> float:
    """Return risk-share for a strategy stream (defaults to 25% each)."""
    if not config:
        return RISK_PARITY_DEFAULTS.get(stream, 0.0)
    div = config.get("strategies", {}).get("diversification", {})
    shares = div.get("risk_shares") or RISK_PARITY_DEFAULTS
    return float(shares.get(stream, RISK_PARITY_DEFAULTS.get(stream, 0.0)))


def regime_affinity(stream: str, regime_ctx: dict[str, Any] | None) -> float:
    """Return affinity score 0..1 for the given strategy / market regime."""
    if not regime_ctx:
        return 0.5
    primary = regime_ctx.get("primary") or regime_ctx.get("regime") or "transitional"
    table = REGIME_AFFINITY.get(stream, {})
    return float(table.get(primary, 0.5))


def bias_aligned(stream: str, side: str, m5_trend: str | None) -> float:
    """Boost factor when M5 trend aligns with the side chosen by the strategy.

    Trend-following strategies (Donchian, ATR Expansion) REQUIRE alignment.
    Mean-reversion (Bollinger) gets PENALIZED in hot trends.
    Decision Engine is neutral.
    """
    if not m5_trend or m5_trend == "neutral":
        return 0.85
    is_long_trend = (m5_trend == "bullish" and side == "BUY")
    is_short_trend = (m5_trend == "bearish" and side == "SELL")
    aligned = is_long_trend or is_short_trend

    if stream in ("donchian_breakout", "atr_expansion"):
        # Trend-following: alignment = REQUIRED, misalignment = hard veto
        return 1.10 if aligned else 0.10
    if stream == "bollinger_reversion":
        # Contrarian: alignment = BAD (in a hot trend, MR is dangerous)
        return 0.60 if aligned else 1.00
    # decision_engine: slightly prefer alignment
    return 1.00 if aligned else 0.85


def score_candidate(
    candidate: dict[str, Any],
    regime_ctx_for_symbol: dict[str, Any] | None,
) -> float:
    """Compute the meta-decision score for a single candidate."""
    stream = str(candidate.get("source") or candidate.get("setup_type") or "")
    confidence = float(candidate.get("confidence") or 0) / 100.0
    confidence = max(0.0, min(1.0, confidence))

    aff = regime_affinity(stream, regime_ctx_for_symbol)
    m5 = None
    if isinstance(candidate.get("market_context"), dict):
        ctx = candidate["market_context"]
        m5 = (candidate.get("market_context") or {}).get("m5_trend") or candidate.get("m5_trend")
    side = candidate.get("side", "BUY")
    bias = bias_aligned(stream, side, m5)

    return float(confidence * aff * bias)


def annotate_candidate_for_risk_parity(
    candidate: dict[str, Any], config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stamp risk_parity_* fields onto candidate for downstream sizing."""
    stream = str(candidate.get("source") or candidate.get("setup_type") or "")
    share = risk_parity_weight(stream, config)
    candidate["risk_parity_stream"] = stream
    candidate["risk_parity_share"] = share
    return candidate


def meta_decide(
    candidates: list[dict[str, Any]],
    regime_ctx: dict[str, Any] | None,
    *,
    min_score: float = 0.20,
    per_symbol_cap: int = 1,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pick the best candidate per symbol.

    Args:
        candidates: list of candidates from all 4 streams.
        regime_ctx: market_regime ctx keyed by symbol.
        min_score: drop candidates with score below threshold.
        per_symbol_cap: max picks per symbol after scoring.

    Returns:
        (chosen, diagnostics) where chosen is the best-per-symbol list and
        diagnostics includes rejected_candidates with reasons.
    """
    regime_ctx = regime_ctx or {}
    by_symbol_scores: dict[str, list[tuple[float, dict[str, Any]]]] = defaultdict(list)
    diag = {
        "considered": len(candidates),
        "by_stream": defaultdict(int),
        "rejected_low_score": 0,
        "chosen": [],
    }

    for cand in candidates:
        sym = cand.get("symbol")
        if not sym:
            continue
        stream = str(cand.get("source") or cand.get("setup_type") or "decision_engine")
        # Bollinger reversion is FX-only — drop other symbols
        if stream == "bollinger_reversion" and not is_fx_symbol(sym):
            cand["meta_reject_reason"] = "bollinger_fx_only"
            diag["rejected_low_score"] += 1
            continue
        sym_regime = regime_ctx.get(sym) or {}
        s = score_candidate(cand, sym_regime)
        diag["by_stream"][stream] += 1
        if s < min_score:
            cand["meta_reject_reason"] = f"score_below_min({s:.2f}<{min_score})"
            diag["rejected_low_score"] += 1
            continue
        by_symbol_scores[sym].append((s, cand))

    chosen: list[dict[str, Any]] = []
    for sym, scored in by_symbol_scores.items():
        scored.sort(key=lambda t: t[0], reverse=True)
        top = scored[:max(1, per_symbol_cap)]
        for score, cand in top:
            cand["meta_score"] = round(score, 4)
            chosen.append(cand)
            diag["chosen"].append({
                "symbol": sym,
                "stream": cand.get("source"),
                "setup": cand.get("setup_type"),
                "side": cand.get("side"),
                "score": round(score, 4),
                "confidence": cand.get("confidence"),
                "chosen_from_pool_size": len(scored),
            })
    diag["by_stream"] = dict(diag["by_stream"])
    diag["chosen_count"] = len(chosen)
    diag["chosen_per_symbol_count"] = {s: sum(1 for c in chosen if c.get("symbol") == s)
                                       for s in {c.get("symbol") for c in chosen}}
    return chosen, diag
