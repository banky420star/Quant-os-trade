"""Shared cost, periodization, and portfolio helpers for trend research.

This module is research-only. It centralizes assumptions used by the trend MVP
and walk-forward runner so both reports use identical costs and portfolio caps.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

COST_BPS: dict[str, float] = {
    "US500m": 1.5,
    "US30m": 2.0,
    "NAS100m": 2.5,
    "UK100m": 2.5,
    "FR40m": 2.5,
    "JP225m": 3.0,
    "EURUSDm": 0.3,
    "GBPUSDm": 0.5,
    "USDJPYm": 0.4,
    "USDCHFm": 0.5,
    "AUDUSDm": 0.6,
    "XAUUSDm": 2.0,
    "USOILm": 3.0,
    "BTCUSDm": 5.0,
}

SWAP_BPS_DAILY: dict[str, float] = {
    "XAUUSDm": 0.15,
    "USOILm": 0.40,
    "EURUSDm": 0.05,
    "GBPUSDm": 0.04,
    "USDJPYm": 0.06,
    "USDCHFm": 0.03,
    "AUDUSDm": 0.04,
    "US500m": 0.08,
    "US30m": 0.10,
    "NAS100m": 0.12,
    "UK100m": 0.08,
    "FR40m": 0.08,
    "JP225m": 0.10,
    "BTCUSDm": 1.0,
}

SLIPPAGE_BPS: float = 0.5
BARS_PER_DAY: dict[str, float] = {"D1": 1.0, "H4": 6.0}

CLUSTERS: dict[str, list[str]] = {
    "equity_indices": ["US500m", "US30m", "NAS100m", "UK100m", "FR40m", "JP225m"],
    "fx_usd_majors": ["EURUSDm", "GBPUSDm", "AUDUSDm", "USDCHFm", "USDJPYm"],
    "metals": ["XAUUSDm"],
    "energy": ["USOILm"],
    "crypto": ["BTCUSDm"],
}
CLUSTER_CAPS: dict[str, float] = {
    "equity_indices": 0.35,
    "fx_usd_majors": 0.35,
    "metals": 0.15,
    "energy": 0.15,
    "crypto": 0.10,
}
SINGLE_SYMBOL_CAP: float = 0.15
VOL_TARGET: float = 0.15
VOL_LOOKBACK: int = 63


def cost_per_trade_bps(symbol: str) -> float:
    """Return estimated round-trip spread and slippage in basis points."""
    return COST_BPS.get(symbol, 2.0) + 2 * SLIPPAGE_BPS


def daily_swap_bps(symbol: str) -> float:
    """Return estimated daily holding cost in basis points."""
    return SWAP_BPS_DAILY.get(symbol, 0.05)


def bars_per_day(timeframe: str) -> float:
    """Return the assumed number of bars per trading day."""
    return BARS_PER_DAY.get(timeframe, 1.0)


def periods_per_year(timeframe: str) -> float:
    """Return annualization periods for the timeframe."""
    return 252.0 * bars_per_day(timeframe)


def equivalent_horizons(
    timeframe: str,
    daily_horizons: tuple[int, ...] = (21, 63, 126, 252),
) -> tuple[int, ...]:
    """Convert daily-bar horizons to equivalent horizons for a timeframe."""
    multiplier = int(round(bars_per_day(timeframe)))
    return tuple(max(1, horizon * multiplier) for horizon in daily_horizons)


def symbol_cluster(symbol: str) -> str:
    """Return the configured correlation cluster for a symbol."""
    for name, members in CLUSTERS.items():
        if symbol in members:
            return name
    return "other"


def net_returns(
    close: pd.Series,
    signal: pd.Series,
    symbol: str,
    *,
    cost_mult: float = 1.0,
    timeframe: str = "D1",
) -> pd.Series:
    """Return strategy returns after transaction and per-bar holding costs."""
    aligned_signal = signal.reindex(close.index).fillna(0.0)
    gross = close.pct_change().mul(aligned_signal.shift(1)).fillna(0.0)

    flip_mask = aligned_signal.diff().fillna(0.0).ne(0.0)
    transaction_cost = flip_mask.astype(float) * (
        cost_per_trade_bps(symbol) * cost_mult / 10_000
    )

    per_bar_swap = (
        daily_swap_bps(symbol)
        * cost_mult
        / bars_per_day(timeframe)
        / 10_000
    )
    holding_cost = aligned_signal.abs().shift(1).fillna(0.0) * per_bar_swap
    return gross - transaction_cost - holding_cost


def capped_vol_weights(
    volatilities: dict[str, float],
    *,
    vol_target: float = VOL_TARGET,
    single_symbol_cap: float = SINGLE_SYMBOL_CAP,
    cluster_caps: dict[str, float] | None = None,
) -> dict[str, float]:
    """Build normalized inverse-vol weights, then apply symbol and cluster caps."""
    caps = cluster_caps or CLUSTER_CAPS
    raw = {
        symbol: (vol_target / vol if np.isfinite(vol) and vol > 0 else 0.0)
        for symbol, vol in volatilities.items()
    }
    total = sum(raw.values())
    if total > 0:
        raw = {symbol: weight / total for symbol, weight in raw.items()}

    weights = {
        symbol: min(max(weight, 0.0), single_symbol_cap)
        for symbol, weight in raw.items()
    }
    cluster_totals: dict[str, float] = {}
    for symbol in sorted(weights, key=weights.get, reverse=True):
        cluster = symbol_cluster(symbol)
        used = cluster_totals.get(cluster, 0.0)
        cap = caps.get(cluster, single_symbol_cap)
        weights[symbol] = min(weights[symbol], max(0.0, cap - used))
        cluster_totals[cluster] = used + weights[symbol]
    return weights


def portfolio_metrics(
    returns: pd.Series,
    *,
    timeframe: str = "D1",
) -> dict[str, Any]:
    """Calculate compounded, timeframe-aware portfolio performance metrics."""
    clean = returns.dropna()
    if len(clean) < 20:
        return {"error": f"only {len(clean)} returns"}

    equity = (1.0 + clean).cumprod()
    total_return = float(equity.iloc[-1] - 1.0)
    drawdown = float((equity / equity.cummax() - 1.0).min())
    periods = periods_per_year(timeframe)
    volatility = float(clean.std())
    sharpe = float(clean.mean() / volatility * np.sqrt(periods)) if volatility > 0 else 0.0
    annual_return = float((1.0 + total_return) ** (periods / len(clean)) - 1.0)

    return {
        "n_periods": len(clean),
        "total_return": round(total_return, 6),
        "annual_return": round(annual_return, 6),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(drawdown, 4),
        "win_period_pct": round(float((clean > 0).mean()), 4),
        "avg_period_return": round(float(clean.mean()), 8),
        "vol_period": round(volatility, 6),
        "vol_annual": round(volatility * np.sqrt(periods), 4),
    }
