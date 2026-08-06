"""Trend baseline strategies for the isolated research plane."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import numpy as np
import pandas as pd


def moving_average_signal(
    close: pd.Series,
    *,
    period: int = 200,
) -> pd.Series:
    """Return +1 above, -1 below, and 0 without a valid moving average."""
    ma = close.rolling(window=period, min_periods=period).mean()
    signal = pd.Series(0.0, index=close.index)
    signal[close > ma] = 1.0
    signal[close < ma] = -1.0
    return signal


def time_series_momentum_signal(
    close: pd.Series,
    *,
    lookback: int = 63,
) -> pd.Series:
    """Return the sign of the price return over ``lookback`` periods."""
    ret = close.pct_change(periods=lookback)
    signal = pd.Series(0.0, index=close.index)
    signal[ret > 0] = 1.0
    signal[ret < 0] = -1.0
    return signal


def blended_momentum_signal(
    close: pd.Series,
    *,
    horizons: tuple[int, ...] = (21, 63, 126, 252),
    threshold: float = 0.0,
) -> pd.Series:
    """Return a direction from standardized momentum across several horizons."""
    if len(close) < max(horizons) + 1:
        return pd.Series(0.0, index=close.index)

    scores = pd.DataFrame(index=close.index)
    for horizon in horizons:
        ret = close.pct_change(periods=horizon)
        mean = ret.rolling(window=horizon, min_periods=horizon).mean()
        std = ret.rolling(window=horizon, min_periods=horizon).std()
        scores[f"h{horizon}"] = (ret - mean) / std.replace(0, np.nan)

    blended = scores.mean(axis=1)
    signal = pd.Series(0.0, index=close.index)
    signal[blended > threshold] = 1.0
    signal[blended < -threshold] = -1.0
    return signal


def _round_to_tick(value: float, tick_size: float | None, digits: int | None) -> float:
    """Round a price to the supplied tick size or decimal precision."""
    if tick_size is not None and np.isfinite(tick_size) and tick_size > 0:
        tick = Decimal(str(tick_size))
        ticks = (Decimal(str(value)) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return float(ticks * tick)
    precision = 5 if digits is None else max(0, int(digits))
    return round(value, precision)


def signal_to_intent(
    symbol: str,
    signal: float,
    price: float,
    atr: float | None = None,
    *,
    risk_mult: float = 2.0,
    reward_mult: float = 3.0,
    tick_size: float | None = None,
    digits: int | None = None,
) -> dict[str, Any] | None:
    """Convert a research signal into a nullable strategy-intent dictionary.

    ``tick_size`` takes precedence over ``digits`` when supplied. If neither is
    given, five decimal places are retained for backward compatibility. An ATR
    value of exactly ``0.0`` uses the percentage fallback, matching a missing ATR.
    """
    if signal == 0:
        return None

    side = "BUY" if signal > 0 else "SELL"
    stop_distance = atr * risk_mult if atr else price * 0.02
    target_distance = atr * reward_mult if atr else price * 0.06
    stop = price - stop_distance if side == "BUY" else price + stop_distance
    target = price + target_distance if side == "BUY" else price - target_distance

    return {
        "symbol": symbol,
        "side": side,
        "entry": _round_to_tick(price, tick_size, digits),
        "stop": _round_to_tick(stop, tick_size, digits),
        "target": _round_to_tick(target, tick_size, digits),
        "signal_strength": round(float(signal), 4),
        "source": "trend_baseline",
    }
