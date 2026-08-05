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
    "bb_squeeze_pct",
    "rsi",
    "macd",
    "macd_signal",
    "macd_hist",
    "macd_cross",
    "cci",
    "mfi",
    "adx",
    "di_plus",
    "di_minus",
    "obv",
    "obv_ema",
    "obv_cross",
    "atr_pct",
    "cmf",
    "vwap",
    "vwap_upper",
    "vwap_lower",
    "vwap_position",
    "supertrend_dir",
    "supertrend_flip",
    "ichimoku_tenkan",
    "ichimoku_kijun",
    "ichimoku_senkou_a",
    "ichimoku_senkou_b",
    "ichimoku_cloud_position",
    "ichimoku_tk_cross",
    "fvg",
    "fvg_size_atr",
    "engulfing",
    "inside_bar",
    "close_streak",
    "order_block",
    "ha_trend",
)

# 2026-08-04 — Opening Range Breakout (ORB) session-open anchors, UTC (hour,
# minute) of each symbol's PRIMARY cash session. Used by _orb to define the
# opening range. Equities use their real cash open; 24h markets (FX/metals/oil/
# crypto) + Asian indices anchor at 00:00 UTC so they get a deterministic daily
# ORB (and fill the Asian-session 21:00-07:00 UTC coverage gap that the
# london_/ny_ killzone setups leave empty).
OR_SESSION_OPEN_UTC: dict[str, tuple[int, int]] = {
    "US30m": (13, 30), "US500m": (13, 30), "NAS100m": (13, 30),  # US RTH 13:30 UTC
    "UK100m": (8, 0), "FR40m": (8, 0),                            # London 08:00 UTC
    "JP225m": (0, 0),                                             # Tokyo 00:00 UTC
    "XAUUSDm": (0, 0), "USOILm": (0, 0), "BTCUSDm": (0, 0),
    "EURUSDm": (0, 0), "GBPUSDm": (0, 0), "AUDUSDm": (0, 0),
    "USDJPYm": (0, 0), "USDCHFm": (0, 0),                         # 24h -> 00:00 anchor
}
OR_BARS_DEFAULT = 12  # 12 M5 bars = 1-hour opening range


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
        prepared_m5: dict[str, list] = {}

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

            prepared_m5[symbol] = m5
            features["symbols"][symbol] = self._compute_symbol_features(symbol, m5, m15)

        # 2026-08-04 — cross-symbol intermarket pass (research setup #2).
        # Needs BOTH symbols' aligned M5 closes, so it runs here in compute_all
        # (all symbols' candles are in scope) instead of the per-symbol path.
        self._inject_intermarket(features.get("symbols", {}), prepared_m5)

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
        bb_squeeze_pct = self._bb_squeeze(df_m5)
        rsi = self._rsi(df_m5)
        macd = self._macd(df_m5)
        cci = self._cci(df_m5)
        mfi = self._mfi(df_m5)
        adx = self._adx(df_m5)
        obv = self._obv(df_m5)
        atr_pct = self._atr_pct(df_m5)
        cmf = self._cmf(df_m5)
        vwap = self._vwap(df_m5)
        supertrend = self._supertrend(df_m5)
        ichimoku = self._ichimoku(df_m5)
        fvg = self._fvg(df_m5, atr)
        engulfing = self._engulfing(df_m5)
        inside_bar = self._inside_bar(df_m5)
        close_streak = self._close_streak(df_m5)
        order_block = self._order_block(df_m5, atr)
        ha_trend = self._ha_trend(df_m5)
        orb = self._orb(df_m5, symbol)
        cvd_div = self._cvd_divergence(df_m5)
        macd_div = self._macd_divergence(df_m5)
        vp = self._session_volume_profile_state(df_m5, atr)
        adx_trend = self._adx_trend_state(df_m5)
        ote = self._ote_state(df_m5, atr)
        rsi_div = self._rsi_divergence(df_m5)
        breaker = self._breaker_block_state(df_m5, atr)

        feat = {
            "symbol": symbol,
            "price": round(price, 5),
            "m5_trend": m5_trend,
            "m15_trend": m15_trend,
            "bb_upper": round(bb["upper"], 5),
            "bb_middle": round(bb["middle"], 5),
            "bb_lower": round(bb["lower"], 5),
            "bb_position": round(bb["position"], 4),
            "bb_squeeze_pct": round(bb_squeeze_pct, 4),
            "rsi": round(rsi, 2),
            "macd": round(macd["macd"], 6),
            "macd_signal": round(macd["signal"], 6),
            "macd_hist": round(macd["hist"], 6),
            "macd_cross": macd["cross"],
            "cci": round(cci, 2),
            "mfi": round(mfi, 2),
            "adx": round(adx["adx"], 2),
            "di_plus": round(adx["di_plus"], 2),
            "di_minus": round(adx["di_minus"], 2),
            "obv": round(obv["obv"], 2),
            "obv_ema": round(obv["ema"], 2),
            "obv_cross": obv["cross"],
            "atr_pct": round(atr_pct, 4),
            "cmf": round(cmf, 4),
            "vwap": round(vwap["vwap"], 5),
            "vwap_upper": round(vwap["upper"], 5),
            "vwap_lower": round(vwap["lower"], 5),
            "vwap_position": round(vwap["position"], 4),
            "supertrend_dir": supertrend["dir"],
            "supertrend_flip": supertrend["flip"],
            "ichimoku_tenkan": round(ichimoku["tenkan"], 5),
            "ichimoku_kijun": round(ichimoku["kijun"], 5),
            "ichimoku_senkou_a": round(ichimoku["senkou_a"], 5),
            "ichimoku_senkou_b": round(ichimoku["senkou_b"], 5),
            "ichimoku_cloud_position": ichimoku["cloud_position"],
            "ichimoku_tk_cross": ichimoku["tk_cross"],
            "fvg": fvg["fvg"],
            "fvg_size_atr": round(fvg["size_atr"], 4),
            "engulfing": engulfing,
            "inside_bar": inside_bar,
            "close_streak": close_streak,
            "order_block": order_block,
            "ha_trend": ha_trend,
            "orb_signal": orb["orb_signal"],
            "or_high": orb["or_high"],
            "or_low": orb["or_low"],
            "cvd_divergence": cvd_div,
            "vp_poc_rejection": vp["poc_rejection"],
            "vp_va_breakout": vp["va_breakout"],
            "macd_divergence": macd_div,
            "adx_trend": adx_trend,
            "ote_state": ote,
            "rsi_divergence": rsi_div,
            "breaker_block": breaker,
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
        return feat

    # --- 2026-08-04 research-setup state features (shadow-only trials) -----
    # These power the 4 registry setups that were research-only in scripts/
    # (cvd_divergence_reversal, intermarket_divergence_zero_cross,
    # session_volume_profile_poc_rejection, session_volume_profile_va_breakout).
    # Like ORB, the FeatureEngine precomputes a per-bar STATE from full history
    # and the detect callables in core/specialized_setups.py read it — the
    # detectors get one bar's feat dict, not raw candles. The shadow ledger
    # (shadow=true) logs fires without placing orders; these are shadow trials.

    def _cvd_divergence(self, df: pd.DataFrame, pivot_l: int = 5) -> str | None:
        """CVD divergence reversal state at the CURRENT bar (research setup #1).

        Tick-rule delta = sign(close-open) * tick_volume (Lee-Ready proxy on
        Exness CFD tick volume), cumulative with a daily UTC anchor reset.
        Confirmed pivots (L=5) on price; fires on divergence:
        price higher high + CVD lower high -> SELL (bearish_divergence);
        price lower low + CVD higher low -> BUY (bullish_divergence).
        Lookahead-free: a pivot at bar i is only confirmed at bar i+L, and the
        divergence is only emitted at the confirmation bar — same as the OOS
        scorer in scripts/score_cvd_divergence.py.
        """
        n = len(df)
        if n < 2 * pivot_l + 30:
            return None
        opens = df["open"].to_numpy(dtype=float)
        closes = df["close"].to_numpy(dtype=float)
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        vols = df["volume"].to_numpy(dtype=float)
        times = pd.to_datetime(df["time"], utc=True)
        days = times.dt.strftime("%Y-%m-%d")

        delta = np.sign(closes - opens) * vols
        cvd = np.zeros(n)
        running = 0.0
        prev_day: str | None = None
        for i in range(n):
            day = days[i]
            if prev_day is not None and day != prev_day:
                running = 0.0
            running += float(delta[i])
            cvd[i] = running
            prev_day = day

        # Track the last confirmed pivot of each kind; fire only at the
        # confirmation bar (i+L == n-1) when price and CVD diverge.
        last_pivot_high: tuple[float, float] | None = None  # (price, cvd)
        last_pivot_low: tuple[float, float] | None = None
        for i in range(pivot_l, n - pivot_l):
            confirm = i + pivot_l
            left_h = highs[i - pivot_l:i]
            right_h = highs[i + 1:i + pivot_l + 1]
            left_l = lows[i - pivot_l:i]
            right_l = lows[i + 1:i + pivot_l + 1]
            is_ph = highs[i] > left_h.max() and highs[i] >= right_h.max()
            is_pl = lows[i] < left_l.min() and lows[i] <= right_l.min()
            if is_ph:
                if (
                    confirm == n - 1
                    and last_pivot_high is not None
                    and highs[i] > last_pivot_high[0]
                    and cvd[i] < last_pivot_high[1]
                ):
                    return "bearish_divergence"
                last_pivot_high = (float(highs[i]), float(cvd[i]))
            if is_pl:
                if (
                    confirm == n - 1
                    and last_pivot_low is not None
                    and lows[i] < last_pivot_low[0]
                    and cvd[i] > last_pivot_low[1]
                ):
                    return "bullish_divergence"
                last_pivot_low = (float(lows[i]), float(cvd[i]))
        return None

    def _macd_divergence(self, df: pd.DataFrame, pivot_l: int = 5,
                        fast: int = 12, slow: int = 26, signal: int = 9) -> str | None:
        """MACD-histogram divergence reversal state at the CURRENT bar (arsenal
        expansion iter-49, ``macd_hist_divergence`` setup).

        Standard MACD(12,26,9) histogram series; confirmed pivots (L=5) on price;
        fires on divergence at the confirmation bar (i+L == n-1):
          price higher high + hist lower high -> SELL (bearish_divergence);
          price lower low + hist higher low -> BUY (bullish_divergence).
        Lookahead-free (a pivot at bar i is only confirmed at i+L, divergence
        emitted only at the confirmation bar) — parity-identical to the
        vectorized replay pass in ``specialized_replay_labeler._vectorized_features``
        and structured like ``_cvd_divergence`` (CVD swapped for MACD-hist).
        Source: StratBase.ai MACD backtest — divergence is the best MACD variant
        (54% WR, PF 1.71) vs crossover (41% WR, PF 1.22).
        """
        n = len(df)
        if n < 2 * pivot_l + slow + signal:
            return None
        close = df["close"]
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        sig = macd_line.ewm(span=signal, adjust=False).mean()
        hist = (macd_line - sig).to_numpy(dtype=float)
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)

        last_ph: tuple[float, float] | None = None  # (price_high, hist_at_pivot)
        last_pl: tuple[float, float] | None = None
        for i in range(pivot_l, n - pivot_l):
            confirm = i + pivot_l
            left_h = highs[i - pivot_l:i]
            right_h = highs[i + 1:i + pivot_l + 1]
            left_l = lows[i - pivot_l:i]
            right_l = lows[i + 1:i + pivot_l + 1]
            is_ph = highs[i] > left_h.max() and highs[i] >= right_h.max()
            is_pl = lows[i] < left_l.min() and lows[i] <= right_l.min()
            if is_ph:
                if (
                    confirm == n - 1
                    and last_ph is not None
                    and highs[i] > last_ph[0]
                    and hist[i] < last_ph[1]
                ):
                    return "bearish_divergence"
                last_ph = (float(highs[i]), float(hist[i]))
            if is_pl:
                if (
                    confirm == n - 1
                    and last_pl is not None
                    and lows[i] < last_pl[0]
                    and hist[i] > last_pl[1]
                ):
                    return "bullish_divergence"
                last_pl = (float(lows[i]), float(hist[i]))
        return None

    def _rsi_divergence(self, df: pd.DataFrame, pivot_l: int = 5,
                       period: int = 14) -> str | None:
        """RSI divergence reversal state at the CURRENT bar (arsenal expansion
        iter-54, ``rsi_divergence`` setup).

        Wilder RSI(14) series; confirmed pivots (L=5) on price; fires on
        divergence at the confirmation bar (i+L == n-1):
          price higher high + RSI lower high -> SELL (bearish_divergence);
          price lower low + RSI higher low -> BUY (bullish_divergence).
        Structured IDENTICALLY to ``_macd_divergence`` (and ``_cvd_divergence``)
        with RSI swapped for MACD-hist / CVD — a third, mathematically-distinct
        oscillator divergence (RSI is a smoothed momentum-ratio; MACD-hist is an
        EMA-spread derivative; CVD is a cumulative signed-volume line). Pure
        divergence (no overbought/oversold extreme filter) matches the MACD-hist
        variant's structure. Lookahead-free (a pivot at bar i confirms at i+L,
        divergence emitted only at the confirmation bar) — parity-identical to
        the vectorized replay pass ``_vectorized_rsi_divergence``. Distinct from
        the session-gated ``london_rsi_reversion``/``ny_rsi_reversion`` (those
        fade RSI EXTREMES, momentum-continuation; this keys off RSI DIVERGENCE
        vs price, a reversal model). Source: RSI divergence is a staple
        TradingView reversal setup (StocksToTrade / Investopedia) and the most
        cited RSI variant; complements the MACD-hist + CVD divergence family.
        """
        n = len(df)
        if n < 2 * pivot_l + period + 10:
            return None
        close = df["close"]
        delta = close.diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)
        avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = (100.0 - 100.0 / (1.0 + rs)).to_numpy(dtype=float)
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)

        last_ph: tuple[float, float] | None = None  # (price_high, rsi_at_pivot)
        last_pl: tuple[float, float] | None = None
        for i in range(pivot_l, n - pivot_l):
            confirm = i + pivot_l
            left_h = highs[i - pivot_l:i]
            right_h = highs[i + 1:i + pivot_l + 1]
            left_l = lows[i - pivot_l:i]
            right_l = lows[i + 1:i + pivot_l + 1]
            is_ph = highs[i] > left_h.max() and highs[i] >= right_h.max()
            is_pl = lows[i] < left_l.min() and lows[i] <= right_l.min()
            if is_ph:
                if (
                    confirm == n - 1
                    and last_ph is not None
                    and highs[i] > last_ph[0]
                    and rsi[i] < last_ph[1]
                ):
                    return "bearish_divergence"
                last_ph = (float(highs[i]), float(rsi[i]))
            if is_pl:
                if (
                    confirm == n - 1
                    and last_pl is not None
                    and lows[i] < last_pl[0]
                    and rsi[i] > last_pl[1]
                ):
                    return "bullish_divergence"
                last_pl = (float(lows[i]), float(rsi[i]))
        return None

    def _breaker_block_state(self, df: pd.DataFrame, atr: float,
                             pivot_l: int = 5, max_age: int = 40,
                             zone_atr: float = 0.35) -> str | None:
        """ICT Breaker Block state at the CURRENT bar (arsenal expansion iter-53,
        ``ict_breaker_block`` setup).

        A breaker is a FAILED swing level that FLIPS polarity on retest:
          bullish_breaker: a confirmed pivot high H was BROKEN ABOVE (a close > H
            between the pivot and now), price retraced BACK DOWN to the H zone
            (low within zone_atr*ATR of H), and the current bar is a bullish
            rejection (close>open, close in the upper half of the bar's range) ->
            BUY (former resistance becomes support);
          bearish_breaker: a confirmed pivot low L was BROKEN BELOW (close < L),
            price retraced UP to the L zone (high within zone_atr*ATR of L), and
            the current bar is a bearish rejection (close<open, close in the lower
            half) -> SELL (former support becomes resistance).
        Searches confirmed pivots (L=5) within max_age (40 M5 = ~3.3h) of the
        current bar, most-recent first, and returns the first that satisfies
        break-then-retest-then-rejection. Lookahead-free (only confirmed pivots
        i+L <= k, and bars through the current bar). Mirrors the vectorized replay
        pass ``_vectorized_breaker_block_state`` so live/replay labels agree at
        every bar. Distinct from ``_order_block`` (the last opposite-color candle
        before a displacement, polarity-by-construction) and from
        ``london_order_block``/``ny_order_block``: this keys off a level that
        FAILED and flipped — a genuinely-new ICT dimension (the arsenal has order
        blocks but no breaker). Source: PineScriptForge ICT Breaker Block
        backtests (PF 1.54-1.82 across ES/NQ/CL/YM, 47-53% WR, Sharpe 1.76-2.50)
        + Backtrex (EUR/USD PF 1.62, NAS100 PF 1.74 with FVG confluence).
        HONESTY: OHLCV falsified per VERDICT.md — breadth, not deployable edge.
        """
        n = len(df)
        if n < 2 * pivot_l + 30 or not atr or atr <= 0:
            return None
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        closes = df["close"].to_numpy(dtype=float)
        opens = df["open"].to_numpy(dtype=float)
        k = n - 1
        zone = zone_atr * float(atr)
        latest_confirmable = k - pivot_l
        for i in range(latest_confirmable, pivot_l - 1, -1):
            if k - i > max_age:
                break
            left_h = highs[i - pivot_l:i]; right_h = highs[i + 1:i + pivot_l + 1]
            left_l = lows[i - pivot_l:i]; right_l = lows[i + 1:i + pivot_l + 1]
            is_ph = highs[i] > left_h.max() and highs[i] >= right_h.max()
            is_pl = lows[i] < left_l.min() and lows[i] <= right_l.min()
            if is_ph:
                H = float(highs[i])
                # broke above H strictly before the retest bar k
                broke = any(closes[b] > H for b in range(i + 1, k))
                if broke and (H - zone <= lows[k] <= H + zone):
                    rng = highs[k] - lows[k]
                    if closes[k] > opens[k] and rng > 0 and (closes[k] - lows[k]) >= 0.5 * rng:
                        return "bullish_breaker"
            if is_pl:
                L = float(lows[i])
                broke = any(closes[b] < L for b in range(i + 1, k))
                if broke and (L - zone <= highs[k] <= L + zone):
                    rng = highs[k] - lows[k]
                    if closes[k] < opens[k] and rng > 0 and (highs[k] - closes[k]) >= 0.5 * rng:
                        return "bearish_breaker"
        return None

    def _adx_trend_state(self, df: pd.DataFrame, period: int = 14,
                        min_adx: float = 20.0) -> str | None:
        """ADX/DMI rising-trend-continuation state at the CURRENT bar (arsenal
        expansion iter-50, ``adx_di_rising_trend`` setup).

        Wilder DMI(14) DI-crossover DIRECTION gated by trend-STRENGTH + a
        RISING-ADX filter. Fires at the current bar when all hold:
          ADX >= min_adx (default 20 — the lower threshold beat 25/30 across 2025
          backtests; waiting for "strong" trends misses the early, most profitable
          leg) AND ADX is RISING (adx[-1] > adx[-2] — the rising-slope filter was
          the key differentiator in PineScriptForge's NQ/DAX/ES sweep, PF 1.54-
          2.31, avoiding exhausted trends) AND DI direction agrees:
            DI+ > DI- -> "bullish_trend" (BUY continuation)
            DI- > DI+ -> "bearish_trend" (SELL continuation)
        Lookahead-free (uses only bars through the current bar). Wilder smoothing
        ewm(alpha=1/period, adjust=False) = RMA, identical to ``_adx`` and to the
        vectorized replay pass ``_vectorized_adx_trend_state`` so live/replay
        labels agree at every bar (the iter-13 parity lesson). Distinct from the
        session-gated ``london_adx_trend``/``ny_adx_trend`` (ADX>=25, no rising
        filter, 07:00-10:00/14:00-17:00 only): this is 24h-eligible, lower
        threshold, and adds the rising-slope gate.
        Source: Quant Signals 4236-trade ADX sweep (DI-crossover entries >
        ADX-as-filter in 83% of tests; threshold 20 > 25/30) + PineScriptForge
        DMI/ADX system (rising-ADX filter -> PF 1.54-2.31).
        """
        n = len(df)
        if n < period * 3:
            return None
        high = df["high"]; low = df["low"]; close = df["close"]
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
        minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        atr_w = tr.ewm(alpha=1.0 / period, adjust=False).mean()
        plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_w.replace(0, np.nan)
        minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_w.replace(0, np.nan)
        dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx_series = dx.ewm(alpha=1.0 / period, adjust=False).mean()
        a_cur = adx_series.iloc[-1]
        a_prev = adx_series.iloc[-2]
        if pd.isna(a_cur) or pd.isna(a_prev):
            return None
        if float(a_cur) < min_adx or float(a_cur) <= float(a_prev):
            return None  # trend too weak, or ADX not rising (exhausted/flat)
        dp = plus_di.iloc[-1]; dm = minus_di.iloc[-1]
        if pd.isna(dp) or pd.isna(dm):
            return None
        if float(dp) > float(dm):
            return "bullish_trend"
        if float(dm) > float(dp):
            return "bearish_trend"
        return None

    def _ote_state(self, df: pd.DataFrame, atr: float, pivot_l: int = 5,
                   max_leg_bars: int = 80, min_leg_atr: float = 1.5) -> str | None:
        """ICT Optimal Trade Entry state at the CURRENT bar (arsenal expansion
        iter-52, ``ict_ote`` setup).

        Identifies the most recent CONFIRMED displacement leg (a swing low -> swing
        high = up leg, or swing high -> swing low = down leg; pivots confirmed at
        i+L, L=5) and fires when the current close retraces into the 62-79% OTE
        Fibonacci zone of that leg (sweet spot 70.5%):
          up leg (low->high, high most recent): close in [H-0.79*(H-L), H-0.62*(H-L)]
            -> "bullish_ote" (buy the pullback in an uptrend);
          down leg (high->low, low most recent): close in [L+0.62*(H-L), L+0.79*(H-L)]
            -> "bearish_ote" (sell the rally in a downtrend).
        Guards: leg >= min_leg_atr*ATR (default 1.5 ATR — a real displacement, not
        noise) and the leg end pivot is within max_leg_bars (80 M5 = ~6.7h) of the
        current bar (stale legs don't qualify). Lookahead-free (only confirmed
        pivots + the current close). Mirrors the vectorized replay pass
        ``_vectorized_ote_state`` so live/replay labels agree at every bar.
        Distinct from every prior setup: NO existing model uses Fibonacci
        retracement-of-an-impulse — the 50+ entry models use direction / reversion
        / volume / trend-strength, none measure a measured-move retracement.
        Source: PineScriptForge ICT OTE backtests (PF 2.30 gold, 2.22 RTY, 2.63
        silver, Sharpe ~2.5) + ictkillzone.com (71% fill rate, 68% WR to T1 at the
        70.5% level on 160 NQ entries). HONESTY: OHLCV falsified per VERDICT.md —
        breadth, not deployable edge.
        """
        n = len(df)
        if n < 2 * pivot_l + 30 or not atr or atr <= 0:
            return None
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        closes = df["close"].to_numpy(dtype=float)
        min_leg = min_leg_atr * float(atr)
        last_ph: tuple[int, float] | None = None  # (idx, price)
        last_pl: tuple[int, float] | None = None
        for i in range(pivot_l, n - pivot_l):
            left_h = highs[i - pivot_l:i]; right_h = highs[i + 1:i + pivot_l + 1]
            left_l = lows[i - pivot_l:i]; right_l = lows[i + 1:i + pivot_l + 1]
            if highs[i] > left_h.max() and highs[i] >= right_h.max():
                last_ph = (i, float(highs[i]))
            if lows[i] < left_l.min() and lows[i] <= right_l.min():
                last_pl = (i, float(lows[i]))
        if last_ph is None or last_pl is None:
            return None
        cur = float(closes[-1])
        if last_ph[0] > last_pl[0]:
            # up leg: low (start) -> high (end, most recent); retracement DOWN
            low = last_pl[1]; high = last_ph[1]; end_idx = last_ph[0]
            if high - low < min_leg:
                return None
            if (n - 1) - end_idx > max_leg_bars:
                return None
            zone_lo = high - 0.79 * (high - low); zone_hi = high - 0.62 * (high - low)
            if zone_lo <= cur <= zone_hi:
                return "bullish_ote"
            return None
        # down leg: high (start) -> low (end, most recent); retracement UP
        high = last_ph[1]; low = last_pl[1]; end_idx = last_pl[0]
        if high - low < min_leg:
            return None
        if (n - 1) - end_idx > max_leg_bars:
            return None
        zone_lo = low + 0.62 * (high - low); zone_hi = low + 0.79 * (high - low)
        if zone_lo <= cur <= zone_hi:
            return "bearish_ote"
        return None

    def _session_volume_profile_state(
        self, df: pd.DataFrame, atr: float, profile_bars: int = 96, n_bins: int = 50,
    ) -> dict[str, str | None]:
        """Session volume profile POC-rejection + VA-breakout state (setups #3/#4).

        Builds a rolling tick-volume profile over the prior ``profile_bars``
        (96 x M5 = 8h session). POC = price bin with max tick volume; VAH/VAL =
        70%% value area around POC. Returns two states for the current bar:
          * poc_rejection: wick touched POC within 0.5*ATR and closed back on
            the away side -> "bullish" (close > POC) / "bearish" (close < POC)
          * va_breakout: close broke VAH with volume_ratio >= 1.2 -> "bullish";
            close broke VAL with volume_ratio >= 1.2 -> "bearish"
        Lookahead-free (window ends at the current bar). Mirrors
        scripts/score_volume_profile_setup.py + score_volume_profile_vabreakout.py.
        """
        n = len(df)
        if n < 30 or atr is None or atr <= 0:
            return {"poc_rejection": None, "va_breakout": None}
        lo = max(0, n - profile_bars)
        window = df.iloc[lo:n]
        if len(window) < 20:
            return {"poc_rejection": None, "va_breakout": None}
        price_min = float(window["low"].min())
        price_max = float(window["high"].max())
        if price_max <= price_min:
            return {"poc_rejection": None, "va_breakout": None}
        bin_w = (price_max - price_min) / n_bins
        vol = np.zeros(n_bins)
        tp = (window["high"] + window["low"] + window["close"]) / 3.0
        idx = ((tp - price_min) / bin_w).astype(int)
        idx = np.clip(idx, 0, n_bins - 1)
        np.add.at(vol, idx, window["volume"].to_numpy(dtype=float))
        total = vol.sum()
        if total <= 0:
            return {"poc_rejection": None, "va_breakout": None}
        poc_idx = int(np.argmax(vol))
        poc = price_min + (poc_idx + 0.5) * bin_w
        target = 0.70 * total
        cum = vol[poc_idx]
        lo_i, hi_i = poc_idx, poc_idx
        while cum < target and (lo_i > 0 or hi_i < n_bins - 1):
            if hi_i + 1 < n_bins and (lo_i == 0 or vol[hi_i + 1] >= vol[lo_i - 1]):
                hi_i += 1
            elif lo_i > 0:
                lo_i -= 1
            cum = vol[lo_i:hi_i + 1].sum()
        vah = price_min + (hi_i + 1) * bin_w
        val = price_min + lo_i * bin_w

        cur = window.iloc[-1]
        o = float(cur["open"]); h = float(cur["high"]); l = float(cur["low"]); c = float(cur["close"])
        rng = max(h - l, 1e-9)
        body = c - o
        tol = 0.5 * atr
        poc_rejection: str | None = None
        wick_touched = (l <= poc + tol) and (h >= poc - tol)
        if wick_touched:
            if c > poc and body > 0 and (c - l) / rng > 0.5:
                poc_rejection = "bullish"
            elif c < poc and body < 0 and (h - c) / rng > 0.5:
                poc_rejection = "bearish"

        va_breakout: str | None = None
        vol_avg = float(df["volume"].tail(20).mean())
        vr = float(df["volume"].iloc[-1] / vol_avg) if vol_avg > 0 else 1.0
        if vr >= 1.2:
            if c > vah:
                va_breakout = "bullish"
            elif c < val:
                va_breakout = "bearish"
        return {"poc_rejection": poc_rejection, "va_breakout": va_breakout}

    # --- Intermarket (cross-symbol) state -----------------------------------

    INTERMARKET_PAIRS: tuple[tuple[str, str], ...] = (
        ("US30m", "NAS100m"), ("NAS100m", "US30m"),
        ("US500m", "US30m"), ("EURUSDm", "GBPUSDm"), ("GBPUSDm", "EURUSDm"),
        ("UK100m", "FR40m"), ("FR40m", "UK100m"),
        ("USOILm", "XAUUSDm"), ("BTCUSDm", "NAS100m"),
    )

    def _inject_intermarket(
        self, feature_symbols: dict[str, dict[str, Any]], prepared_m5: dict[str, list],
    ) -> None:
        """Cross-symbol intermarket zero-cross state (research setup #2).

        For each positively-correlated (target, anchor) pair, EMA-smooth both
        closes, z-score each over a 50-bar window, spread d = zA - zB; stamp
        ``intermarket_zero_cross`` (bullish_cross -> BUY target / bearish_cross
        -> SELL target) onto the TARGET's feature dict when d crossed zero at
        the current bar. Fault-isolated: any pair failure leaves the feature
        unset (no fire). Mirrors scripts/score_intermarket_divergence.py.
        """
        ema_p, win = 20, 50
        # Build each symbol's {5-min-bucket: close} map ONCE (each symbol
        # participates in up to 3 pairs) instead of re-bucketing per pair.
        bucket_maps: dict[str, dict[int, float]] = {}
        for sym, bars in prepared_m5.items():
            m: dict[int, float] = {}
            try:
                times = pd.Series(pd.to_datetime(
                    [b.get("time") for b in bars], utc=True,
                ))
                closes = np.array([float(b["close"]) for b in bars])
                buckets = (
                    times.dt.tz_convert("UTC").dt.tz_localize(None)
                    .astype("datetime64[ns]").to_numpy().view("int64")
                    // (300 * 10**9)
                )
                # Last bar wins for a bucket (keep newest close per 5-min slot).
                for k in range(len(buckets)):
                    m[int(buckets[k])] = float(closes[k])
            except Exception:  # noqa: BLE001 — never break the feature pipeline
                m = {}
            bucket_maps[sym] = m
        for target, anchor in self.INTERMARKET_PAIRS:
            if target not in feature_symbols or anchor not in bucket_maps:
                continue
            try:
                state = self._intermarket_zero_cross(
                    prepared_m5[target], bucket_maps[anchor], ema_p, win,
                )
            except Exception:  # noqa: BLE001 — never break the feature pipeline
                state = None
            if state:
                feature_symbols[target]["intermarket_zero_cross"] = state
                feature_symbols[target]["intermarket_anchor"] = anchor

    @staticmethod
    def _intermarket_zero_cross(
        t_bars: list[dict[str, Any]], anchor_buckets: dict[int, float] | list[dict[str, Any]],
        ema_p: int = 20, win: int = 50,
    ) -> str | None:
        """Zero-crossing spread state at the current bar for a (target, anchor) pair.

        ``anchor_buckets`` is the pre-built {5-min-bucket: close} map from
        ``_inject_intermarket`` (callers may also pass raw bars for tests —
        bucketed here in that case).
        """
        sec: dict[int, float]
        if isinstance(anchor_buckets, dict):
            sec = anchor_buckets
        else:
            sec = {}
            for b in anchor_buckets:
                t = pd.to_datetime(b.get("time"), utc=True)
                sec[int(t.timestamp() // 300)] = float(b["close"])
        times_p = pd.Series(pd.to_datetime([b.get("time") for b in t_bars], utc=True))
        # Normalize to nanoseconds FIRST: a list-built Series is datetime64[us],
        # and .view("int64") on that dtype would misread microseconds as
        # nanoseconds -> buckets off by 1000x -> every mask fails. astype to ns
        # guarantees a uniform epoch-ns base regardless of input precision.
        buckets = (
            times_p.dt.tz_convert("UTC").dt.tz_localize(None)
            .astype("datetime64[ns]").to_numpy().view("int64") // (300 * 10**9)
        )
        closes_all = np.array([float(b["close"]) for b in t_bars])
        mask = np.array([int(bk) in sec for bk in buckets])
        if mask.sum() < win + ema_p + 4:
            return None
        # Stale-anchor guard: the CURRENT target bar must have an aligned anchor
        # close. If the anchor lags the target by even one 5-min bucket, the
        # masked tail drops real bars and d_now would describe an OLDER bar — a
        # one-bar-stale zero-cross. Refuse rather than fire stale.
        if not mask[-1]:
            return None
        t_p = closes_all[mask]
        a_p = np.array([sec[int(bk)] for bk in buckets[mask]], dtype=float)

        def _ema(x: np.ndarray, p: int) -> np.ndarray:
            if len(x) < p:
                return np.full(len(x), np.nan)
            s = pd.Series(x)
            return s.ewm(span=p, adjust=False).mean().to_numpy()

        def _zscore(x: np.ndarray, w: int) -> np.ndarray:
            s = pd.Series(x)
            mean = s.rolling(w).mean()
            sd = s.rolling(w).std(ddof=0)
            out = ((s - mean) / sd.replace(0, np.nan)).to_numpy()
            # Any NaN z becomes 0.0 (flat). Two sources: warmup (bars < w, no
            # window yet) and zero-std stretches mid-series (a genuinely flat
            # segment where mean == value -> z of 0 is correct there too).
            return np.where(np.isnan(out), 0.0, out)

        t_ema = _ema(t_p, ema_p)
        a_ema = _ema(a_p, ema_p)
        t_z = _zscore(t_ema, win)
        a_z = _zscore(a_ema, win)
        d_now = t_z[-1] - a_z[-1]
        d_prev = t_z[-2] - a_z[-2]
        if np.isnan(d_now) or np.isnan(d_prev):
            return None
        if d_prev < 0.0 <= d_now:
            return "bullish_cross"
        if d_prev > 0.0 >= d_now:
            return "bearish_cross"
        return None

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

    def _bb_squeeze(self, df: pd.DataFrame, period: int = 20, std_mult: float = 2.0,
                    lookback: int = 50) -> float:
        """Bollinger-bandwidth compression percentile — iteration 19.

        Returns the percentile rank of the CURRENT BB bandwidth within the prior
        ``lookback`` bars, in [0, 1]: 0.0 = tightest squeeze (most compressed),
        1.0 = widest. A low value (e.g. <= 0.20) means volatility is compressed
        relative to recent history — the "squeeze" state that precedes expansion.
        This is the rolling-compression baseline the specialized squeeze-breakout
        entry model keys off (the existing ``volatility_regime`` "low" gate is an
        ABSOLUTE threshold, far too rare to fire ~0.06% of bars; this RELATIVE
        percentile is per-symbol-adaptive and fires often enough to measure).

        Computed identically in the vectorized replay
        (``specialized_replay_labeler._vectorized_features``) so live/research
        parity holds — the iter-13 dead-setup lesson. bw = (upper-lower)/middle;
        pct = bw.rolling(lookback).rank(pct=True) (current value ranked within the
        window including itself). Returns 0.5 (neutral) during warmup.
        """
        close = df["close"]
        mid = close.rolling(period).mean()
        std = close.rolling(period).std()
        upper = mid + std_mult * std
        lower = mid - std_mult * std
        bw = (upper - lower) / mid.replace(0, np.nan)
        pct = bw.rolling(lookback).rank(pct=True)
        val = pct.iloc[-1]
        return float(val) if pd.notna(val) else 0.5

    def _rsi(self, df: pd.DataFrame, period: int = 14) -> float:
        """Wilder's Relative Strength Index — iteration 21.

        Returns the RSI(14) of the current bar, in [0, 100]. A genuinely-new
        feature: no prior setup used RSI (stoch_k is a price-RANGE position
        oscillator; RSI is a smoothed momentum-ratio oscillator — mathematically
        distinct, and one of the most-used TradingView oscillators). Keys the
        RSI-reversion entry model (oversold/overbought fade), a clean test of
        whether a different oscillator avoids the "stoch stays overbought in a
        trend" trap that made stoch_reversion (iter 16) a broad per-symbol loser.

        Wilder's smoothing = ewm(alpha=1/period, adjust=False). Computed
        identically in the vectorized replay
        (``specialized_replay_labeler._vectorized_features``) so live/research
        parity holds (the iter-13 dead-setup lesson). Returns 50 (neutral) during
        warmup / zero-range edge cases.
        """
        close = df["close"]
        delta = close.diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)
        avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi_series = 100.0 - 100.0 / (1.0 + rs)
        val = rsi_series.iloc[-1]
        return float(val) if pd.notna(val) else 50.0

    def _macd(self, df: pd.DataFrame, fast: int = 12, slow: int = 26,
              signal: int = 9) -> dict:
        """Moving Average Convergence Divergence — iteration 22.

        Returns the MACD line (EMA fast - EMA slow), its signal line (EMA of the
        MACD line), the histogram (MACD - signal), and the signal-line cross
        event this bar (bullish_cross / bearish_cross / none). A genuinely-new
        feature: no prior setup used MACD. MACD is a SMOOTHED-MOMENTUM oscillator
        (EMA12-EMA26, the difference of two EMAs) — mathematically distinct from
        stoch_k (price-RANGE position oscillator), from the EMA20-slope m5_trend
        (single-EMA slope, not a fast/slow EMA difference), and from RSI
        (momentum-ratio). Keys the MACD-cross entry model (signal-line
        momentum-continuation), a clean test of whether a different oscillator's
        cross works per symbol — stoch_cross (iter 12) was the prior cross-event
        model, on a different oscillator.

        Standard TradingView MACD(12,26,9). Computed identically in the
        vectorized replay (``specialized_replay_labeler._vectorized_features``)
        so live/research parity holds (the iter-13 dead-setup lesson). Returns
        0 / "none" during warmup.
        """
        close = df["close"]
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        hist = macd_line - signal_line

        m = float(macd_line.iloc[-1]) if pd.notna(macd_line.iloc[-1]) else 0.0
        s = float(signal_line.iloc[-1]) if pd.notna(signal_line.iloc[-1]) else 0.0
        h = float(hist.iloc[-1]) if pd.notna(hist.iloc[-1]) else 0.0
        m_prev = float(macd_line.iloc[-2]) if len(macd_line) > 1 and pd.notna(macd_line.iloc[-2]) else m
        s_prev = float(signal_line.iloc[-2]) if len(signal_line) > 1 and pd.notna(signal_line.iloc[-2]) else s

        cross = "none"
        if m_prev <= s_prev and m > s:
            cross = "bullish_cross"
        elif m_prev >= s_prev and m < s:
            cross = "bearish_cross"
        return {"macd": m, "signal": s, "hist": h, "cross": cross}

    def _cci(self, df: pd.DataFrame, period: int = 20) -> float:
        """Commodity Channel Index — iteration 23.

        Returns the CCI(20) of the current bar. CCI = (TP - SMA(TP, period)) /
        (0.015 * mean_deviation(TP, period)), where TP = (high+low+close)/3 and
        mean_deviation = mean of |TP_i - SMA| over the window. A genuinely-new
        feature: no prior setup used CCI. CCI is a PRICE-DEVIATION-FROM-MA
        oscillator (how far price sits from its own SMA, in units of mean
        deviation) — mathematically distinct from RSI (gain/loss momentum
        ratio, iter 21), stoch_k (price-RANGE position), and MACD (EMA
        difference, iter 22). Keys the CCI-reversion entry model (|CCI| >= 100
        fade), a third oscillator-reversion cell — a clean per-symbol test of
        whether a price-deviation oscillator's extreme-fade works where RSI
        (iter 21) and stoch (iter 16) fades didn't.

        Standard TradingView CCI(20, 0.015). Computed identically in the
        vectorized replay (``specialized_replay_labeler._vectorized_features``)
        so live/research parity holds (the iter-13 dead-setup lesson). Returns
        0 (neutral) during warmup / zero-deviation edge cases.
        """
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        sma = tp.rolling(period).mean()
        md = tp.rolling(period).apply(
            lambda x: np.abs(x - x.mean()).mean(), raw=True
        )
        cci_series = (tp - sma) / (0.015 * md.replace(0, np.nan))
        val = cci_series.iloc[-1]
        return float(val) if pd.notna(val) else 0.0

    def _mfi(self, df: pd.DataFrame, period: int = 14) -> float:
        """Money Flow Index — iteration 24.

        Returns the MFI(14) of the current bar, in [0, 100]. MFI is a
        VOLUME-WEIGHTED oscillator (the volume-weighted cousin of RSI): TP =
        (high+low+close)/3, raw money flow = TP * volume, positive flow is the
        sum of raw flows on bars where TP rose, negative flow where TP fell, and
        MFI = 100 - 100/(1 + pos_flow/neg_flow) over the window. A genuinely-new
        feature: no prior setup used MFI. It is mathematically DISTINCT from RSI
        (iter 21, a pure price gain/loss ratio with NO volume), CCI (iter 23,
        price-deviation-from-MA, no volume), stoch_k (price-range position, no
        volume), and MACD (iter 22, EMA difference, no volume) — MFI is the ONLY
        oscillator here that folds in volume via the money-flow ratio. Keys the
        MFI-reversion entry model (oversold/overbought fade), a fifth
        oscillator-reversion cell — a clean per-symbol test of whether a
        volume-weighted fade distinguishes symbols (volume-informative crypto /
        oil / indices) where the pure-price RSI/CCI/stoch fades did not.

        Standard TradingView MFI(14) (20/80 oversold/overbought). Computed
        identically in the vectorized replay
        (``specialized_replay_labeler._vectorized_features``) so live/research
        parity holds (the iter-13 dead-setup lesson). Uses a rolling SUM (not an
        EMA) so values are exact windowed matches after 14 bars, not just
        converged — parity is byte-tight past warmup. Returns 50 (neutral) during
        warmup / zero-negative-flow edge cases.
        """
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        raw_mf = tp * df["volume"]
        tp_prev = tp.shift(1)
        pos_flow = pd.Series(np.where(tp > tp_prev, raw_mf, 0.0), index=df.index)
        neg_flow = pd.Series(np.where(tp < tp_prev, raw_mf, 0.0), index=df.index)
        pos_sum = pos_flow.rolling(period).sum()
        neg_sum = neg_flow.rolling(period).sum()
        mfr = pos_sum / neg_sum.replace(0, np.nan)
        mfi_series = 100.0 - 100.0 / (1.0 + mfr)
        val = mfi_series.iloc[-1]
        return float(val) if pd.notna(val) else 50.0

    def _adx(self, df: pd.DataFrame, period: int = 14) -> dict:
        """Wilder's Average Directional Index (DMI) — iteration 25.

        Returns the ADX(14) trend-STRENGTH line and the +DI/-DI directional
        indicators of the current bar. ADX = the Wilder-smoothed average of
        DX = 100 * |+DI - -DI| / (+DI + -DI), where +DI/-DI = 100 * Wilder-smoothed
        (+DM / -DM) / Wilder-smoothed TR. +DM = up-move when up>down & up>0; -DM =
        down-move when down>up & down>0; TR = max(H-L, |H-prevC|, |L-prevC|).
        Wilder smoothing = ewm(alpha=1/period, adjust=False) = RMA (matches
        TradingView's RMA, the standard DMI/ADX). ADX is in [0, 100]: >= 25 is a
        strong trend (classic threshold), regardless of direction; +DI/-DI give
        the direction.

        A genuinely-new FEATURE and a genuinely-new DIMENSION: ADX is a
        TREND-STRENGTH oscillator (non-directional — it reads how strong a trend
        is, not which way). NO prior setup measures strength — the 15 prior
        entry models use DIRECTION (breakout/cross/trend/m15_trend), REVERSION
        (oscillator extremes: bb/stoch/rsi/cci/mfi), or VOLUME (volume_spike/
        volume_breakout). ADX is mathematically distinct from all of them (DI
        smoothing of directional moves, normalized to a strength index). Keys
        the ADX-trend continuation entry model (strong-trend + DI direction),
        a clean per-symbol test of whether a TRENGTH-STRENGTH filter works where
        the directional trend models (mtf_align iter15, htf_breakout iter18,
        vol_expansion iter13) split without a clear edge.

        Computed identically in the vectorized replay
        (``specialized_replay_labeler._vectorized_features``) so live/research
        parity holds (the iter-13 dead-setup lesson). Wilder ewm(alpha=1/14)
        converges in ~60 bars. Returns 0 / 0 / 0 during warmup / zero-TR edge
        cases.
        """
        high = df["high"]
        low = df["low"]
        close = df["close"]
        up_move = high.diff()
        down_move = -low.diff()  # low_prev - low
        plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
        minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        atr_w = tr.ewm(alpha=1.0 / period, adjust=False).mean()  # Wilder-smoothed TR
        plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_w.replace(0, np.nan)
        minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_w.replace(0, np.nan)
        dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx_series = dx.ewm(alpha=1.0 / period, adjust=False).mean()

        a = float(adx_series.iloc[-1]) if pd.notna(adx_series.iloc[-1]) else 0.0
        p = float(plus_di.iloc[-1]) if pd.notna(plus_di.iloc[-1]) else 0.0
        m = float(minus_di.iloc[-1]) if pd.notna(minus_di.iloc[-1]) else 0.0
        return {"adx": a, "di_plus": p, "di_minus": m}

    def _obv(self, df: pd.DataFrame, ema_period: int = 20) -> dict:
        """On-Balance Volume — iteration 26.

        OBV is a CUMULATIVE signed-volume line: each bar adds +volume if close
        rose, -volume if close fell, 0 if unchanged. A genuinely-new VOLUME
        dimension: no prior setup uses cumulative volume. ``volume_ratio``
        (iter 14/20) is a single-bar volume spike / volume+breakout compound;
        ``mfi`` (iter 24) is a windowed pos/neg money-flow RATIO (bounded
        oscillator). OBV is the volume-ACCUMULATION trend line — the running
        total of signed volume — so its DIRECTION (vs its own EMA) measures
        whether buyers or sellers are accumulating across many bars, not one
        bar. Keys the OBV-EMA-cross entry model (volume-accumulation momentum
        continuation), a clean test of whether cumulative-volume direction
        works per symbol — the prior volume models (volume_spike/volume_breakout
        = single-bar spike, mfi = windowed ratio) didn't.

        Standard TradingView On Balance Volume + OBV EMA(20) cross. OBV is
        path-dependent (cumulative from the first bar), so the absolute LEVEL
        differs between live (full history) and the replay cap (max_bars tail).
        The setup keys off the CROSS EVENT (obv > obv_ema flipping), which after
        EMA warmup (~ema_period*5 bars) is independent of the starting level
        (a constant offset shifts both OBV and its EMA equally), so live/research
        SIGNAL parity holds even though the levels differ. Computed identically
        in the vectorized replay (``specialized_replay_labeler._vectorized_features``).
        Returns 0 / "none" during warmup.
        """
        close = df["close"]
        volume = df["volume"]
        # sign of close change: +1 up, -1 down, 0 unchanged (np.sign(0)=0)
        direction = np.sign(close.diff().fillna(0.0))
        obv_series = (direction * volume).cumsum()
        ema_series = obv_series.ewm(span=ema_period, adjust=False).mean()

        o = float(obv_series.iloc[-1]) if pd.notna(obv_series.iloc[-1]) else 0.0
        e = float(ema_series.iloc[-1]) if pd.notna(ema_series.iloc[-1]) else 0.0
        o_prev = float(obv_series.iloc[-2]) if len(obv_series) > 1 and pd.notna(obv_series.iloc[-2]) else o
        e_prev = float(ema_series.iloc[-2]) if len(ema_series) > 1 and pd.notna(ema_series.iloc[-2]) else e

        cross = "none"
        if o_prev <= e_prev and o > e:
            cross = "bullish_cross"
        elif o_prev >= e_prev and o < e:
            cross = "bearish_cross"
        return {"obv": o, "ema": e, "cross": cross}

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

    def _atr_pct(self, df: pd.DataFrame, atr_period: int = 14,
                 lookback: int = 100) -> float:
        """ATR percentile rank — iteration 27.

        The percentile rank (0..1) of the current ATR(14) within its own
        rolling ``lookback``-bar ATR history — a non-parametric RELATIVE
        volatility measure. A genuinely-new VOLATILITY-RANK dimension: no
        prior setup uses it. ``volatility_regime`` / vol_expansion (iter 5/13)
        gate on ABSOLUTE ``atr_ratio`` (ATR/price) thresholded into
        high/normal/low — a spot, cross-symbol-normalized level. ATR-percentile
        instead asks where current vol sits within its OWN recent history
        (rank), capturing vol EXPANSION/CONTRACTION dynamics that a spot
        atr_ratio does not: a symbol whose vol is perpetually high in absolute
        terms sits forever in ``volatility_regime=high`` but only registers on
        atr_pct when it is high RELATIVE to its own recent history. Mathematically
        distinct (a rolling rank, not a thresholded ratio). Keys the
        ATR-percentile-breakout entry model (relative-vol-expansion momentum
        continuation), a clean test of whether a relative-vol gate works per
        symbol where the absolute-vol gate (vol_expansion) didn't.

        Windowed (rolling rank, NOT cumulative) so live/research parity is
        byte-tight: rolling(lookback).rank(pct=True) at the last bar depends
        only on the last ``lookback`` ATR values, identical between live
        (full history tail) and the replay cap (max_bars tail) once both have
        >= lookback+atr_period bars. Computed identically in the vectorized
        replay (``specialized_replay_labeler._vectorized_features``). Returns
        0.5 (mid rank) during warmup.
        """
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        atr_series = tr.rolling(atr_period).mean()
        pct = atr_series.rolling(lookback).rank(pct=True)
        val = pct.iloc[-1]
        return float(val) if pd.notna(val) else 0.5

    def _cmf(self, df: pd.DataFrame, period: int = 20) -> float:
        """Chaikin Money Flow — iteration 28.

        CMF(period) = sum(money_flow_multiplier * volume, period) /
                      sum(volume, period), where the money-flow multiplier is
        (2*close - high - low) / (high - low) = the close LOCATION within the
        bar's range, in [-1, +1]. A genuinely-new VOLUME dimension: no prior
        setup keys off intrabar close-location weighted by volume. The three
        existing volume features each measure something different —
        ``volume_ratio`` (iter 14/20) is a single-bar volume LEVEL vs its
        average; ``mfi`` (iter 24) is a price-CHANGE-weighted money-flow ratio
        (uses close-to-close direction, reversion); ``obv`` (iter 26) is the
        CUMULATIVE signed-DIRECTION volume line (cross). CMF instead asks where
        price closed WITHIN each bar's range, weighted by volume, summed and
        normalized — accumulation/distribution PRESSURE as a bounded [-1, +1]
        oscillator. A close near the bar high on high volume => strong buying
        pressure (+multiplier * big volume); a close near the low on high
        volume => distribution. Captures intrabar flow that close-to-close
        (MFI/OBV) and level-only (volume_ratio) both miss. Keys the
        CMF-confirmed-continuation entry model (accumulation + bullish M5 trend
        => BUY / distribution + bearish trend => SELL), a clean test of whether
        intrabar money-flow pressure works per symbol where the other three
        volume dimensions didn't.

        Standard TradingView Chaikin Money Flow. Windowed (rolling sum, NOT
        cumulative) so live/research parity is byte-tight: the last-bar value
        depends only on the last ``period`` bars, identical between live (full
        history tail) and the replay cap (max_bars tail) once both have >=
        period bars. Computed identically in the vectorized replay
        (``specialized_replay_labeler._vectorized_features``). Returns 0
        (neutral flow) during warmup or when all bars in the window have zero
        range.
        """
        high = df["high"]
        low = df["low"]
        close = df["close"]
        volume = df["volume"]
        rng = high - low
        # money-flow multiplier in [-1, +1]; zero range => 0 pressure
        mfm = ((2 * close - high - low) / rng.replace(0, np.nan)).fillna(0.0)
        mfv = mfm * volume
        vol_sum = volume.rolling(period).sum()
        cmf_series = mfv.rolling(period).sum() / vol_sum.replace(0, np.nan)
        val = cmf_series.iloc[-1]
        return float(val) if pd.notna(val) else 0.0

    def _vwap(self, df: pd.DataFrame, period: int = 20, std_mult: float = 1.5) -> dict:
        """Rolling Volume-Weighted Average Price + bands — iteration 34.

        VWAP(period) = sum(typical_price * volume, period) / sum(volume, period),
        where typical_price = (high + low + close) / 3. Bands at
        vwap +/- std_mult * rolling(period).std(typical_price). vwap_position is
        the close's location within the bands in [0, 1] (0 = at/below lower band,
        1 = at/above upper band), clipped — the same shape as bb_position but on a
        VOLUME-WEIGHTED anchor instead of a price-only SMA.

        A genuinely-new PRICE-ANCHOR dimension: no prior setup keys off a
        volume-weighted price level. The four existing volume features measure
        FLOW/LEVEL (volume_ratio = single-bar level, mfi = price-change ratio,
        obv = cumulative signed line, cmf = intrabar close-location pressure);
        VWAP instead anchors a PRICE LEVEL by volume — where the "fair" weighted
        price sits and how far spot has extended from it. Distinct from
        bb_position (Bollinger bands on a plain SMA of close) because the anchor
        and bands both fold in volume: a high-volume bar pulls VWAP toward it,
        so the bands adapt to volume distribution, not just price dispersion.

        Windowed (rolling sum, NOT session-cumulative) so live/research parity is
        byte-tight: the last-bar value depends only on the last ``period`` bars,
        identical between live (full history tail) and the replay cap (max_bars
        tail) once both have >= period bars. (Session-anchored cumulative VWAP —
        the textbook TradingView definition — is path-dependent on the session
        boundary and would diverge between the live window and the replay tail;
        the rolling form is the parity-honest choice and is still a legitimate
        volume-weighted average price.) Computed identically in the vectorized
        replay (``specialized_replay_labeler._vectorized_features``). Returns
        vwap=price, position=0.5 (mid) during warmup or zero-volume windows.
        """
        high = df["high"]
        low = df["low"]
        close = df["close"]
        volume = df["volume"]
        tp = (high + low + close) / 3.0
        tp_vol = tp * volume
        vol_sum = volume.rolling(period).sum()
        vwap_series = tp_vol.rolling(period).sum() / vol_sum.replace(0, np.nan)
        std_series = tp.rolling(period).std()
        upper = vwap_series + std_mult * std_series
        lower = vwap_series - std_mult * std_series
        width = upper - lower
        pos = ((close - lower) / width.replace(0, np.nan)).clip(0.0, 1.0).fillna(0.5)
        v = vwap_series.iloc[-1]
        u = upper.iloc[-1]
        l = lower.iloc[-1]
        price = float(close.iloc[-1])
        return {
            "vwap": float(v) if pd.notna(v) else price,
            "upper": float(u) if pd.notna(u) else price,
            "lower": float(l) if pd.notna(l) else price,
            "position": float(pos.iloc[-1]) if pd.notna(pos.iloc[-1]) else 0.5,
        }

    def _supertrend(self, df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> dict:
        """Supertrend trend-STATE indicator + flip event — iteration 35.

        Classic TradingView Supertrend: an ATR-band trend overlay whose band
        collapses toward price on reversals and holds on continuations, with an
        explicit up/down direction that flips when close crosses the trailing
        band. Formula (TradingView default period=10, mult=3.0):
          hl2 = (high + low) / 2;  atr = ATR(period)
          basic_upper = hl2 + mult*atr;  basic_lower = hl2 - mult*atr
          final_upper[i] = basic_upper[i]  if  basic_upper[i] < final_upper[i-1]
                                              OR close[i-1] > final_upper[i-1]
                           else final_upper[i-1]   (holds — band only drops in
           an uptrend, so it ratchets to track the rising structure)
          final_lower[i] = basic_lower[i]  if  basic_lower[i] > final_lower[i-1]
                                              OR close[i-1] < final_lower[i-1]
                           else final_lower[i-1]   (ratchets up in a downtrend)
          direction[i] = UP   if close[i] > final_upper[i-1]  (close above the
                                                              trailing upper band)
                       = DOWN if close[i] < final_lower[i-1]
                       = direction[i-1] otherwise                 (hold)

        supertrend_flip = bullish_flip (DOWN->UP) / bearish_flip (UP->DOWN) /
        none. The band LEVEL is recursive (carry-forward of final_upper/lower),
        so it is PATH-DEPENDENT: live (500-bar tail) and replay (full-history
        tail) start the recursion at different bars and the absolute band LEVELS
        differ early on. BUT final_upper/lower are bounded by recent hl2 +/-
        mult*ATR and forget their initial condition in ~2*period bars, so for
        any bar past ~30 bars of warmup the band level — and therefore the
        direction/FLIP — converges identical between live and replay. This is
        the same parity argument as the OBV cross (iter 26): the SIGNAL event
        matches even though the level series can differ in warmup. Parity-
        verified on the last bar (which is what live uses) against the vectorized
        replay. Computed identically in
        ``specialized_replay_labeler._vectorized_features``.

        A genuinely-new TREND-STATE dimension: no prior setup keys off an
        ATR-band trend overlay with an explicit flip. m5_trend (EMA slope) gives
        trend DIRECTION; adx (iter 25) gives trend STRENGTH; Supertrend gives a
        third thing — a BAND-BOUNDED trend STATE that flips on a clean
        ATR-adjusted cross, the canonical TradingView trend-continuation trigger.
        Keys the Supertrend-flip-continuation entry model (bullish_flip -> BUY
        / bearish_flip -> SELL). Returns dir="up", flip="none" during warmup
        (<2*period bars).
        """
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()
        close = df["close"].to_numpy()
        n = len(df)
        # ATR(period) — simple rolling mean of True Range (matches the labeler).
        prev_close = np.concatenate(([np.nan], close[:-1]))
        tr = np.maximum.reduce([
            high - low,
            np.abs(high - prev_close),
            np.abs(low - prev_close),
        ])
        tr[0] = high[0] - low[0]  # no prior close on bar 0 -> NaN would poison cumsum
        # rolling-mean ATR with min_periods=period (NaN until enough bars)
        atr = np.full(n, np.nan)
        if n >= period:
            csum = np.cumsum(tr)
            atr[period - 1:] = (csum[period - 1:] - np.concatenate(([0.0], csum[:-period])) ) / period
        hl2 = (high + low) / 2.0
        basic_upper = hl2 + mult * atr
        basic_lower = hl2 - mult * atr
        final_upper = np.full(n, np.nan)
        final_lower = np.full(n, np.nan)
        direction = np.array(["up"] * n, dtype=object)
        if n == 0:
            return {"dir": "up", "flip": "none"}
        # seed at the first bar where ATR is defined
        start = period - 1 if n >= period else 0
        if n > period:
            final_upper[start] = basic_upper[start]
            final_lower[start] = basic_lower[start]
            direction[start] = "up" if close[start] > final_upper[start] else "down"
        for i in range(start + 1, n):
            fu = basic_upper[i] if (
                basic_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]
            ) else final_upper[i - 1]
            fl = basic_lower[i] if (
                basic_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]
            ) else final_lower[i - 1]
            final_upper[i] = fu
            final_lower[i] = fl
            if close[i] > final_upper[i - 1]:
                d = "up"
            elif close[i] < final_lower[i - 1]:
                d = "down"
            else:
                d = direction[i - 1]
            direction[i] = d
        d_now = str(direction[-1])
        d_prev = str(direction[-2]) if n >= 2 else d_now
        if d_prev == "down" and d_now == "up":
            flip = "bullish_flip"
        elif d_prev == "up" and d_now == "down":
            flip = "bearish_flip"
        else:
            flip = "none"
        return {"dir": d_now, "flip": flip}

    def _ichimoku(self, df: pd.DataFrame, tenkan_period: int = 9, kijun_period: int = 26,
                  senkou_b_period: int = 52) -> dict:
        """Ichimoku Kinko Hyo equilibrium — iteration 36.

        Classic TradingView Ichimoku: rolling high-low MIDPOINT equilibria, the
        core being the Tenkan-sen (9-bar midpoint) and Kijun-sen (26-bar
        midpoint) whose cross is the iconic Ichimoku trend-continuation trigger,
        confirmed by cloud position (price vs the Senkou Span A/B cloud). Formula
        (TradingView defaults 9/26/52):
          tenkan = (max(high,9)  + min(low,9))  / 2   — short-term equilibrium
          kijun  = (max(high,26) + min(low,26)) / 2   — medium-term baseline
          senkou_a = (tenkan + kijun) / 2              — cloud upper edge
          senkou_b = (max(high,52) + min(low,52)) / 2  — cloud lower edge
          cloud_position = above / below / inside (price vs the senkou_a/b cloud)
          tk_cross = bullish_cross (tenkan crosses above kijun) /
                     bearish_cross (tenkan crosses below kijun) / none

        A genuinely-new EQUILIBRIUM dimension: no prior setup keys off a rolling
        high-low MIDPOINT. m5_trend (EMA slope) is a smoothed-price direction;
        adx (iter 25) is trend strength; supertrend (iter 35) is an ATR-band
        state flip; vwap (iter 34) is a volume-weighted price anchor; the
        oscillators (rsi/stoch/cci/mfi) use close-vs-range. The rolling midpoint
        of high/low is a distinct equilibrium measure — it IS the market's
        short/medium-term fair value with no smoothing lag and no volume
        weighting — and the Tenkan/Kijun cross confirmed by cloud side is the
        canonical TradingView Ichimoku continuation signal. Keys the
        Ichimoku-TK-cross-continuation entry model (bullish_cross + above cloud
        -> BUY / bearish_cross + below cloud -> SELL).

        PARITY: the rolling max/min midpoint is PATH-INDEPENDENT (a fixed
        window — no carry-forward recursion like Supertrend/OBV), so live
        (500-bar tail) and replay (full-history tail) produce identical values
        for any bar past max(senkou_b_period)=52 bars of warmup. No NaN trap
        (rolling max/min over a fully-observed window is defined for every bar
        past the window). Parity-verified on the last bar against the vectorized
        replay. Computed identically in
        ``specialized_replay_labeler._vectorized_features``.
        """
        high = df["high"]
        low = df["low"]
        close = df["close"]
        n = len(df)
        if n < 2:
            return {"tenkan": 0.0, "kijun": 0.0, "senkou_a": 0.0, "senkou_b": 0.0,
                    "cloud_position": "above", "tk_cross": "none"}
        tenkan = (high.rolling(tenkan_period).max() + low.rolling(tenkan_period).min()) / 2.0
        kijun = (high.rolling(kijun_period).max() + low.rolling(kijun_period).min()) / 2.0
        senkou_a = (tenkan + kijun) / 2.0
        senkou_b = (high.rolling(senkou_b_period).max() + low.rolling(senkou_b_period).min()) / 2.0
        t = float(tenkan.iloc[-1]) if pd.notna(tenkan.iloc[-1]) else 0.0
        k = float(kijun.iloc[-1]) if pd.notna(kijun.iloc[-1]) else 0.0
        t_prev = float(tenkan.iloc[-2]) if pd.notna(tenkan.iloc[-2]) else t
        k_prev = float(kijun.iloc[-2]) if pd.notna(kijun.iloc[-2]) else k
        sa = float(senkou_a.iloc[-1]) if pd.notna(senkou_a.iloc[-1]) else 0.0
        sb = float(senkou_b.iloc[-1]) if pd.notna(senkou_b.iloc[-1]) else 0.0
        price = float(close.iloc[-1])
        # cloud position: above both edges / below both / inside the cloud
        cloud_top = max(sa, sb)
        cloud_bot = min(sa, sb)
        if price > cloud_top:
            cloud_position = "above"
        elif price < cloud_bot:
            cloud_position = "below"
        else:
            cloud_position = "inside"
        # Tenkan/Kijun cross event on the last bar
        if t_prev <= k_prev and t > k:
            tk_cross = "bullish_cross"
        elif t_prev >= k_prev and t < k:
            tk_cross = "bearish_cross"
        else:
            tk_cross = "none"
        return {"tenkan": t, "kijun": k, "senkou_a": sa, "senkou_b": sb,
                "cloud_position": cloud_position, "tk_cross": tk_cross}

    def _fvg(self, df: pd.DataFrame, atr: float, min_size_atr: float = 0.25) -> dict:
        """Fair Value Gap — ICT/SMC 3-bar structural imbalance — iteration 37.

        A Fair Value Gap is a 3-bar price inefficiency where the move between
        bar i-2 and bar i is so fast it leaves a GAP between the wicks of the
        non-adjacent bars (bar i-1 is the impulse bar). Classic ICT/TradingView
        definition:
          bullish_fvg: bar[i-2].high < bar[i].low  -> gap up, imbalance to the
                                                   upside, price tends to return
                                                   to fill the (high[i-2], low[i])
                                                   zone before continuing up
          bearish_fvg: bar[i-2].low > bar[i].high  -> gap down, imbalance to the
                                                     downside
        The gap is only tradeable if it is non-trivial relative to volatility,
        so it is gated by min_size_atr (gap size >= min_size_atr * ATR(14)) — a
        sub-ATR gap is noise, not a real inefficiency. fvg_size_atr is the gap
        size in ATR units (0.0 when no FVG).

        A genuinely-new PRICE-IMBALANCE dimension: no prior setup keys off a
        NON-ADJACENT-BAR gap. m5_trend (EMA slope) is smoothed direction; adx is
        strength; supertrend is an ATR-band state; ichimoku (iter 36) is a
        midpoint-equilibrium cross; the breakouts (breakout/compression/squeeze/
        volume/htf/atr_pct) all use PRIOR-BAR S/R levels (a 1-bar structure); vwap
        is a volume-weighted anchor. FVG is the only setup that detects a
        3-bar STRUCTURAL gap between non-adjacent bars — the canonical ICT/SMC
        inefficiency that price tends to fill, the most-traded FVG setup on
        TradingView. Keys the FVG-continuation entry model (bullish_fvg -> BUY
        / bearish_fvg -> SELL — trade the imbalance direction).

        PARITY: the 3-bar gap is PATH-INDEPENDENT (a fixed lookback to i-2, no
        carry-forward recursion), so live (500-bar tail) and replay (full-history
        tail) produce identical fvg/fvg_size_atr for every bar past index 2. No
        NaN trap (the gap is a direct comparison of observed highs/lows).
        Parity-verified on the last bar against the vectorized replay. Computed
        identically in ``specialized_replay_labeler._vectorized_features``.

        HONESTY: per-cell CI lo>0 is selection-biased (iter33-36 all confirmed;
        iter36 Ichimoku had 0/28 CI+). DSR/SPA + OOS across full K remains the
        bar; the per-cell numbers below are necessary-not-sufficient, NOT a
        deployable edge.
        """
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()
        n = len(df)
        if n < 3 or not atr or atr <= 0:
            return {"fvg": "none", "size_atr": 0.0}
        # FVG is confirmed on the last completed bar (i = n-1), using i-2 and i.
        i = n - 1
        h_prev2 = high[i - 2]
        l_prev2 = low[i - 2]
        h_now = high[i]
        l_now = low[i]
        gap_up = l_now - h_prev2  # bullish FVG gap (bar i-2 high -> bar i low)
        gap_dn = l_prev2 - h_now  # bearish FVG gap (bar i-2 low -> bar i high)
        if gap_up > 0 and (gap_up / atr) >= min_size_atr:
            return {"fvg": "bullish_fvg", "size_atr": float(gap_up / atr)}
        if gap_dn > 0 and (gap_dn / atr) >= min_size_atr:
            return {"fvg": "bearish_fvg", "size_atr": float(gap_dn / atr)}
        return {"fvg": "none", "size_atr": 0.0}

    def _engulfing(self, df: pd.DataFrame) -> str:
        """2-bar Engulfing candlestick pattern — iteration 38.

        Classic TradingView candlestick reversal pattern: a 2-bar candle-BODY
        structure where the current bar's body ENGULFS the prior bar's body.
          bullish_engulfing: prior bar bearish (close < open) AND current bar
                             bullish (close > open) AND current open <= prior
                             close AND current close >= prior open  (the bullish
                             body wraps the prior bearish body -> 2-bar reversal)
          bearish_engulfing: prior bar bullish (close > open) AND current bar
                             bearish (close < open) AND current open >= prior
                             close AND current close <= prior open
          none otherwise.

        A genuinely-new CANDLE-STRUCTURE dimension: no prior setup keys off a
        2-bar body-vs-body engulf. ``rejection`` / ``liquidity_sweep`` /
        ``false_breakout`` (iters 1) all use the 1-bar WICK-vs-body rejection
        (a single-bar wick signal); ``fvg`` (iter 37) uses a 3-bar gap between
        non-adjacent bars; the oscillators/crosses use smoothed levels. The
        2-bar engulfing body-wrap is the canonical TradingView candlestick
        reversal pattern, a distinct entry model. Keys the Engulfing-reversal
        entry model (bullish_engulfing -> BUY / bearish_engulfing -> SELL).

        PARITY: the 2-bar pattern is PATH-INDEPENDENT (a fixed lookback to
        i-1, no carry-forward recursion, no data-availability issue like daily
        pivots), so live (500-bar tail) and replay (full-history tail) produce
        identical engulfing for every bar past index 1. No NaN trap (direct
        comparison of observed opens/closes). Parity-verified on the last bar
        against the vectorized replay. Computed identically in
        ``specialized_replay_labeler._vectorized_features``.

        HONESTY: per-cell CI lo>0 is selection-biased (iter33-37 all confirmed;
        iter37 FVG had 3/28 CI+ overlapping the iter33-adjudicated-negative
        EUR/GBP L+NY cluster). DSR/SPA + OOS across full K remains the bar;
        per-cell numbers below are necessary-not-sufficient, NOT a deployable
        edge.
        """
        open_ = df["open"].to_numpy()
        close = df["close"].to_numpy()
        n = len(df)
        if n < 2:
            return "none"
        o_prev, c_prev = open_[-2], close[-2]
        o_now, c_now = open_[-1], close[-1]
        prev_bear = c_prev < o_prev
        prev_bull = c_prev > o_prev
        now_bull = c_now > o_now
        now_bear = c_now < o_now
        if prev_bear and now_bull and o_now <= c_prev and c_now >= o_prev:
            return "bullish_engulfing"
        if prev_bull and now_bear and o_now >= c_prev and c_now <= o_prev:
            return "bearish_engulfing"
        return "none"

    def _inside_bar(self, df: pd.DataFrame) -> str:
        """3-bar Inside-Bar breakout — iteration 39.

        Classic TradingView candlestick pattern: a mother bar, an inside bar
        whose full range nests within the mother bar's range (contraction), then
        a breakout bar that closes outside the mother bar's range (expansion).
          bullish_inside_breakout: bar[i-1] is inside bar[i-2]
                                   (high[i-1] <= high[i-2] AND low[i-1] >=
                                   low[i-2]) AND bar[i] close > high[i-2]
                                   (breakout UP through the mother high -> BUY)
          bearish_inside_breakout: same inside condition AND bar[i] close <
                                   low[i-2] (breakout DOWN through the mother
                                   low -> SELL)
          none otherwise.

        A genuinely-new CANDLE-STRUCTURE dimension: no prior setup keys off a
        2-3-bar RANGE-NESTING relationship. ``engulfing`` (iter 38) is a 2-bar
        BODY-vs-body wrap; ``fvg`` (iter 37) is a 3-bar price GAP between
        non-adjacent bars; ``rejection``/``liquidity_sweep``/``false_breakout``
        (iter 1) are 1-bar WICK signals; legacy ``breakout``/``compression_
        breakout`` key off a 1-bar S/R state / BB-squeeze width (continuous
        metrics), not raw 2-bar range containment. Inside-bar is the canonical
        TradingView volatility-contraction-then-expansion pattern, a distinct
        entry model. Keys the inside-bar-breakout entry model
        (bullish_inside_breakout -> BUY / bearish_inside_breakout -> SELL).

        PARITY: the 3-bar pattern is PATH-INDEPENDENT (a fixed lookback to
        i-2/i-1, no carry-forward recursion, no data-availability issue like
        daily pivots), so live (500-bar tail) and replay (full-history tail)
        produce identical inside_bar for every bar past index 2. No NaN trap
        (direct comparison of observed highs/lows/close). Parity-verified on
        the last bar against the vectorized replay. Computed identically in
        ``specialized_replay_labeler._vectorized_features``.

        HONESTY: per-cell CI lo>0 is selection-biased (iter33-38 all confirmed;
        iter38 Engulfing was the cleanest negative 0/28 CI+). DSR/SPA + OOS
        across full K remains the bar; per-cell numbers below are
        necessary-not-sufficient, NOT a deployable edge.
        """
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()
        close = df["close"].to_numpy()
        n = len(df)
        if n < 3:
            return "none"
        h_mother, l_mother = high[-3], low[-3]
        h_inside, l_inside = high[-2], low[-2]
        c_now = close[-1]
        is_inside = (h_inside <= h_mother) and (l_inside >= l_mother)
        if not is_inside:
            return "none"
        if c_now > h_mother:
            return "bullish_inside_breakout"
        if c_now < l_mother:
            return "bearish_inside_breakout"
        return "none"

    def _close_streak(self, df: pd.DataFrame) -> int:
        """Consecutive same-direction close streak — iteration 40.

        A RUN-LENGTH / statistical momentum dimension: the signed count of
        consecutive same-direction closes ending at the current bar.
          +N: the last N closes were each strictly higher than the prior close
               (a consecutive up-close run of length N ending now)
          -N: the last N closes were each strictly lower than the prior close
               (a consecutive down-close run of length N ending now)
          0: the current close equals the prior close, or fewer than 2 bars.

        A genuinely-new STATISTICAL dimension: no prior setup keys off a
        consecutive-close run-length. ``m5_trend`` is an EMA-ribbon STATE;
        ``adx_trend`` is ADX STRENGTH; ``mtf_align`` is m5-vs-m15 trend
        AGREEMENT; the oscillators use smoothed LEVELS/crosses; the candle-
        structure setups (rejection/engulfing/fvg/inside_bar) use 1-3-bar
        PATTERNS. None count a run of consecutive same-direction closes. The
        run-length is the canonical TradingView "consecutive candle
        streak" / "N-bar momentum" signal, a distinct entry model. Keys the
        close-streak-continuation entry model (streak >= +threshold -> BUY
        continuation / streak <= -threshold -> SELL continuation).

        PARITY: the run-length at the current bar is PATH-INDEPENDENT — it is
        the count of consecutive same-direction closes ending at the last bar,
        which depends only on the observed close series in the lookback window,
        not on any carry-forward state or data availability beyond the window.
        Live (500-bar tail) and replay (full-history tail) produce the SAME
        streak value at the last bar as long as the streak length is shorter
        than the window (true for any practical threshold: a 500-bar window
        holds runs up to 500, far beyond any signaling threshold of 3-5). No
        NaN trap (direct comparison of observed closes). Parity-verified on the
        last bar against the vectorized replay. Computed identically in
        ``specialized_replay_labeler._vectorized_features``.

        HONESTY: per-cell CI lo>0 is selection-biased (iter33-39 all confirmed;
        iter38+iter39 both 0/28 CI+). DSR/SPA + OOS across full K remains the
        bar; per-cell numbers below are necessary-not-sufficient, NOT a
        deployable edge.
        """
        close = df["close"].to_numpy()
        n = len(df)
        if n < 2:
            return 0
        # walk backward from the last bar while same direction
        streak = 0
        prev_dir = 0  # +1 up, -1 down, 0 flat
        for i in range(n - 1, 0, -1):
            d = 0
            if close[i] > close[i - 1]:
                d = 1
            elif close[i] < close[i - 1]:
                d = -1
            if d == 0:
                break
            if prev_dir == 0:
                prev_dir = d
                streak = d
            elif d == prev_dir:
                streak += d
            else:
                break
        return int(streak)

    def _order_block(self, df: pd.DataFrame, atr: float,
                     min_displacement_atr: float = 0.8) -> str:
        """ICT/SMC Order-Block displacement origin — iteration 41.

        A CANDLE-STRUCTURE + impulse-magnitude dimension: the last bar is a
        strong DISPLACEMENT candle whose origin is the prior OPPOSITE-colored
        candle (the "order block"). Bullish order block = prior bearish candle
        followed by a bullish displacement bar whose body >= min_displacement_atr
        * ATR(14); bearish = mirror. The prior opposite candle is the institutional
        origin level; the strong body is the displacement away from it.

        Genuinely distinct from the other candle-structure setups:
          - engulfing (iter 38) keys off a 2-bar BODY WRAP (curr body wraps prior
            body) with NO magnitude gate; order_block keys off IMPULSE MAGNITUDE
            (body >= k*ATR) with NO wrap requirement. A strong impulse that does
            not wrap the prior body fires order_block but not engulfing; a tiny
            body that wraps fires engulfing but not order_block.
          - inside_bar (iter 39) keys off range-NESTING then expansion (3 bars).
          - fvg (iter 37) keys off a 3-bar gap between NON-adjacent bars.
          - rejection/liquidity_sweep/false_breakout use a 1-bar WICK signal.
        None require a displacement-magnitude body after an opposite origin
        candle. The order-block is the canonical TradingView ICT/SMC "displacement
        origin" signal, a distinct entry model.

        PARITY: PATH-INDEPENDENT 2-bar structure (prior + current bar only, no
        recursion, no carry-forward state). Live (500-bar tail, last-bar atr) and
        replay (full-history tail, last-bar atr) produce the SAME order_block
        label at the last bar as long as atr > 0 (true on any liquid symbol after
        the 14-bar warmup). No NaN trap (direct comparison of observed OHLC).
        Parity-verified on the last bar against the vectorized replay. Computed
        identically in ``specialized_replay_labeler._vectorized_features``.

        HONESTY: per-cell CI lo>0 is selection-biased across the full specialized-
        replay search (iter33-40 all confirmed; iter40 Close-streak had 4/28 CI+
        overlapping the iter33-adjudicated-negative EUR/GBP L+NY cluster). DSR/SPA
        + OOS across full K remains the bar; per-cell numbers below are necessary-
        not-sufficient, NOT a deployable edge.
        """
        if len(df) < 2 or not atr or atr <= 0:
            return "none"
        o = df["open"].to_numpy()
        c = df["close"].to_numpy()
        o_prev, c_prev = float(o[-2]), float(c[-2])
        o_now, c_now = float(o[-1]), float(c[-1])
        body_now = abs(c_now - o_now)
        threshold = float(min_displacement_atr) * float(atr)
        if body_now < threshold:
            return "none"
        prev_bear = c_prev < o_prev
        prev_bull = c_prev > o_prev
        now_bull = c_now > o_now
        now_bear = c_now < o_now
        if prev_bear and now_bull:
            return "bullish_order_block"
        if prev_bull and now_bear:
            return "bearish_order_block"
        return "none"

    def _ha_trend(self, df: pd.DataFrame, lookback: int = 500) -> str:
        """Heikin-Ashi smoothed-candle strong-trend signal — iteration 42.

        A PRICE-TRANSFORM dimension: Heikin-Ashi replaces raw OHLC with a smoothed
        candle series, then reads the SMOOTHED CANDLE's wick structure for a
        strong-trend signal. No prior setup transforms the OHLC series before
        reading structure — they all read raw OHLC:
          - close_streak (iter 40) counts raw closes;
          - engulfing (iter 38) / inside_bar (iter 39) / fvg (iter 37) / order_block
            (iter 41) read raw 2-3-bar patterns;
          - rejection reads a raw 1-bar wick.
        HA dampens noise (HA_open is a 0.5-alpha recursive average of prior
        HA_open+HA_close), so a smoothed no-lower-wick / no-upper-wick candle is a
        genuinely different strong-trend signal. This is the canonical TradingView
        "Heikin-Ashi smoothed candle" signal, a distinct 6th family.

        HA_close[i] = (O+H+L+C)/4; HA_open[i] = (HA_open[i-1]+HA_close[i-1])/2,
        seeded HA_open[0] = (O[0]+C[0])/2. bullish_ha_strong = HA_close > HA_open
        (green HA) AND low >= HA_open (no lower wick — the real low did not poke
        below the HA open, a strong bullish HA candle). bearish_ha_strong =
        HA_close < HA_open (red HA) AND high <= HA_open (no upper wick). else none.

        PARITY: HA_open is a 0.5-alpha recursion that forgets its seed at rate
        0.5/bar — after ~20 bars the seed contributes <1e-6, after 500 bars
        <1e-150. The live method uses the last `lookback`=500 bars (seeded 500
        bars back); the vectorized replay uses full history (seeded at bar 0).
        Both converge to the SAME HA_open at the last bar to ~300 decimal places,
        so the last-bar ha_trend label matches exactly (the wick-absence
        comparison low>=HA_open is robust unless HA_open is within 1e-150 of low,
        which cannot occur). This is the same accepted approximation the project
        uses for all recursive features (EMA/supertrend/ichimoku/vwap). No NaN
        trap. Parity-verified on the last bar against the vectorized replay.
        Computed identically in ``specialized_replay_labeler._vectorized_features``.

        HONESTY: per-cell CI lo>0 is selection-biased across the full specialized-
        replay search (iter33-41 all confirmed; iter41 Order-Block 1/28 CI+ clean
        negative). DSR/SPA + OOS across full K remains the bar; per-cell numbers
        below are necessary-not-sufficient, NOT a deployable edge.
        """
        import numpy as np
        o = df["open"].to_numpy(); h = df["high"].to_numpy()
        l = df["low"].to_numpy(); c = df["close"].to_numpy()
        n = len(df)
        if n < 2:
            return "none"
        start = max(0, n - lookback)
        ha_close = np.empty(n)
        ha_open = np.empty(n)
        ha_close[start] = (o[start] + h[start] + l[start] + c[start]) / 4.0
        ha_open[start] = (o[start] + c[start]) / 2.0
        for i in range(start + 1, n):
            ha_close[i] = (o[i] + h[i] + l[i] + c[i]) / 4.0
            ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0
        i = n - 1
        gc = ha_close[i]; go = ha_open[i]
        if gc > go and l[i] >= go:
            return "bullish_ha_strong"
        if gc < go and h[i] <= go:
            return "bearish_ha_strong"
        return "none"

    def _orb(self, df: pd.DataFrame, symbol: str, or_bars: int = OR_BARS_DEFAULT) -> dict:
        """Opening Range Breakout state for the CURRENT bar — iteration 43.

        A SESSION-ANCHORED RANGE dimension: the high/low of the first `or_bars`
        M5 bars after the symbol's primary cash-session open defines the opening
        range; the FIRST close beyond that range after the OR forms is the
        breakout. No prior setup keys off a session-anchored range — killzone
        setups (london_/ny_) gate a candle-pattern detect by UTC hour but read
        bar structure, not a defined intraday range. ORB is the canonical
        TradingView "opening range breakout" and a distinct entry model. Each
        symbol lands in its own culturing cell (symbol, "opening_range_breakout")
        because the session anchor differs per symbol.

        Lookahead-free + live/research parity: at the current bar i=n-1, find
        TODAY's first bar at/after the session open (open_idx), form the OR over
        [open_idx, open_idx+or_bars-1], then scan bars (or_end, i) — if any
        already closed beyond the OR, the breakout already happened (no re-fire);
        only if bar i is the FIRST close beyond the OR do we emit. This depends
        only on past + current bars, so the last-bar label computed here matches
        the vectorized replay's label at the same bar exactly. Computed on the
        same `df_m5` the other features use (history-augmented latest bars).

        HONESTY: OOS score on this exact feature (scripts/score_opening_range_
        breakout.py) = pooled +0.0051R raw / +0.0209R with a 1.5x volume filter,
        0/14 cells clearing the per-cell gate — NO EDGE at retail 30bps pre-cost.
        Per-cell CI lo>0 is selection-biased; DSR/SPA across full K remains the
        bar. Wired in live on demo per explicit user sign-off ("turn it on right
        now ... if they don't [do well] who cares"); demo losses are the accepted
        cost of live forward scoring.
        """
        n = len(df)
        if n < or_bars + 2:
            return {"orb_signal": None, "or_high": 0.0, "or_low": 0.0}
        try:
            ts = pd.to_datetime(df["time"], utc=True)
        except Exception:  # noqa: BLE001 — never break the feature pipeline
            return {"orb_signal": None, "or_high": 0.0, "or_low": 0.0}
        open_h, open_m = OR_SESSION_OPEN_UTC.get(symbol, (0, 0))
        open_mod = open_h * 60 + open_m
        minute_of_day = ts.dt.hour * 60 + ts.dt.minute
        day_key = ts.dt.strftime("%Y-%m-%d")
        cur_day = str(day_key.iloc[-1])
        today_mask = (day_key == cur_day).to_numpy()
        if int(today_mask.sum()) < or_bars + 2:
            return {"orb_signal": None, "or_high": 0.0, "or_low": 0.0}
        idx_today = np.where(today_mask)[0]
        mod_today = minute_of_day.to_numpy()[idx_today]
        after = idx_today[mod_today >= open_mod]
        if len(after) < or_bars + 2:
            return {"orb_signal": None, "or_high": 0.0, "or_low": 0.0}
        open_idx = int(after[0])
        or_end = open_idx + or_bars - 1
        cur = n - 1
        if cur <= or_end:
            return {"orb_signal": None, "or_high": 0.0, "or_low": 0.0}
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        closes = df["close"].to_numpy()
        or_high = float(highs[open_idx:or_end + 1].max())
        or_low = float(lows[open_idx:or_end + 1].min())
        if or_high <= or_low:
            return {"orb_signal": None, "or_high": 0.0, "or_low": 0.0}
        # First-breakout check: if any bar between OR end and the current bar
        # already closed beyond the OR, the day's breakout already fired -> None.
        for k in range(or_end + 1, cur):
            if closes[k] > or_high or closes[k] < or_low:
                return {"orb_signal": None, "or_high": round(or_high, 5),
                        "or_low": round(or_low, 5)}
        c = float(closes[cur])
        if c > or_high:
            return {"orb_signal": "bullish_breakout", "or_high": round(or_high, 5),
                    "or_low": round(or_low, 5)}
        if c < or_low:
            return {"orb_signal": "bearish_breakout", "or_high": round(or_high, 5),
                    "or_low": round(or_low, 5)}
        return {"orb_signal": None, "or_high": round(or_high, 5),
                "or_low": round(or_low, 5)}

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