"""Spread model — current vs historical spread for cost estimation."""

from __future__ import annotations

import numpy as np
import pandas as pd


def session_spread_profile(
    df: pd.DataFrame,
    *,
    spread_col: str = "spread",
    session_col: str | None = None,
) -> dict[str, dict[str, float]]:
    """
    Compute spread statistics for each session or for the entire DataFrame.
    
    Parameters:
        spread_col (str): Name of the column containing spread values.
        session_col (str | None): Name of the column used to group statistics by
            session. If omitted or unavailable, statistics are computed globally.
    
    Returns:
        dict[str, dict[str, float]]: Mapping of session names to mean, median,
            95th percentile, 99th percentile, and count of valid spread values.
            Returns an empty dictionary when the spread column is unavailable.
    """
    if spread_col not in df.columns:
        return {}

    if session_col and session_col in df.columns:
        groups = df.groupby(session_col)
    else:
        groups = [("global", df)]

    result: dict[str, dict[str, float]] = {}
    for name, group in groups:
        s = group[spread_col].dropna()
        if len(s) == 0:
            continue
        result[str(name)] = {
            "mean": float(s.mean()),
            "median": float(s.median()),
            "p95": float(np.percentile(s, 95)),
            "p99": float(np.percentile(s, 99)),
            "count": len(s),
        }
    return result


def spread_atr_ratio(
    df: pd.DataFrame,
    *,
    spread_col: str = "spread",
    atr_period: int = 14,
    point: float | None = None,
) -> pd.Series:
    """
    Calculate spread cost relative to rolling average true range.
    
    Parameters:
        spread_col (str): Name of the spread column.
        atr_period (int): Number of periods used to calculate the rolling average true range.
        point (float | None): Optional multiplier for converting spread values to price units.
    
    Returns:
        pd.Series: Spread-to-ATR ratios, with undefined values where the ATR is zero.
    """
    if spread_col not in df.columns:
        return pd.Series(dtype=float)

    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(window=atr_period, min_periods=1).mean()

    if point is not None:
        spread_px = df[spread_col] * point
    else:
        spread_px = df[spread_col]

    return spread_px / atr.replace(0, np.nan)
