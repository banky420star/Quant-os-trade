"""
Quant OS Research Laboratory.

Phase 1 scaffolding per the 2026-08-05 strategic roadmap.
Modules are organised by function:

    data/         — MT5 history export, point-in-time catalog, quality checks
    costs/        — spread, swap, slippage models, broker-native risk
    strategies/   — trend baselines, pairs, basket residuals
    factors/      — feature engineering, label construction, ranker models
    validation/   — walk-forward, lookahead, recursive, parameter-stability tests
    artifacts/    — strategy registry, versioned schema

All modules operate on Parquet / CSV files read from ``data/history/``.
No module in this package may call ``mt5.order_send()`` or directly mutate
a live account.  The research plane is read-only with respect to MT5.
"""

__version__ = "0.1.0"
__all__ = [
    "data",
    "costs",
    "strategies",
    "factors",
    "validation",
    "artifacts",
]
