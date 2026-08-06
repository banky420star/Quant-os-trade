"""Pairs-trading research with rolling hedge ratios and cointegration tests."""

from __future__ import annotations

import warnings
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats


def rolling_hedge_ratio(y: pd.Series, x: pd.Series, window: int = 252) -> pd.DataFrame:
    """Estimate rolling OLS hedge-ratio coefficients for aligned series."""
    common_idx = y.index.intersection(x.index)
    if len(common_idx) < window:
        return pd.DataFrame({"alpha": np.nan, "beta": np.nan, "r_squared": np.nan}, index=common_idx)
    y_aligned = y.reindex(common_idx)
    x_aligned = x.reindex(common_idx)
    alphas = pd.Series(np.nan, index=common_idx)
    betas = pd.Series(np.nan, index=common_idx)
    r2s = pd.Series(np.nan, index=common_idx)
    for i in range(window - 1, len(common_idx)):
        joint = pd.concat({"y": y_aligned.iloc[i-window+1:i+1], "x": x_aligned.iloc[i-window+1:i+1]}, axis=1).dropna()
        if len(joint) < 2:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            slope, intercept, r_value, _, _ = stats.linregress(joint["x"], joint["y"])
        alphas.iloc[i] = intercept
        betas.iloc[i] = slope
        r2s.iloc[i] = r_value ** 2
    return pd.DataFrame({"alpha": alphas, "beta": betas, "r_squared": r2s}, index=common_idx)


def spread_residual(y: pd.Series, x: pd.Series, beta: float | pd.Series) -> pd.Series:
    return y - beta * x if isinstance(beta, pd.Series) else y - float(beta) * x


def residual_zscore(residual: pd.Series, window: int = 252) -> pd.Series:
    mean = residual.rolling(window=window, min_periods=window).mean()
    std = residual.rolling(window=window, min_periods=window).std()
    return (residual - mean) / std.replace(0, np.nan)


def half_life(residual: pd.Series) -> float:
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


def cointegration_test(y: pd.Series, x: pd.Series, *, window: int = 252, significance: float = 0.05) -> dict[str, Any]:
    """Run an Engle-Granger cointegration test on jointly aligned observations."""
    from statsmodels.tsa.stattools import coint

    common = y.index.intersection(x.index)
    joint = pd.concat({"y": y.reindex(common), "x": x.reindex(common)}, axis=1).tail(window).dropna()
    if len(joint) < window:
        return {"is_cointegrated": False, "error": "insufficient_data"}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        slope, intercept, _, _, _ = stats.linregress(joint["x"], joint["y"])
        test_stat, p_value, _ = coint(joint["y"], joint["x"])
    residual = joint["y"] - intercept - slope * joint["x"]
    return {"is_cointegrated": bool(p_value < significance), "adf_stat": float(test_stat), "p_value": float(p_value), "hedge_beta": float(slope), "hedge_alpha": float(intercept), "half_life": float(half_life(residual)), "n": len(residual)}
