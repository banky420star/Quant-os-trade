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
    """Compute spread statistics grouped by session (or overall).

    Returns ``{session: {mean, median, p95, p99}}``.
    If *session_col* is None, returns a single ``\"global\"`` entry.
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
    """Return the ratio of spread to ATR as a time series.

    Low ratio → spread is cheap relative to typical movement.
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
