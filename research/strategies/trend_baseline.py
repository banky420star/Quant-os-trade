"""Trend baseline strategies — moving-average crossover and time-series momentum.

Implements the two simplest trend models from the roadmap:

  A.  Close above/below 200-period moving average
  B.  Sign of 3-period return (where period = month for monthly, day for daily)

Outputs a ``StrategyIntent``-shaped dict suitable for the research plane.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def moving_average_signal(
    close: pd.Series,
    *,
    period: int = 200,
) -> pd.Series:
    """Return +1 (long), -1 (short), 0 (flat) based on close vs MA.

    Signal is computed at each bar; only the *latest* value matters for
    a weekly-rebalanced portfolio, but the full series is returned for
    backtesting.
    """
    ma = close.rolling(window=period, min_periods=period).mean()
    signal = pd.Series(0.0, index=close.index)
    signal[close > ma] = 1.0
    signal[close < ma] = -1.0
    return signal


def time_series_momentum_signal(
    close: pd.Series,
    *,
    lookback: int = 63,  # ~3 months of trading days
) -> pd.Series:
    """Return +1 / -1 / 0 based on the sign of *lookback*-period return."""
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
    """Blended momentum: standardise returns at each horizon, average, threshold.

    *horizons*: trading-day lookbacks (default 1/3/6/12-month approx).
    *threshold*: only trade when |blended| > threshold.
    """
    if len(close) < max(horizons) + 1:
        return pd.Series(0.0, index=close.index)

    scores = pd.DataFrame(index=close.index)
    for h in horizons:
        ret = close.pct_change(periods=h)
        mu = ret.rolling(window=h, min_periods=h).mean()
        sigma = ret.rolling(window=h, min_periods=h).std()
        scores[f"h{h}"] = (ret - mu) / sigma.replace(0, np.nan)

    blended = scores.mean(axis=1)
    signal = pd.Series(0.0, index=close.index)
    signal[blended > threshold] = 1.0
    signal[blended < -threshold] = -1.0
    return signal


def signal_to_intent(
    symbol: str,
    signal: float,
    price: float,
    atr: float | None = None,
    *,
    risk_mult: float = 2.0,
    reward_mult: float = 3.0,
) -> dict[str, Any] | None:
    """Convert a -1/0/+1 signal into a StrategyIntent dict.

    Returns None for flat (0) signals.
    """
    if signal == 0:
        return None

    side = "BUY" if signal > 0 else "SELL"
    stop_distance = atr * risk_mult if atr else price * 0.02
    target_distance = atr * reward_mult if atr else price * 0.06

    return {
        "symbol": symbol,
        "side": side,
        "entry": round(price, 5),
        "stop": round(price - stop_distance if side == "BUY" else price + stop_distance, 5),
        "target": round(price + target_distance if side == "BUY" else price - target_distance, 5),
        "signal_strength": round(float(signal), 4),
        "source": "trend_baseline",
    }
