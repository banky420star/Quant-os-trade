"""Deterministic parameter-stability tests for research strategies."""

from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy
from itertools import product
from typing import Any

import numpy as np
import pandas as pd

_log = logging.getLogger(__name__)


def _metric(strategy_returns: pd.Series, metric: str) -> float:
    """Return the requested metric for an already aligned return series."""
    if metric == "sharpe":
        volatility = float(strategy_returns.std())
        return (
            float(strategy_returns.mean() / volatility * np.sqrt(252))
            if volatility > 0
            else 0.0
        )
    return float((1.0 + strategy_returns).prod() - 1.0)


def neighbourhood_stability(
    base_params: dict[str, float],
    signal_fn: Callable[..., pd.Series],
    prices: pd.DataFrame,
    *,
    perturbations: float = 0.10,
    n_samples: int = 20,
    metric: str = "sharpe",
    seed: int | None = 0,
) -> dict[str, Any]:
    """Measure nearby-parameter stability using reproducible perturbations."""
    rng = np.random.default_rng(seed)
    returns = prices["close"].pct_change()
    results: dict[str, dict[str, Any]] = {}

    for parameter, base_value in base_params.items():
        values: list[float] = []
        scale = abs(base_value) if base_value != 0 else 1.0
        for _ in range(n_samples):
            trial_value = base_value + rng.uniform(-perturbations, perturbations) * scale
            trial = deepcopy(base_params)
            trial[parameter] = trial_value
            try:
                signal = signal_fn(prices, **trial)
            except Exception:  # noqa: BLE001 - retain remaining research trials
                _log.warning(
                    "parameter trial failed: %s=%r",
                    parameter,
                    trial_value,
                    exc_info=True,
                )
                continue
            strategy_returns = (returns * signal.shift(1)).dropna()
            if len(strategy_returns) >= 30:
                values.append(_metric(strategy_returns, metric))

        if values:
            results[parameter] = {
                "base_value": base_value,
                "mean": float(np.mean(values)),
                "std": float(np.std(values)),
                "min": float(np.min(values)),
                "max": float(np.max(values)),
                "samples": len(values),
            }
    return results


def parameter_surface(
    param_grid: dict[str, list[float]],
    signal_fn: Callable[..., pd.Series],
    prices: pd.DataFrame,
    *,
    metric: str = "sharpe",
) -> pd.DataFrame:
    """Evaluate the full parameter grid and return successful trials."""
    rows: list[dict[str, Any]] = []
    keys = list(param_grid)
    values_list = [param_grid[key] for key in keys]
    returns = prices["close"].pct_change()

    for combination in product(*values_list):
        trial = dict(zip(keys, combination, strict=True))
        try:
            signal = signal_fn(prices, **trial)
        except Exception:  # noqa: BLE001 - retain remaining research trials
            _log.warning("parameter grid trial failed: %r", trial, exc_info=True)
            continue
        strategy_returns = (returns * signal.shift(1)).dropna()
        if len(strategy_returns) < 30:
            continue
        trial[metric] = _metric(strategy_returns, metric)
        rows.append(trial)
    return pd.DataFrame(rows)
