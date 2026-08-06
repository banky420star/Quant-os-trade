"""Strategy implementations — trend baselines, pairs, basket residuals.

Every strategy module outputs a ``StrategyIntent``-like dict with
symbol, side, entry, stop, target, and metadata.  No module in this
subpackage may call ``mt5.order_send()``.
"""

from .basket_residual import basket_residual_zscore, pca_residual
from .pairs import cointegration_test, rolling_hedge_ratio
from .trend_baseline import (
    blended_momentum_signal,
    moving_average_signal,
    time_series_momentum_signal,
)
from .trend_blended import blended_daily_signal

__all__ = [
    "basket_residual_zscore",
    "blended_daily_signal",
    "blended_momentum_signal",
    "cointegration_test",
    "moving_average_signal",
    "pca_residual",
    "rolling_hedge_ratio",
    "time_series_momentum_signal",
]
