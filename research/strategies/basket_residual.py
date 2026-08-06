"""Basket residual research — PCA residuals and replicating baskets.

Phase 4 extension: instead of one-to-one pairs, test whether a
multi-instrument replicating basket produces a more stable residual.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA


def pca_residual(
    returns: pd.DataFrame,
    *,
    n_components: int = 2,
    window: int = 252,
) -> pd.DataFrame:
    """
    Compute rolling principal-component scores for a basket of instrument returns.
    
    Parameters:
    	returns (pd.DataFrame): Instrument returns indexed by date.
    	n_components (int): Number of principal components to calculate.
    	window (int): Number of observations in each rolling window.
    
    Returns:
    	pd.DataFrame: Principal-component scores indexed like `returns`, with columns named `pc1_score`, `pc2_score`, and so on. Values remain missing where a valid score cannot be calculated.
    """
    if len(returns) < window:
        return pd.DataFrame(index=returns.index)

    residuals = pd.DataFrame(index=returns.index)
    pca = PCA(n_components=n_components)

    for i in range(window - 1, len(returns)):
        window_data = returns.iloc[i - window + 1 : i + 1].dropna(axis=1)
        if window_data.shape[1] < 2:
            continue

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            scores = pca.fit_transform(window_data)

        if i - window + 1 + scores.shape[0] - 1 < len(residuals):
            idx = returns.index[i]
            for j in range(min(n_components, scores.shape[1])):
                residuals.loc[idx, f"pc{j+1}_score"] = scores[-1, j]

    return residuals


def basket_residual_zscore(
    target: pd.Series,
    basket: pd.DataFrame,
    *,
    window: int = 252,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
) -> pd.DataFrame:
    """
    Compute basket-relative residual z-scores and trading signals.
    
    Parameters:
        target (pd.Series): Price series for the target instrument.
        basket (pd.DataFrame): Price series for the basket constituents.
        window (int): Number of common return observations used for rolling estimation and z-score calculation.
        entry_z (float): Absolute z-score threshold for entering a signal.
        exit_z (float): Absolute z-score threshold below which the signal is neutral.
    
    Returns:
        pd.DataFrame: DataFrame indexed by common return dates with `residual`, `zscore`, and `signal` columns. Signals are `-1` above `entry_z`, `1` below `-entry_z`, and `0` when the absolute z-score is below `exit_z`.
    """
    ret_target = target.pct_change().dropna()
    ret_basket = basket.pct_change().dropna()

    common_idx = ret_target.index.intersection(ret_basket.index)
    if len(common_idx) < window:
        return pd.DataFrame(index=common_idx)

    ret_target = ret_target.reindex(common_idx)
    ret_basket = ret_basket.reindex(common_idx)

    # Rolling OLS: target_return = alpha + betas * basket_returns
    residuals = pd.Series(np.nan, index=common_idx)

    for i in range(window - 1, len(common_idx)):
        yi = ret_target.iloc[i - window + 1 : i + 1].values
        xi = ret_basket.iloc[i - window + 1 : i + 1].values
        if xi.shape[1] == 0:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from scipy import stats as _stats
            betas, intercept, _, _, _ = _stats.linregress(
                xi if xi.ndim == 1 else xi.mean(axis=1), yi
            )
        pred = intercept + np.dot(xi[-1] if xi.ndim == 1 else xi[-1].mean(), betas)
        residuals.iloc[i] = yi[-1] - pred

    zscore = residual_zscore(residuals, window=window)
    signal = pd.Series(0, index=common_idx)
    signal[zscore > entry_z] = -1
    signal[zscore < -entry_z] = 1
    signal[zscore.abs() < exit_z] = 0

    return pd.DataFrame({"residual": residuals, "zscore": zscore, "signal": signal}, index=common_idx)


# re-use from pairs module
from .pairs import residual_zscore  # noqa: E402
import warnings  # noqa: E402
