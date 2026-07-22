"""Candidate signal generation — no verification or execution."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from core.utils import utc_now_iso


class SignalEngine:
    """Generate candidate trades from feature snapshots."""

    WEIGHTS = {
        "trend": 0.20,
        "momentum": 0.15,
        "structure": 0.20,
        "volatility": 0.10,
        "volume": 0.10,
        "historical_edge": 0.20,
        "risk": 0.05,
    }

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("signal_engine")

    def generate_candidates(
        self,
        features_data: dict[str, Any],
        edge_scores: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Build candidate signals for all symbols."""
        edge_scores = edge_scores or {}
        symbols = features_data.get("symbols", {})
        candidates: list[dict[str, Any]] = []
        from core.trade_limits import max_candidates_per_run
        max_candidates = max_candidates_per_run(self.config)

        for symbol, feat in symbols.items():
            symbol_edges = edge_scores.get("setups", {}).get(symbol, {})
            for signal in self._signals_for_symbol(feat, symbol_edges):
                candidates.append(signal)

        candidates.sort(key=lambda s: s["confidence"], reverse=True)
        trimmed = candidates if max_candidates is None else candidates[:max_candidates]
        dropped = len(candidates) - len(trimmed)
        self.logger.info(
            "Generated %d candidate signals (%d returned, %d trimmed)",
            len(candidates),
            len(trimmed),
            dropped,
        )
        return trimmed

    def _signals_for_symbol(self, feat: dict[str, Any], edge_lookup: dict[str, float]) -> list[dict]:
        signals = []
        generators = [
            self._trend_continuation,
            self._reversal_sr,
            self._bollinger_mean_reversion,
            self._breakout_retest,
            self._stochastic_momentum,
            self._volume_rejection,
        ]
        for gen in generators:
            sig = gen(feat, edge_lookup)
            if sig:
                signals.append(sig)
        return signals

    def _score_signal(self, feat: dict[str, Any], setup_type: str, edge_lookup: dict[str, float]) -> int:
        trend_score = 80 if feat.get("timeframe_alignment") else 45
        if feat.get("m5_trend") == feat.get("m15_trend") == "neutral":
            trend_score = 50

        stoch_k = feat.get("stoch_k", 50)
        momentum_score = abs(stoch_k - 50) * 1.2
        if feat.get("stoch_cross") != "none":
            momentum_score += 15

        price = feat.get("price", 0)
        support = feat.get("support", price)
        resistance = feat.get("resistance", price)
        dist_support = abs(price - support) / price if price else 1
        dist_resistance = abs(resistance - price) / price if price else 1
        structure_score = max(0, 100 - min(dist_support, dist_resistance) * 10000)

        vol_regime = feat.get("volatility_regime", "normal")
        volatility_score = {"low": 30, "normal": 70, "high": 55}.get(vol_regime, 50)

        volume_score = min(100, feat.get("volume_ratio", 1.0) * 50)
        historical_edge_score = edge_lookup.get(setup_type, 50)
        risk_score = 70 if feat.get("atr_ratio", 0) > self.config["filters"].get("min_atr_ratio", 0) else 30

        final = (
            trend_score * self.WEIGHTS["trend"]
            + momentum_score * self.WEIGHTS["momentum"]
            + structure_score * self.WEIGHTS["structure"]
            + volatility_score * self.WEIGHTS["volatility"]
            + volume_score * self.WEIGHTS["volume"]
            + historical_edge_score * self.WEIGHTS["historical_edge"]
            + risk_score * self.WEIGHTS["risk"]
        )
        return int(min(99, max(0, round(final))))

    def _build_signal(
        self,
        feat: dict[str, Any],
        side: str,
        setup_type: str,
        reason: str,
        edge_lookup: dict[str, float],
    ) -> dict[str, Any] | None:
        price = feat["price"]
        atr = feat.get("atr", price * 0.001)
        support = feat.get("support", price - atr)
        resistance = feat.get("resistance", price + atr)

        risk_dist = max(atr * 1.5, price * 0.001)
        reward_dist = risk_dist * 1.5

        if side == "BUY":
            sl = round(price - risk_dist, 5)
            tp1 = round(price + reward_dist, 5)
            tp2 = round(price + reward_dist * 2, 5)
        else:
            sl = round(price + risk_dist, 5)
            tp1 = round(price - reward_dist, 5)
            tp2 = round(price - reward_dist * 2, 5)

        confidence = self._score_signal(feat, setup_type, edge_lookup)
        if confidence < self.config["signals"].get("min_confidence", 70):
            return None

        return {
            "signal_id": str(uuid.uuid4()),
            "symbol": feat["symbol"],
            "side": side,
            "setup_type": setup_type,
            "entry": price,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "reason": reason,
            "confidence": confidence,
            "created_at": utc_now_iso(),
        }

    def _trend_continuation(self, feat: dict, edge_lookup: dict) -> dict | None:
        if not feat.get("timeframe_alignment"):
            return None
        trend = feat.get("m5_trend")
        if trend == "bullish" and feat.get("stoch_k", 50) < 80:
            return self._build_signal(
                feat, "BUY", "trend_continuation",
                "Bullish M5/M15 alignment with room before overbought",
                edge_lookup,
            )
        if trend == "bearish" and feat.get("stoch_k", 50) > 20:
            return self._build_signal(
                feat, "SELL", "trend_continuation",
                "Bearish M5/M15 alignment with room before oversold",
                edge_lookup,
            )
        return None

    def _reversal_sr(self, feat: dict, edge_lookup: dict) -> dict | None:
        price = feat["price"]
        support = feat.get("support", price)
        resistance = feat.get("resistance", price)
        near_support = abs(price - support) / price < 0.002
        near_resistance = abs(resistance - price) / price < 0.002

        if near_support and feat.get("rejection") == "bullish_rejection":
            return self._build_signal(
                feat, "BUY", "reversal_sr",
                "Bullish rejection candle near support",
                edge_lookup,
            )
        if near_resistance and feat.get("rejection") == "bearish_rejection":
            return self._build_signal(
                feat, "SELL", "reversal_sr",
                "Bearish rejection candle near resistance",
                edge_lookup,
            )
        return None

    def _bollinger_mean_reversion(self, feat: dict, edge_lookup: dict) -> dict | None:
        pos = feat.get("bb_position", 0.5)
        if pos >= 0.95:
            return self._build_signal(
                feat, "SELL", "bollinger_mean_reversion",
                "Price at upper Bollinger band — mean reversion sell",
                edge_lookup,
            )
        if pos <= 0.05:
            return self._build_signal(
                feat, "BUY", "bollinger_mean_reversion",
                "Price at lower Bollinger band — mean reversion buy",
                edge_lookup,
            )
        return None

    def _breakout_retest(self, feat: dict, edge_lookup: dict) -> dict | None:
        breakout = feat.get("breakout", "none")
        if breakout == "breakout_retest" and feat.get("m5_trend") == "bullish":
            return self._build_signal(
                feat, "BUY", "breakout_retest",
                "Breakout retest holding above resistance",
                edge_lookup,
            )
        if breakout == "breakdown_retest" and feat.get("m5_trend") == "bearish":
            return self._build_signal(
                feat, "SELL", "breakout_retest",
                "Breakdown retest holding below support",
                edge_lookup,
            )
        return None

    def _stochastic_momentum(self, feat: dict, edge_lookup: dict) -> dict | None:
        cross = feat.get("stoch_cross", "none")
        if cross == "bullish_cross" and feat.get("m5_trend") != "bearish":
            return self._build_signal(
                feat, "BUY", "stochastic_momentum",
                "Stochastic bullish cross with non-bearish M5 trend",
                edge_lookup,
            )
        if cross == "bearish_cross" and feat.get("m5_trend") != "bullish":
            return self._build_signal(
                feat, "SELL", "stochastic_momentum",
                "Stochastic bearish cross with non-bullish M5 trend",
                edge_lookup,
            )
        return None

    def _volume_rejection(self, feat: dict, edge_lookup: dict) -> dict | None:
        if feat.get("volume_ratio", 0) < self.config["filters"].get("min_volume_ratio", 0.8):
            return None
        rejection = feat.get("rejection", "none")
        if rejection == "bullish_rejection":
            return self._build_signal(
                feat, "BUY", "volume_rejection",
                "Volume-confirmed bullish rejection",
                edge_lookup,
            )
        if rejection == "bearish_rejection":
            return self._build_signal(
                feat, "SELL", "volume_rejection",
                "Volume-confirmed bearish rejection",
                edge_lookup,
            )
        return None