"""Evidence Engine — evidence-first scores (0-1), not indicator crosses."""

from __future__ import annotations

import logging
from typing import Any


class EvidenceEngine:
    """Compute normalized evidence vector for each symbol."""

    def __init__(self, config: dict[str, Any] | None = None, logger: logging.Logger | None = None):
        self.config = config or {}
        self.logger = logger or logging.getLogger("evidence_engine")

    def compute_all(
        self,
        features_data: dict[str, Any],
        context_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from core.utils import utc_now_iso

        contexts = (context_data or {}).get("symbols", {})
        evidence: dict[str, Any] = {"timestamp": utc_now_iso(), "symbols": {}}

        for symbol, feat in features_data.get("symbols", {}).items():
            ctx = contexts.get(symbol, {})
            evidence["symbols"][symbol] = self.compute_symbol(feat, ctx)

        return evidence

    def compute_symbol(self, feat: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        price = feat.get("price", 0) or 1
        support = feat.get("support", price)
        resistance = feat.get("resistance", price)
        atr = feat.get("atr", price * 0.001)

        trend = self._trend_evidence(feat, context)
        momentum = self._momentum_evidence(feat)
        structure = self._structure_evidence(feat, price, support, resistance, atr)
        liquidity = self._liquidity_evidence(feat, price, support, resistance)
        volatility = self._volatility_evidence(feat, context)
        volume = self._volume_evidence(feat)
        risk = self._risk_evidence(feat, context)

        return {
            "trend": round(trend, 2),
            "momentum": round(momentum, 2),
            "structure": round(structure, 2),
            "liquidity": round(liquidity, 2),
            "volatility": round(volatility, 2),
            "volume": round(volume, 2),
            "risk": round(risk, 2),
        }

    def _trend_evidence(self, feat: dict, ctx: dict) -> float:
        score = 0.4
        if feat.get("timeframe_alignment"):
            score += 0.35
        strength = ctx.get("trend_strength", "weak")
        score += {"strong": 0.2, "moderate": 0.12, "weak": 0.04}.get(strength, 0)
        if ctx.get("regime") == "trending":
            score += 0.1
        return min(1.0, score)

    def _momentum_evidence(self, feat: dict) -> float:
        k = feat.get("stoch_k", 50)
        d = feat.get("stoch_d", 50)
        dist = abs(k - 50) / 50
        score = 0.3 + dist * 0.4
        if feat.get("stoch_cross") != "none":
            score += 0.15
        if abs(k - d) > 5:
            score += 0.1
        return min(1.0, score)

    def _structure_evidence(self, feat: dict, price: float, support: float, resistance: float, atr: float) -> float:
        range_ = resistance - support
        if range_ <= 0:
            return 0.5
        pos = (price - support) / range_
        dist_edge = min(pos, 1 - pos)
        score = 0.5 + (0.5 - dist_edge) * 0.8
        if feat.get("rejection") != "none":
            score += 0.15
        if feat.get("breakout") not in ("none",):
            score += 0.1
        sr_buffer = atr / price if price else 0
        if dist_edge < sr_buffer * 2:
            score += 0.1
        return min(1.0, max(0.0, score))

    def _liquidity_evidence(self, feat: dict, price: float, support: float, resistance: float) -> float:
        near_support = abs(price - support) / price < 0.002 if price else False
        near_resistance = abs(resistance - price) / price < 0.002 if price else False
        if near_support or near_resistance:
            return 0.75
        bb = feat.get("bb_position", 0.5)
        if bb < 0.1 or bb > 0.9:
            return 0.7
        return 0.45

    def _volatility_evidence(self, feat: dict, ctx: dict) -> float:
        regime = feat.get("volatility_regime", "normal")
        phase = ctx.get("phase", "neutral")
        base = {"low": 0.35, "normal": 0.65, "high": 0.55}.get(regime, 0.5)
        if phase == "expansion":
            base += 0.1
        if phase == "compression":
            base += 0.05
        if ctx.get("volatility_trend") == "increasing":
            base += 0.08
        return min(1.0, base)

    def _volume_evidence(self, feat: dict) -> float:
        ratio = feat.get("volume_ratio", 1.0)
        return min(1.0, max(0.0, 0.3 + (ratio - 0.5) * 0.5))

    def _risk_evidence(self, feat: dict, ctx: dict) -> float:
        """Lower risk score = safer trade (inverted for combination)."""
        risk = 0.2
        if feat.get("volatility_regime") == "high":
            risk += 0.25
        if ctx.get("move_unusual", {}).get("unusual"):
            risk += 0.2
        if not feat.get("timeframe_alignment"):
            risk += 0.2
        if feat.get("atr_ratio", 0) < 0.0003:
            risk += 0.15
        return min(1.0, risk)