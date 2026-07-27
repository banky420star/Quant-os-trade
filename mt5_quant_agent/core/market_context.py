"""Market Context Engine — what is the market trying to do?"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from core.session_scorer import resolve_trading_session
from core.utils import utc_now_iso


class MarketContextEngine:
    """Derive market regime, phase, intent, and session context from features + candles."""

    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger("market_context")

    def analyze_all(
        self,
        features_data: dict[str, Any],
        candles_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        symbols = features_data.get("symbols", {})
        candle_symbols = (candles_data or {}).get("symbols", {})

        result: dict[str, Any] = {
            "timestamp": utc_now_iso(),
            "symbols": {},
        }

        for symbol, feat in symbols.items():
            m5 = candle_symbols.get(symbol, {}).get("M5", [])
            result["symbols"][symbol] = self._analyze_symbol(symbol, feat, m5)

        return result

    def _analyze_symbol(self, symbol: str, feat: dict[str, Any], m5_candles: list) -> dict[str, Any]:
        closes = [float(c["close"]) for c in m5_candles[-50:]] if m5_candles else []
        highs = [float(c["high"]) for c in m5_candles[-50:]] if m5_candles else []
        lows = [float(c["low"]) for c in m5_candles[-50:]] if m5_candles else []

        regime = self._regime(feat, closes)
        phase = self._phase(feat, closes)
        move = self._move_type(feat, phase)
        trend_strength = self._trend_strength(feat, closes)
        vol_trend = self._volatility_trend(feat, closes)
        session = self._session()
        unusual = self._unusual_move(closes)
        intent = self._market_intent(feat, regime, phase, move, trend_strength)

        return {
            "symbol": symbol,
            "regime": regime,
            "phase": phase,
            "move_type": move,
            "trend_strength": trend_strength,
            "volatility_trend": vol_trend,
            "session": session,
            "move_unusual": unusual,
            "market_intent": intent,
            "questions": {
                "trending_or_ranging": regime,
                "expansion_or_compression": phase,
                "pullback_or_continuation": move,
                "impulse_or_correction": "impulse" if move in ("continuation", "breakout") else "correction",
                "trend_strength_level": trend_strength,
                "volatility_increasing": vol_trend == "increasing",
                "session": session,
                "statistically_unusual": unusual.get("unusual", False),
            },
        }

    def _regime(self, feat: dict, closes: list) -> str:
        aligned = feat.get("timeframe_alignment", False)
        m5 = feat.get("m5_trend", "neutral")
        m15 = feat.get("m15_trend", "neutral")
        bb_pos = feat.get("bb_position", 0.5)

        if aligned and m5 in ("bullish", "bearish"):
            return "trending"
        if not aligned:
            return "transitional"
        if 0.35 < bb_pos < 0.65 and closes:
            rng = (max(closes[-20:]) - min(closes[-20:])) / closes[-1] if closes[-1] else 0
            if rng < feat.get("atr_ratio", 0.001) * 8:
                return "ranging"
        return "ranging" if m5 == "neutral" else "trending"

    def _phase(self, feat: dict, closes: list) -> str:
        vol_regime = feat.get("volatility_regime", "normal")
        atr_ratio = feat.get("atr_ratio", 0)
        if vol_regime == "high" or atr_ratio > 0.002:
            return "expansion"
        if vol_regime == "low" or atr_ratio < 0.0004:
            return "compression"
        if closes and len(closes) >= 10:
            recent_range = max(closes[-10:]) - min(closes[-10:])
            prior_range = max(closes[-20:-10]) - min(closes[-20:-10]) if len(closes) >= 20 else recent_range
            if prior_range > 0 and recent_range / prior_range > 1.3:
                return "expansion"
            if prior_range > 0 and recent_range / prior_range < 0.7:
                return "compression"
        return "neutral"

    def _move_type(self, feat: dict, phase: str) -> str:
        breakout = feat.get("breakout", "none")
        rejection = feat.get("rejection", "none")
        m5 = feat.get("m5_trend", "neutral")

        if breakout in ("breakout", "breakdown"):
            return "breakout"
        if breakout in ("breakout_retest", "breakdown_retest"):
            return "pullback"
        if rejection != "none":
            return "pullback"
        if feat.get("timeframe_alignment") and m5 in ("bullish", "bearish"):
            return "continuation"
        if phase == "compression":
            return "compression_setup"
        return "unclear"

    def _trend_strength(self, feat: dict, closes: list) -> str:
        if not feat.get("timeframe_alignment"):
            return "weak"
        stoch_k = feat.get("stoch_k", 50)
        m5 = feat.get("m5_trend", "neutral")
        if m5 == "neutral":
            return "weak"
        if closes and len(closes) >= 15:
            slope = (closes[-1] - closes[-15]) / closes[-15] if closes[-15] else 0
            if abs(slope) > 0.003:
                return "strong"
            if abs(slope) > 0.001:
                return "moderate"
        if (m5 == "bullish" and stoch_k > 55) or (m5 == "bearish" and stoch_k < 45):
            return "moderate"
        return "weak"

    def _volatility_trend(self, feat: dict, closes: list) -> str:
        if not closes or len(closes) < 20:
            return "stable"
        rets = np.diff(closes[-20:]) / np.array(closes[-20:-1])
        first = np.std(rets[:10]) if len(rets) >= 10 else 0
        second = np.std(rets[10:]) if len(rets) > 10 else 0
        if second > first * 1.25:
            return "increasing"
        if second < first * 0.75:
            return "decreasing"
        return "stable"

    def _session(self) -> str:
        return resolve_trading_session()

    def _unusual_move(self, closes: list) -> dict[str, Any]:
        if len(closes) < 30:
            return {"unusual": False, "z_score": 0.0}
        rets = np.diff(closes[-30:]) / np.array(closes[-30:-1])
        last = rets[-1] if len(rets) else 0
        mean = float(np.mean(rets))
        std = float(np.std(rets)) or 1e-9
        z = (last - mean) / std
        return {"unusual": abs(z) > 2.0, "z_score": round(float(z), 2)}

    def _market_intent(
        self,
        feat: dict,
        regime: str,
        phase: str,
        move: str,
        strength: str,
    ) -> str:
        m5 = feat.get("m5_trend", "neutral")
        m15 = feat.get("m15_trend", "neutral")
        price = feat.get("price", 0)
        support = feat.get("support", price)
        resistance = feat.get("resistance", price)

        if regime == "trending" and move == "continuation":
            return f"Market pushing {m5} — trend continuation with {strength} momentum"
        if regime == "ranging" and phase == "compression":
            return "Market coiling in range — energy building for breakout"
        if move == "pullback":
            return f"Market pulling back within {m5} bias — testing structure"
        if move == "breakout":
            return f"Market attempting breakout from {support:.2f}/{resistance:.2f} zone"
        if regime == "transitional":
            return f"M5 ({m5}) and M15 ({m15}) conflict — market undecided"
        if feat.get("rejection") != "none":
            return f"Market rejecting level — {feat.get('rejection')} at price {price}"
        return f"Market in {regime} regime, {phase} phase, no dominant intent"