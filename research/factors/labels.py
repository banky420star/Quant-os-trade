"""Label construction for supervised rankers.

Labels are forward-looking and must be computed with point-in-time
rigor.  No future data may leak into feature computation.
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
    """Future *horizon*-day return divided by *vol_lookback*-day annualised vol.

    Shifts the vol forward so the label is known only after the horizon.
    """
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
    """Estimate P(target hit before stop) over the next *lookahead* bars.

    Uses ATR-based stop/target distances. Returns 1.0 if target hit first,
    0.0 if stop hit first, NaN if neither.
    """
    atr = (high - low).rolling(14, min_periods=5).mean()
    stop_dist = atr * multiplier
    target_dist = atr * (multiplier + 1.0)  # asymmetric RR

    result = pd.Series(np.nan, index=close.index)

    for i in range(len(close) - lookahead):
        entry_price = entry or float(close.iloc[i])
        stop_level = entry_price - stop_dist.iloc[i]
        target_level = entry_price + target_dist.iloc[i]

        for j in range(1, lookahead + 1):
            if i + j >= len(high):
                break
            if low.iloc[i + j] <= stop_level:
                result.iloc[i] = 0.0
                break
            if high.iloc[i + j] >= target_level:
                result.iloc[i] = 1.0
                break

    return result


def expected_realized_r(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    *,
    lookahead: int = 20,
    stop_atr_mult: float = 2.0,
) -> pd.Series:
    """Estimated future realised R after costs (per the roadmap's label
    recommendation).  Exit is assumed on stop-hit, target-hit, or
    lookahead expiry (at close).
    """
    atr = (high - low).rolling(14, min_periods=5).mean()
    stop_dist = atr * stop_atr_mult
    result = pd.Series(np.nan, index=close.index)

    for i in range(len(close) - lookahead):
        entry_price = float(close.iloc[i])
        stop_level = entry_price - stop_dist.iloc[i]
        risk = entry_price - stop_level
        if risk <= 0:
            continue

        exit_price = float(close.iloc[i + lookahead])
        for j in range(1, lookahead + 1):
            if i + j >= len(low):
                break
            if low.iloc[i + j] <= stop_level:
                exit_price = stop_level
                break

        result.iloc[i] = (exit_price - entry_price) / risk

    return result
