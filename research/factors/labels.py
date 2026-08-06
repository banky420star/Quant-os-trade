"""Label construction for supervised rankers.

Labels are forward-looking and must be computed with point-in-time rigor. No
future data may leak into feature computation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def future_vol_adjusted_return(
    close: pd.Series,
    *,
    horizon: int = 5,
    vol_lookback: int = 20,
) -> pd.Series:
    """Compute future return divided by annualized historical volatility."""
    ret = close.pct_change(periods=horizon).shift(-horizon)
    vol = (
        close.pct_change()
        .rolling(vol_lookback, min_periods=5)
        .std()
        .shift(-horizon)
    )
    annual_vol = vol * np.sqrt(252)
    return ret / annual_vol.replace(0, np.nan)


def prob_target_before_stop(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    entry: float | None = None,
    *,
    lookahead: int = 20,
    multiplier: float = 1.5,
) -> pd.Series:
    """Label whether a long-only target is reached before its stop.

    The target and stop are inspected over the next ``lookahead`` bars. When a
    bar touches both levels, the stop wins, which is the conservative intrabar
    tie-break. The result is ``1.0`` for target first, ``0.0`` for stop first,
    and missing when neither level is reached or the inputs are incomplete.
    """
    atr = (high - low).rolling(14, min_periods=5).mean()
    stop_dist = (atr * multiplier).to_numpy(dtype=float)
    target_dist = (atr * (multiplier + 1.0)).to_numpy(dtype=float)
    high_values = high.to_numpy(dtype=float)
    low_values = low.to_numpy(dtype=float)
    close_values = close.to_numpy(dtype=float)
    result_values = np.full(len(close), np.nan, dtype=float)

    fixed_entry = float(entry) if entry is not None else None
    for i in range(max(0, len(close) - lookahead)):
        entry_price = fixed_entry if fixed_entry is not None else close_values[i]
        if not (
            np.isfinite(entry_price)
            and np.isfinite(stop_dist[i])
            and np.isfinite(target_dist[i])
        ):
            continue

        stop_level = entry_price - stop_dist[i]
        target_level = entry_price + target_dist[i]
        end = min(i + lookahead + 1, len(close))
        for j in range(i + 1, end):
            if np.isfinite(low_values[j]) and low_values[j] <= stop_level:
                result_values[i] = 0.0
                break
            if np.isfinite(high_values[j]) and high_values[j] >= target_level:
                result_values[i] = 1.0
                break

    return pd.Series(result_values, index=close.index)


def expected_realized_r(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    *,
    lookahead: int = 20,
    stop_atr_mult: float = 2.0,
) -> pd.Series:
    """Calculate long-only realized return in initial-risk units.

    A position exits at its ATR-based stop when touched, otherwise at the close
    after ``lookahead`` bars. The ``high`` series participates in the ATR range
    estimate; no profit target is applied by this label.
    """
    atr = (high - low).rolling(14, min_periods=5).mean()
    stop_dist = (atr * stop_atr_mult).to_numpy(dtype=float)
    close_values = close.to_numpy(dtype=float)
    low_values = low.to_numpy(dtype=float)
    result_values = np.full(len(close), np.nan, dtype=float)

    for i in range(max(0, len(close) - lookahead)):
        entry_price = close_values[i]
        distance = stop_dist[i]
        if not (np.isfinite(entry_price) and np.isfinite(distance) and distance > 0):
            continue

        stop_level = entry_price - distance
        exit_price = close_values[i + lookahead]
        if not np.isfinite(exit_price):
            continue

        end = min(i + lookahead + 1, len(close))
        for j in range(i + 1, end):
            if np.isfinite(low_values[j]) and low_values[j] <= stop_level:
                exit_price = stop_level
                break

        result_values[i] = (exit_price - entry_price) / distance

    return pd.Series(result_values, index=close.index)
