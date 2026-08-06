"""Parameter stability tests.

Per the roadmap: test whether nearby parameter values behave similarly.
A strategy with a narrow, lucky peak is fragile; a broad plateau is
more likely to generalise.
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd


def neighbourhood_stability(
    base_params: dict[str, float],
    signal_fn: Callable[..., pd.Series],
    prices: pd.DataFrame,
    *,
    perturbations: float = 0.10,
    n_samples: int = 20,
    metric: str = "sharpe",
) -> dict[str, Any]:
    """
    Measure metric stability around each base parameter value.
    
    Parameters:
        base_params (dict[str, float]): Base parameter values to perturb individually.
        signal_fn (Callable[..., pd.Series]): Function that generates a signal from prices and parameter values.
        prices (pd.DataFrame): Price data containing a ``close`` column.
        perturbations (float): Maximum relative perturbation applied in either direction.
        n_samples (int): Number of perturbation trials per parameter.
        metric (str): Metric to calculate: ``"sharpe"`` for annualized Sharpe ratio; any other value selects cumulative return.
    
    Returns:
        dict[str, Any]: Statistics for each parameter with valid trials, including its base value, mean, standard deviation, minimum, maximum, and valid sample count.
    """
    from copy import deepcopy

    results: dict[str, dict[str, Any]] = {}

    for param, base_val in base_params.items():
        values: list[float] = []
        for _ in range(n_samples):
            noise = np.random.uniform(-perturbations, perturbations) * abs(base_val)
            trial = deepcopy(base_params)
            trial[param] = base_val + noise

            try:
                signal = signal_fn(prices, **trial)
            except Exception:
                continue

            rets = prices["close"].pct_change()
            strat_ret = (rets * signal.shift(1)).dropna()
            if len(strat_ret) < 30:
                continue

            if metric == "sharpe":
                val = float(strat_ret.mean() / strat_ret.std() * np.sqrt(252)) if strat_ret.std() > 0 else 0
            else:
                val = float((1 + strat_ret).prod() - 1)

            values.append(val)

        if values:
            results[param] = {
                "base_value": base_val,
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
    """
    Evaluate each parameter combination and return the valid results as a DataFrame.
    
    Parameters:
    	param_grid (dict[str, list[float]]): Parameter names mapped to the values to evaluate.
    	signal_fn (Callable[..., pd.Series]): Function that generates a trading signal for a parameter combination.
    	prices (pd.DataFrame): Price data containing a ``close`` column.
    	metric (str): Metric to compute, either ``"sharpe"`` or cumulative return.
    
    Returns:
    	pd.DataFrame: A row for each valid parameter combination, including the computed metric. Combinations that fail signal generation or produce fewer than 30 strategy-return observations are omitted.
    """
    from itertools import product

    rows: list[dict[str, Any]] = []
    keys = list(param_grid.keys())
    values_list = list(param_grid.values())

    for combo in product(*values_list):
        trial = dict(zip(keys, combo))
        try:
            signal = signal_fn(prices, **trial)
        except Exception:
            continue

        rets = prices["close"].pct_change()
        strat_ret = (rets * signal.shift(1)).dropna()
        if len(strat_ret) < 30:
            continue

        if metric == "sharpe":
            trial[metric] = float(strat_ret.mean() / strat_ret.std() * np.sqrt(252)) if strat_ret.std() > 0 else 0
        else:
            trial[metric] = float((1 + strat_ret).prod() - 1)

        rows.append(trial)

    return pd.DataFrame(rows)
