"""Purged walk-forward validation with temporal buffers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

_log = logging.getLogger(__name__)


@dataclass
class WalkForwardFold:
    fold: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def train_days(self) -> int:
        return (self.train_end - self.train_start).days

    @property
    def test_days(self) -> int:
        return (self.test_end - self.test_start).days


def _generate_folds(start: pd.Timestamp, end: pd.Timestamp, *, train_years: float = 2.0, test_months: int = 6, purge_days: int = 5, min_train_days: int = 252) -> list[WalkForwardFold]:
    folds: list[WalkForwardFold] = []
    train_delta = pd.Timedelta(days=int(train_years * 365.25))
    test_delta = pd.DateOffset(months=test_months)
    purge_delta = pd.Timedelta(days=purge_days)
    fold = 0
    test_start = start + train_delta
    while test_start + test_delta <= end:
        train_end = test_start - purge_delta
        if (train_end - start).days < min_train_days:
            test_start += test_delta
            continue
        folds.append(WalkForwardFold(fold, start, train_end, test_start, min(test_start + test_delta, end)))
        test_start += test_delta
        fold += 1
    return folds


def walk_forward_report(folds: list[WalkForwardFold]) -> dict[str, Any]:
    if not folds:
        return {}
    returns = [fold.metrics["total_return"] for fold in folds]
    positive = sum(value > 0 for value in returns)
    return {"folds": len(folds), "positive_folds": positive, "positive_fraction": round(positive / len(folds), 3), "median_oos_return": round(float(np.median(returns)), 6), "mean_oos_return": round(float(np.mean(returns)), 6), "oos_returns": [round(value, 6) for value in returns]}


def purged_walk_forward(prices: pd.DataFrame, signal_fn: Callable[[pd.DataFrame], pd.Series], *, train_years: float = 2.0, test_months: int = 6, purge_days: int = 5) -> dict[str, Any]:
    folds = _generate_folds(prices.index.min(), prices.index.max(), train_years=train_years, test_months=test_months, purge_days=purge_days)
    for fold in folds:
        train_df = prices.loc[fold.train_start:fold.train_end]
        test_df = prices.loc[fold.test_start:fold.test_end]
        if len(train_df) < 252 or len(test_df) < 20:
            continue
        full_df = prices.loc[fold.train_start:fold.test_end]
        try:
            signal = signal_fn(full_df)
        except Exception:
            _log.warning("walk-forward fold %s failed", fold.fold, exc_info=True)
            continue
        if not isinstance(signal, pd.Series):
            continue
        signal = signal.reindex(test_df.index).fillna(0.0)
        test_ret = test_df["close"].pct_change().fillna(0.0)
        strat_ret = (test_ret * signal.shift(1).fillna(0.0)).dropna()
        fold.metrics["total_return"] = float((1 + strat_ret).prod() - 1)
        fold.metrics["sharpe"] = float(strat_ret.mean() / strat_ret.std() * np.sqrt(252)) if strat_ret.std() > 0 else 0.0
        equity = (1 + strat_ret).cumprod()
        fold.metrics["max_drawdown"] = float((equity / equity.cummax() - 1).min())
    evaluated = [fold for fold in folds if "total_return" in fold.metrics]
    report = walk_forward_report(evaluated)
    report["skipped_folds"] = len(folds) - len(evaluated)
    return report
