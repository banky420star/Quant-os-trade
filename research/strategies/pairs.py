"""Pairs trading research — rolling cointegration and residual z-scores.

Phase 4 of the roadmap.  This module is research-only; it outputs
hypothetical signals and does not interact with the execution plane.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy import stats


def rolling_hedge_ratio(
    y: pd.Series,
    x: pd.Series,
    window: int = 252,
) -> pd.DataFrame:
    """Estimate rolling hedge ratio via OLS:  y = alpha + beta * x + e.

    Returns DataFrame with columns ``alpha``, ``beta``, ``r_squared``.
    """
    if len(y) < window or len(x) < window:
        return pd.DataFrame({"alpha": [np.nan], "beta": [np.nan]}, index=y.index)

    common_idx = y.index.intersection(x.index)
    y_aligned = y.reindex(common_idx)
    x_aligned = x.reindex(common_idx)

    alphas = pd.Series(np.nan, index=common_idx)
    betas = pd.Series(np.nan, index=common_idx)
    r2s = pd.Series(np.nan, index=common_idx)

    for i in range(window - 1, len(common_idx)):
        yi = y_aligned.iloc[i - window + 1 : i + 1]
        xi = x_aligned.iloc[i - window + 1 : i + 1]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            slope, intercept, r_val, _, _ = stats.linregress(xi, yi)
        alphas.iloc[i] = intercept
        betas.iloc[i] = slope
        r2s.iloc[i] = r_val ** 2

    return pd.DataFrame({"alpha": alphas, "beta": betas, "r_squared": r2s}, index=common_idx)


def spread_residual(
    y: pd.Series,
    x: pd.Series,
    beta: float | pd.Series,
) -> pd.Series:
    """Compute the spread residual:  e = y - beta * x."""
    if isinstance(beta, pd.Series):
        return y - beta * x
    return y - float(beta) * x


def residual_zscore(
    residual: pd.Series,
    window: int = 252,
) -> pd.Series:
    """Rolling z-score of the spread residual."""
    mu = residual.rolling(window=window, min_periods=window).mean()
    sigma = residual.rolling(window=window, min_periods=window).std()
    return (residual - mu) / sigma.replace(0, np.nan)


def half_life(residual: pd.Series) -> float:
    """Estimate the mean-reversion half-life of a residual series.

    Fits AR(1) and returns -ln(2) / ln(|phi|).
    """
    clean = residual.dropna()
    if len(clean) < 20:
        return float("inf")

    lagged = clean.shift(1).dropna()
    current = clean.loc[lagged.index]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        slope, _, _, _, _ = stats.linregress(lagged, current)

    phi = abs(slope)
    if phi >= 1.0 or phi <= 0:
        return float("inf")
    return -np.log(2) / np.log(phi)


def cointegration_test(
    y: pd.Series,
    x: pd.Series,
    *,
    window: int = 252,
    significance: float = 0.05,
) -> dict[str, Any]:
    """Test for cointegration using augmented Engle-Granger.

    Returns dict with ``is_cointegrated``, ``adf_stat``, ``p_value``,
    ``hedge_beta``, ``half_life``.
    """
    from statsmodels.tsa.stattools import adfuller

    common = y.index.intersection(x.index)
    if len(common) < window:
        return {"is_cointegrated": False, "error": "insufficient_data"}

    y_c = y.reindex(common).tail(window).dropna()
    x_c = x.reindex(common).tail(window).dropna()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        slope, intercept, _, _, _ = stats.linregress(x_c, y_c)

    resid = y_c - intercept - slope * x_c
    adf = adfuller(resid.dropna(), maxlag=int((len(resid) - 1) ** (1 / 3)))
    hl = half_life(resid)

    return {
        "is_cointegrated": bool(adf[1] < significance),
        "adf_stat": float(adf[0]),
        "p_value": float(adf[1]),
        "hedge_beta": float(slope),
        "hedge_alpha": float(intercept),
        "half_life": float(hl),
        "n": len(resid),
    }

import warnings  # noqa: E402
