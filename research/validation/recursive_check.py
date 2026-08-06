"""Recursive one-step-ahead validation.

Ports the Freqtrade-inspired recursive-analysis idea: at each bar,
re-fit the model on all data up to that bar and predict the next.
Catches parameter drift and overfitting that purged walk-forward
might miss.
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd


def recursive_one_step_ahead(
    features: pd.DataFrame,
    target: pd.Series,
    model_factory: Callable[[], Any],
    *,
    min_train: int = 252,
    step: int = 20,
) -> pd.DataFrame:
    """
    Generate expanding-window predictions with periodic model refitting.
    
    Parameters:
        features (pd.DataFrame): Feature observations indexed by row order.
        target (pd.Series): Target values aligned with `features`.
        model_factory (Callable[[], Any]): Callable that creates a model with
            `fit(X, y)` and `predict(X)` methods.
        min_train (int): Number of initial observations required before prediction
            begins.
        step (int): Number of observations in each prediction batch and interval
            between model refits.
    
    Returns:
        pd.DataFrame: Rows with available predictions, containing `prediction` and
            `actual` columns.
    """
    results = pd.DataFrame(
        {"prediction": np.nan, "actual": target},
        index=features.index,
    )

    model = model_factory()

    for i in range(min_train, len(features), step):
        train_X = features.iloc[:i]
        train_y = target.iloc[:i]
        test_end = min(i + step, len(features))
        test_X = features.iloc[i:test_end]

        clean_idx = train_X.dropna().index.intersection(train_y.dropna().index)
        if len(clean_idx) < 50:
            continue

        try:
            model.fit(train_X.loc[clean_idx], train_y.loc[clean_idx])
            preds = model.predict(test_X.fillna(0.0))
            results.iloc[i:test_end, results.columns.get_loc("prediction")] = preds
        except Exception:
            continue

    return results.dropna(subset=["prediction"])
