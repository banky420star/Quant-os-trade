"""Market Regime Engine — rich market state taxonomy."""

from __future__ import annotations

import logging
from typing import Any

REGIME_LABELS = (
    "strong_trend",
    "weak_trend",
    "range",
    "compression",
    "expansion",
    "volatility_spike",
    "accumulation",
    "distribution",
    "transitional",
)


class MarketRegimeEngine:
    """Classify market into actionable regime labels beyond bullish/bearish."""

    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger("market_regime")

    def classify_symbol(
        self,
        feat: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Return primary regime, sub-regimes, and trading bias."""
        regime = context.get("regime", "ranging")
        phase = context.get("phase", "neutral")
        move = context.get("move_type", "unclear")
        strength = context.get("trend_strength", "weak")
        vol_trend = context.get("volatility_trend", "stable")
        unusual = context.get("move_unusual", {})
        m5 = feat.get("m5_trend", "neutral")
        bb_pos = feat.get("bb_position", 0.5)
        vol_regime = feat.get("volatility_regime", "normal")

        primary = self._primary_regime(
            regime, phase, move, strength, vol_trend, unusual, vol_regime, bb_pos,
        )
        bias = self._directional_bias(m5, primary, bb_pos, move)
        tags = self._tags(regime, phase, move, strength, vol_trend, unusual)

        return {
            "primary": primary,
            "bias": bias,
            "tags": tags,
            "tradeable": primary not in ("transitional", "volatility_spike"),
            "description": self._describe(primary, bias, context),
        }

    def classify_all(
        self,
        features_data: dict[str, Any],
        context_data: dict[str, Any],
    ) -> dict[str, Any]:
        symbols_ctx = context_data.get("symbols", {})
        result: dict[str, Any] = {"symbols": {}}
        for symbol, feat in features_data.get("symbols", {}).items():
            ctx = symbols_ctx.get(symbol, {})
            result["symbols"][symbol] = self.classify_symbol(feat, ctx)
        return result

    def _primary_regime(
        self,
        regime: str,
        phase: str,
        move: str,
        strength: str,
        vol_trend: str,
        unusual: dict,
        vol_regime: str,
        bb_pos: float,
    ) -> str:
        if unusual.get("unusual") and vol_regime == "high":
            return "volatility_spike"
        if phase == "compression" or move == "compression_setup":
            return "compression"
        if phase == "expansion" and vol_trend == "increasing":
            return "expansion"
        if regime == "trending":
            if strength == "strong":
                return "strong_trend"
            return "weak_trend"
        if regime == "ranging":
            if 0.4 < bb_pos < 0.6 and phase == "compression":
                return "accumulation"
            if bb_pos > 0.85 or bb_pos < 0.15:
                return "distribution"
            return "range"
        if regime == "transitional":
            return "transitional"
        return "range"

    def _directional_bias(self, m5: str, primary: str, bb_pos: float, move: str) -> str:
        if primary in ("accumulation", "compression"):
            return "neutral"
        if m5 == "bullish":
            return "bullish"
        if m5 == "bearish":
            return "bearish"
        if bb_pos > 0.7:
            return "bearish"
        if bb_pos < 0.3:
            return "bullish"
        if move == "continuation":
            return m5 if m5 != "neutral" else "neutral"
        return "neutral"

    def _tags(
        self,
        regime: str,
        phase: str,
        move: str,
        strength: str,
        vol_trend: str,
        unusual: dict,
    ) -> list[str]:
        tags = [regime, phase, move, strength]
        if vol_trend != "stable":
            tags.append(f"vol_{vol_trend}")
        if unusual.get("unusual"):
            tags.append("unusual_move")
        return [t for t in tags if t]

    def _describe(self, primary: str, bias: str, ctx: dict) -> str:
        intent = ctx.get("market_intent", "")
        return f"{primary.replace('_', ' ').title()} ({bias}) — {intent}" if intent else f"{primary.replace('_', ' ').title()} market, {bias} bias"