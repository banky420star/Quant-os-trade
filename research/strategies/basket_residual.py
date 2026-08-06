"""Basket residual research using PCA reconstruction and multivariate OLS."""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA

from .pairs import residual_zscore


def pca_residual(
    returns: pd.DataFrame,
    *,
    n_components: int = 2,
    window: int = 252,
) -> pd.DataFrame:
    """Return rolling PCA reconstruction residuals for a stable column universe."""
    columns = list(returns.columns)
    result = pd.DataFrame(
        np.nan,
        index=returns.index,
        columns=[f"residual_{column}" for column in columns],
    )
    if len(returns) < window or len(columns) < 2:
        return result

    components = min(n_components, len(columns) - 1)
    for position in range(window - 1, len(returns)):
        sample = returns.iloc[position - window + 1 : position + 1][columns]
        if sample.isna().any().any():
            continue
        model = PCA(n_components=components)
        scores = model.fit_transform(sample.to_numpy(dtype=float))
        reconstructed = model.inverse_transform(scores)
        residual = sample.to_numpy(dtype=float)[-1] - reconstructed[-1]
        result.iloc[position] = residual
    return result


def basket_residual_zscore(
    target: pd.Series,
    basket: pd.DataFrame,
    *,
    window: int = 252,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
) -> pd.DataFrame:
    """Return residual, z-score, and signal from rolling multivariate regression."""
    target_returns = target.pct_change()
    basket_returns = basket.pct_change()
    joint = pd.concat(
        {"target": target_returns, **{column: basket_returns[column] for column in basket_returns}},
        axis=1,
    ).dropna()
    if len(joint) < window or basket.empty:
        return pd.DataFrame(index=joint.index)

    residuals = pd.Series(np.nan, index=joint.index, dtype=float)
    feature_columns = [column for column in joint.columns if column != "target"]
    for position in range(window - 1, len(joint)):
        sample = joint.iloc[position - window + 1 : position + 1]
        design = np.column_stack(
            [np.ones(len(sample)), sample[feature_columns].to_numpy(dtype=float)]
        )
        response = sample["target"].to_numpy(dtype=float)
        coefficients, *_ = np.linalg.lstsq(design, response, rcond=None)
        latest = np.r_[1.0, sample[feature_columns].iloc[-1].to_numpy(dtype=float)]
        residuals.iloc[position] = response[-1] - float(latest @ coefficients)

    zscore = residual_zscore(residuals, window=window)
    signal = pd.Series(0, index=joint.index, dtype=int)
    signal[zscore > entry_z] = -1
    signal[zscore < -entry_z] = 1
    signal[zscore.abs() < exit_z] = 0
    return pd.DataFrame(
        {"residual": residuals, "zscore": zscore, "signal": signal},
        index=joint.index,
    )
