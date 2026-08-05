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
    """Recursive one-step-ahead predictions.

    *features*: DataFrame with DatetimeIndex.
    *target*: aligned target Series.
    *model_factory*: zero-arg callable returning a scikit-learn-style
                     model with ``fit(X, y)`` and ``predict(X)``.
    *min_train*: minimum training observations before predicting.
    *step*: refit the model every *step* bars.

    Returns DataFrame with ``prediction`` and ``actual`` columns.
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
