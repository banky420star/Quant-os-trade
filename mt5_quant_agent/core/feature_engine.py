"""Technical and quant feature computation from raw candles."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from core.utils import utc_now_iso

# Required per-symbol output fields for downstream loops.
FEATURE_SCHEMA_FIELDS = (
    "price",
    "m5_trend",
    "m15_trend",
    "stoch_k",
    "stoch_d",
    "atr",
    "support",
    "resistance",
    "volume_ratio",
    "rejection",
    "timeframe_alignment",
    "volatility_regime",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "bb_position",
)


class FeatureEngine:
    """Convert OHLCV candles into trading features."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        history_manager: Any | None = None,
        logger: logging.Logger | None = None,
    ):
        self.config = config or {}
        self.history = history_manager
        self.logger = logger or logging.getLogger("feature_engine")
        features_cfg = self.config.get("features", {})
        self.use_history_lookback = bool(features_cfg.get("use_history_lookback", True))
        self.history_lookback_bars = int(features_cfg.get("history_lookback_bars", 500))
        self.min_bars_required = int(features_cfg.get("min_bars_required", 20))

    def compute_all(self, candles_data: dict[str, Any]) -> dict[str, Any]:
        """Compute features for every symbol in a latest_candles.json payload."""
        symbols_data = candles_data.get("symbols", {})
        features: dict[str, Any] = {
            "timestamp": utc_now_iso(),
            "source": candles_data.get("source", "unknown"),
            "symbols": {},
        }

        for symbol, tf_data in symbols_data.items():
            m5 = self._prepare_timeframe_candles(symbol, tf_data.get("M5", []), "M5")
            m15 = self._prepare_timeframe_candles(symbol, tf_data.get("M15", []), "M15")

            if len(m5) < self.min_bars_required or len(m15) < self.min_bars_required:
                self.logger.warning(
                    "Skipping %s — insufficient candle data (M5=%d M15=%d, need %d)",
                    symbol,
                    len(m5),
                    len(m15),
                    self.min_bars_required,
                )
                continue

            features["symbols"][symbol] = self._compute_symbol_features(symbol, m5, m15)

        return features

    def _prepare_timeframe_candles(self, symbol: str, latest: list[dict[str, Any]], timeframe: str) -> list[dict[str, Any]]:
        """Use latest candles and optionally prepend Parquet history for deeper lookback."""
        if not latest:
            return []

        if not self.history or not self.use_history_lookback:
            return latest

        history_bars = self.history.serve_recent(symbol, timeframe, self.history_lookback_bars)
        if not history_bars:
            return latest

        merged = self._merge_candle_series(history_bars, latest)
        if len(merged) > len(latest):
            self.logger.debug(
                "%s %s: augmented %d latest bars with history -> %d total",
                symbol,
                timeframe,
                len(latest),
                len(merged),
            )
        return merged

    @staticmethod
    def _merge_candle_series(older: list[dict[str, Any]], newer: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge candle lists by time; newer bars win on duplicate timestamps."""
        by_time: dict[str, dict[str, Any]] = {}
        for candle in older:
            by_time[str(candle["time"])] = candle
        for candle in newer:
            by_time[str(candle["time"])] = candle
        return [by_time[key] for key in sorted(by_time.keys())]

    def _compute_symbol_features(self, symbol: str, m5: list, m15: list) -> dict[str, Any]:
        df_m5 = self._to_dataframe(m5)
        df_m15 = self._to_dataframe(m15)

        price = float(df_m5["close"].iloc[-1])
        m5_trend = self._trend_direction(df_m5)
        m15_trend = self._trend_direction(df_m15)

        bb = self._bollinger_bands(df_m5)
        stoch = self._stochastic(df_m5)
        atr = self._atr(df_m5)
        vol_avg = float(df_m5["volume"].tail(20).mean())
        vol_ratio = float(df_m5["volume"].iloc[-1] / vol_avg) if vol_avg > 0 else 1.0
        support, resistance = self._support_resistance(df_m5)
        rejection = self._candle_rejection(df_m5)
        breakout = self._breakout_breakdown(df_m5, support, resistance)
        alignment = m5_trend == m15_trend
        volatility_regime = self._volatility_regime(df_m5, atr, price)

        return {
            "symbol": symbol,
            "price": round(price, 5),
            "m5_trend": m5_trend,
            "m15_trend": m15_trend,
            "bb_upper": round(bb["upper"], 5),
            "bb_middle": round(bb["middle"], 5),
            "bb_lower": round(bb["lower"], 5),
            "bb_position": round(bb["position"], 4),
            "stoch_k": round(stoch["k"], 2),
            "stoch_d": round(stoch["d"], 2),
            "stoch_cross": stoch["cross"],
            "atr": round(atr, 5),
            "atr_ratio": round(atr / price, 6) if price else 0.0,
            "volume_avg": round(vol_avg, 2),
            "volume_ratio": round(vol_ratio, 2),
            "support": round(support, 5),
            "resistance": round(resistance, 5),
            "rejection": rejection,
            "breakout": breakout,
            "timeframe_alignment": alignment,
            "volatility_regime": volatility_regime,
        }

    @staticmethod
    def validate_symbol_features(features: dict[str, Any]) -> list[str]:
        """Return missing required schema fields for a symbol feature dict."""
        return [field for field in FEATURE_SCHEMA_FIELDS if field not in features]

    def _to_dataframe(self, candles: list) -> pd.DataFrame:
        df = pd.DataFrame(candles)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        return df

    def _trend_direction(self, df: pd.DataFrame, period: int = 20) -> str:
        ema = df["close"].ewm(span=period, adjust=False).mean()
        slope = float(ema.iloc[-1] - ema.iloc[-5]) if len(ema) >= 5 else 0.0
        if slope > 0:
            return "bullish"
        if slope < 0:
            return "bearish"
        return "neutral"

    def _bollinger_bands(self, df: pd.DataFrame, period: int = 20, std_mult: float = 2.0) -> dict:
        close = df["close"]
        middle = close.rolling(period).mean().iloc[-1]
        std = close.rolling(period).std().iloc[-1]
        upper = middle + std_mult * std
        lower = middle - std_mult * std
        price = close.iloc[-1]
        width = upper - lower
        position = (price - lower) / width if width > 0 else 0.5
        return {"upper": float(upper), "middle": float(middle), "lower": float(lower), "position": float(position)}

    def _stochastic(self, df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> dict:
        low_min = df["low"].rolling(k_period).min()
        high_max = df["high"].rolling(k_period).max()
        denom = high_max - low_min
        k_series = 100 * (df["close"] - low_min) / denom.replace(0, np.nan)
        k_series = k_series.fillna(50)
        d_series = k_series.rolling(d_period).mean()

        k = float(k_series.iloc[-1])
        d = float(d_series.iloc[-1])
        k_prev = float(k_series.iloc[-2]) if len(k_series) > 1 else k
        d_prev = float(d_series.iloc[-2]) if len(d_series) > 1 else d

        cross = "none"
        if k_prev <= d_prev and k > d:
            cross = "bullish_cross"
        elif k_prev >= d_prev and k < d:
            cross = "bearish_cross"

        return {"k": k, "d": d, "cross": cross}

    def _atr(self, df: pd.DataFrame, period: int = 14) -> float:
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1])

    def _support_resistance(self, df: pd.DataFrame, lookback: int = 50) -> tuple[float, float]:
        window = df.tail(lookback)
        support = float(window["low"].min())
        resistance = float(window["high"].max())
        return support, resistance

    def _candle_rejection(self, df: pd.DataFrame) -> str:
        last = df.iloc[-1]
        body = abs(last["close"] - last["open"])
        upper_wick = last["high"] - max(last["open"], last["close"])
        lower_wick = min(last["open"], last["close"]) - last["low"]
        range_ = last["high"] - last["low"]
        if range_ <= 0:
            return "none"

        if upper_wick > body * 1.5 and upper_wick > lower_wick:
            return "bearish_rejection"
        if lower_wick > body * 1.5 and lower_wick > upper_wick:
            return "bullish_rejection"
        return "none"

    def _breakout_breakdown(self, df: pd.DataFrame, support: float, resistance: float) -> str:
        close = float(df["close"].iloc[-1])
        prev_close = float(df["close"].iloc[-2]) if len(df) > 1 else close

        if prev_close <= resistance and close > resistance:
            return "breakout"
        if prev_close >= support and close < support:
            return "breakdown"
        if close > resistance * 0.998 and close < resistance:
            return "breakout_retest"
        if close < support * 1.002 and close > support:
            return "breakdown_retest"
        return "none"

    def _volatility_regime(self, df: pd.DataFrame, atr: float, price: float) -> str:
        atr_ratio = atr / price if price else 0
        recent = df["close"].pct_change().tail(20).std()
        if atr_ratio > 0.002 or (recent and recent > 0.003):
            return "high"
        if atr_ratio < 0.0003:
            return "low"
        return "normal"