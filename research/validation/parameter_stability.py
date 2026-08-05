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
    """Perturb each parameter ±*perturbations* and record the metric.

    Returns ``{param: {mean, std, min, max, base_value, samples}}``.
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
    """Evaluate a grid of parameters and return a surface DataFrame.

    Each row = one parameter combination plus the computed metric.
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
