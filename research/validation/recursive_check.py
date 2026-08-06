"""Recursive expanding-window validation with label purging."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

_log = logging.getLogger(__name__)


def recursive_one_step_ahead(
    features: pd.DataFrame,
    target: pd.Series,
    model_factory: Callable[[], Any],
    *,
    min_train: int = 252,
    step: int = 20,
    purge: int = 0,
) -> pd.DataFrame:
    """Refit every ``step`` bars and predict the next block.

    ``purge`` removes the final training rows before each test block and should
    be at least as large as the forward label horizon.
    """
    if purge < 0:
        raise ValueError("purge must be non-negative")

    results = pd.DataFrame(
        {"prediction": np.nan, "actual": target}, index=features.index
    )

    for index in range(min_train, len(features), step):
        train_end = max(0, index - purge)
        train_features = features.iloc[:train_end]
        train_target = target.iloc[:train_end]
        test_end = min(index + step, len(features))
        test_features = features.iloc[index:test_end]

        clean_index = train_features.dropna().index.intersection(
            train_target.dropna().index
        )
        if len(clean_index) < 50:
            continue

        model = model_factory()
        try:
            model.fit(
                train_features.loc[clean_index], train_target.loc[clean_index]
            )
            predictions = model.predict(test_features.fillna(0.0))
            if isinstance(predictions, pd.DataFrame):
                prediction_values = (
                    predictions["expected_net_r"]
                    if "expected_net_r" in predictions
                    else predictions.iloc[:, 0]
                )
            else:
                prediction_values = predictions
            results.iloc[
                index:test_end, results.columns.get_loc("prediction")
            ] = np.asarray(prediction_values)
        except Exception:  # noqa: BLE001 - one fold must not abort the audit
            _log.warning("recursive fold at %s failed", index, exc_info=True)

    return results.dropna(subset=["prediction"])
