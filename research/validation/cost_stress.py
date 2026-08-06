"""Cost stress tests for research-only strategy validation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd


def _aligned_positions(positions: pd.Series, index: pd.Index) -> pd.Series:
    """Align a position series to a returns index without looking ahead."""
    return positions.reindex(index).ffill().fillna(0.0).astype(float)


def double_cost_test(
    returns: pd.Series,
    positions: pd.Series | None = None,
    *,
    base_cost_bps: float = 2.0,
) -> dict[str, Any]:
    """Test strategy returns at normal and doubled transaction costs.

    ``positions`` should contain the held position for each return observation.
    For backwards compatibility, non-zero returns are treated as active positions
    when no explicit position series is supplied.
    """
    positions = (returns != 0).astype(float) if positions is None else positions
    aligned = _aligned_positions(positions, returns.index)
    trades = aligned.diff().abs().fillna(aligned.abs())

    base_cost = base_cost_bps / 10_000
    net_base = returns - trades * base_cost
    net_double = returns - trades * (2 * base_cost)

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
    signal_fn: Callable[[pd.DataFrame], pd.Series],
    *,
    spread_multipliers: tuple[float, ...] = (1.0, 2.0, 3.0),
) -> dict[str, dict[float, float]]:
    """Evaluate strategy returns under configurable spread multipliers."""
    results: dict[str, dict[float, float]] = {}

    for symbol, frame in prices.items():
        close = frame["close"]
        returns = close.pct_change().fillna(0.0)
        positions = signal_fn(frame)
        if not isinstance(positions, pd.Series):
            raise TypeError(f"signal_fn must return a Series for {symbol}")
        aligned = _aligned_positions(positions, returns.index)
        gross = returns * aligned.shift(1).fillna(0.0)
        trades = aligned.diff().abs().fillna(aligned.abs())

        results[symbol] = {}
        for multiplier in spread_multipliers:
            cost = 0.0001 * multiplier
            net = gross - trades * cost
            results[symbol][multiplier] = round(
                float((1 + net).prod() - 1), 6
            )

    return results
