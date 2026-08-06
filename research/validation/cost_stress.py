"""Cost stress tests.

Per the roadmap's acceptance conditions: the strategy must remain
positive after doubled estimated costs.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def double_cost_test(
    returns: pd.Series,
    *,
    base_cost_bps: float = 2.0,
) -> dict[str, Any]:
    """
    Assess whether a strategy remains profitable when transaction costs are doubled.
    
    Parameters:
        returns (pd.Series): Daily strategy returns.
        base_cost_bps (float): One-way transaction cost in basis points.
    
    Returns:
        dict[str, Any]: A dictionary containing rounded base and stressed returns,
            a boolean indicating whether the stressed return is positive, and the
            configured base cost in basis points.
    """
    base_cost = base_cost_bps / 10_000  # bps → decimal
    double_cost = 2 * base_cost

    # Assume one trade per day per signal flip
    trades = (returns != 0).astype(int).diff().abs().fillna(0)
    cost_series_base = trades * base_cost
    cost_series_double = trades * double_cost

    net_base = returns - cost_series_base
    net_double = returns - cost_series_double

    base_total = float((1 + net_base).prod() - 1)
    stressed_total = float((1 + net_double).prod() - 1)

    return {
        "base_return": round(base_total, 6),
        "stressed_return": round(stressed_total, 6),
        "survives": stressed_total > 0,
        "cost_bps": base_cost_bps,
    }


def spread_stress_test(
    prices: dict[str, pd.DataFrame],
    signal_fn: Any,
    *,
    spread_multipliers: tuple[float, ...] = (1.0, 2.0, 3.0),
) -> dict[str, dict[float, float]]:
    """
    Evaluate compounded net returns under configurable spread-cost multipliers.
    
    Parameters:
        prices (dict[str, pd.DataFrame]): Symbol-indexed price data containing a
            ``close`` column.
        signal_fn (Any): Accepted for interface compatibility; it is not used.
        spread_multipliers (tuple[float, ...]): Multipliers applied to the
            one-basis-point spread cost.
    
    Returns:
        dict[str, dict[float, float]]: Rounded net returns grouped by symbol and
            spread multiplier.
    """
    results: dict[str, dict[float, float]] = {}

    for sym, df in prices.items():
        close = df["close"]
        rets = close.pct_change().dropna()

        results[sym] = {}
        for mult in spread_multipliers:
            cost = 0.0001 * mult  # 1 bp × multiplier
            trades = (rets != 0).astype(int).diff().abs().fillna(0)
            net = rets - trades * cost
            results[sym][mult] = round(float((1 + net).prod() - 1), 6)

    return results
