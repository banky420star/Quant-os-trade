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
    """
    Generate directional signals by comparing closing prices with their rolling moving average.
    
    Parameters:
    	close (pd.Series): Closing prices indexed by observation.
    	period (int): Number of observations used to calculate the moving average.
    
    Returns:
    	pd.Series: A series containing `1.0` when the close is above the moving average,
    	`-1.0` when it is below, and `0.0` when the moving average is unavailable or
    	the close equals the moving average.
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
    """
    Determine the directional signal from the return over a specified lookback period.
    
    Parameters:
        close (pd.Series): Closing prices indexed in time.
        lookback (int): Number of periods used to calculate the return.
    
    Returns:
        pd.Series: A series containing 1.0 for positive returns, -1.0 for negative returns, and 0.0 for zero or unavailable returns.
    """
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
    """
    Generate trend signals from standardized momentum across multiple lookback horizons.
    
    Parameters:
        close (pd.Series): Closing prices.
        horizons (tuple[int, ...]): Lookback periods used to calculate momentum scores.
        threshold (float): Minimum absolute blended score required for a long or short signal.
    
    Returns:
        pd.Series: A series containing 1.0 for long signals, -1.0 for short signals, and 0.0 otherwise.
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
    """
    Convert a trading signal into a strategy intent.
    
    Parameters:
    	symbol (str): Instrument symbol.
    	signal (float): Trading signal, where positive values indicate a buy and negative values indicate a sell.
    	price (float): Entry price used to calculate the intent levels.
    	atr (float | None): Average true range used to calculate stop and target distances when provided.
    	risk_mult (float): ATR multiplier for the stop distance.
    	reward_mult (float): ATR multiplier for the target distance.
    
    Returns:
    	dict[str, Any] | None: A strategy intent containing the symbol, side, rounded entry, stop, target, signal strength, and source; `None` for a zero signal.
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
