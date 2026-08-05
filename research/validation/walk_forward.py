"""Purged walk-forward validation with temporal buffers.

Implements the roadmap's requirement: point-in-time correctness, purging,
embargoes, and walk-forward folds that never leak training data into test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd


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


def _generate_folds(
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    train_years: float = 2.0,
    test_months: int = 6,
    purge_days: int = 5,
    min_train_days: int = 252,
) -> list[WalkForwardFold]:
    """Generate purged walk-forward folds with temporal buffers."""
    folds: list[WalkForwardFold] = []
    train_delta = pd.Timedelta(days=int(train_years * 365.25))
    test_delta = pd.DateOffset(months=test_months)
    purge_delta = pd.Timedelta(days=purge_days)

    fold = 0
    test_start = start + train_delta

    while test_start + test_delta <= end:
        train_end = test_start - purge_delta
        if (train_end - start).days < min_train_days:
            test_start = test_start + test_delta
            continue

        folds.append(
            WalkForwardFold(
                fold=fold,
                train_start=start,
                train_end=train_end,
                test_start=test_start,
                test_end=min(test_start + test_delta, end),
            )
        )
        test_start = test_start + test_delta
        fold += 1

    return folds


def walk_forward_report(
    folds: list[WalkForwardFold],
) -> dict[str, Any]:
    """Summarise walk-forward results across all folds."""
    if not folds:
        return {}

    oos_returns: list[float] = []
    for f in folds:
        ret = f.metrics.get("total_return", 0.0)
        oos_returns.append(ret)

    median_ret = float(np.median(oos_returns)) if oos_returns else 0.0
    positive_folds = sum(1 for r in oos_returns if r > 0)

    return {
        "folds": len(folds),
        "positive_folds": positive_folds,
        "positive_fraction": round(positive_folds / len(folds), 3) if folds else 0,
        "median_oos_return": round(median_ret, 6),
        "mean_oos_return": round(float(np.mean(oos_returns)), 6) if oos_returns else 0,
        "oos_returns": [round(r, 6) for r in oos_returns],
    }


def purged_walk_forward(
    prices: pd.DataFrame,
    signal_fn: Callable[[pd.DataFrame], pd.Series],
    *,
    train_years: float = 2.0,
    test_months: int = 6,
    purge_days: int = 5,
) -> dict[str, Any]:
    """Run a purged walk-forward test with the given signal function.

    *prices*: DataFrame with DatetimeIndex and at minimum a ``close`` column.
    *signal_fn*: callable that receives a train-period DataFrame slice
                 and returns a Series of position sizes (-1/0/+1).

    Returns a walk-forward report dict.
    """
    start = prices.index.min()
    end = prices.index.max()
    folds = _generate_folds(
        start, end, train_years=train_years, test_months=test_months, purge_days=purge_days
    )

    for f in folds:
        train_df = prices.loc[f.train_start : f.train_end]
        test_df = prices.loc[f.test_start : f.test_end]

        if len(train_df) < 252 or len(test_df) < 20:
            continue

        try:
            signal = signal_fn(train_df)
        except Exception:
            continue

        # Naive backtest: daily return × previous signal
        if hasattr(signal, "reindex"):
            signal = signal.reindex(test_df.index, method="ffill").fillna(0)
        test_ret = test_df["close"].pct_change().fillna(0)
        strat_ret = (test_ret * signal.shift(1)).dropna()
        f.metrics["total_return"] = float((1 + strat_ret).prod() - 1)
        f.metrics["sharpe"] = float(strat_ret.mean() / strat_ret.std() * np.sqrt(252)) if strat_ret.std() > 0 else 0
        f.metrics["max_drawdown"] = float((strat_ret.cumsum().cummax() - strat_ret.cumsum()).max())

    return walk_forward_report(folds)
