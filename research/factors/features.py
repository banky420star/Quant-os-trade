"""Point-in-time feature computation for research models."""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_momentum_features(
    df: pd.DataFrame,
    *,
    horizons: tuple[int, ...] = (5, 20, 60, 120),
    high_col: str = "high",
) -> pd.DataFrame:
    """Return horizon returns and distance from the trailing 252-bar high."""
    close = df["close"]
    high = df[high_col]
    out = pd.DataFrame(index=df.index)
    for horizon in horizons:
        if len(close) > horizon:
            out[f"return_{horizon}d"] = close.pct_change(periods=horizon)
    out["dist_52w_high"] = (
        close - high.rolling(window=252, min_periods=1).max()
    ) / close.replace(0, np.nan)
    return out


def compute_risk_features(
    df: pd.DataFrame,
    *,
    vol_lookback: int = 20,
    dd_lookback: int = 252,
    gap_threshold: float = 1e-4,
) -> pd.DataFrame:
    """Return volatility, downside risk, drawdown, and relative gap features."""
    close = df["close"]
    returns = close.pct_change()
    out = pd.DataFrame(index=df.index)
    out["volatility_20d"] = returns.rolling(vol_lookback, min_periods=5).std()
    out["downside_vol"] = (
        returns.where(returns < 0).rolling(vol_lookback, min_periods=5).std()
    )
    rolling_max = close.rolling(dd_lookback, min_periods=1).max()
    out["current_drawdown"] = (close - rolling_max) / rolling_max.replace(0, np.nan)

    previous_close = close.shift(1)
    relative_gap = ((df["open"] - previous_close) / previous_close).abs()
    gaps = relative_gap.gt(gap_threshold).astype(float)
    gaps[previous_close.isna()] = np.nan
    out["gap_frequency"] = gaps.rolling(vol_lookback, min_periods=5).mean()
    return out


def compute_trend_features(df: pd.DataFrame) -> pd.DataFrame:
    """Return moving-average trend and breakout features."""
    close = df["close"]
    high = df["high"]
    low = df["low"]
    out = pd.DataFrame(index=df.index)
    ma_50 = close.rolling(50, min_periods=5).mean()
    ma_200 = close.rolling(200, min_periods=5).mean()
    out["ma_slope_50"] = ma_50.diff(5) / ma_50.shift(5)
    out["ma_slope_200"] = ma_200.diff(20) / ma_200.shift(20)
    out["trend_agreement"] = ((close > ma_50) & (ma_50 > ma_200)).astype(float)
    high_20 = high.rolling(20, min_periods=5).max()
    low_20 = low.rolling(20, min_periods=5).min()
    out["breakout_distance"] = (close - high_20) / (high_20 - low_20).replace(0, np.nan)
    return out


def compute_cost_features(
    df: pd.DataFrame,
    *,
    spread_col: str = "spread",
    point: float | None = None,
) -> pd.DataFrame:
    """Return spread-relative cost features when spread data is available."""
    out = pd.DataFrame(index=df.index)
    atr = (df["high"] - df["low"]).rolling(14, min_periods=5).mean()
    if spread_col in df.columns:
        spread = df[spread_col] * point if point else df[spread_col]
        out["spread_atr"] = spread / atr.replace(0, np.nan)
        out["spread_avg_20"] = spread.rolling(20, min_periods=5).mean()
    return out


def compute_cross_market_features(
    prices: dict[str, pd.DataFrame],
    target_symbol: str,
) -> pd.DataFrame:
    """Return rolling return correlations against the target symbol."""
    target_close = prices[target_symbol]["close"]
    out = pd.DataFrame(index=target_close.index)
    for symbol, frame in prices.items():
        if symbol == target_symbol:
            continue
        common = target_close.index.intersection(frame.index)
        if len(common) < 20:
            continue
        target_returns = target_close.reindex(common).pct_change()
        other_returns = frame["close"].reindex(common).pct_change()
        out[f"corr_{symbol}"] = target_returns.rolling(20, min_periods=10).corr(
            other_returns
        ).reindex(target_close.index)
    return out


def feature_dataframe(
    df: pd.DataFrame,
    *,
    with_momentum: bool = True,
    with_risk: bool = True,
    with_trend: bool = True,
    with_cost: bool = True,
) -> pd.DataFrame:
    """Assemble selected feature groups into one index-aligned matrix."""
    parts: list[pd.DataFrame] = []
    if with_momentum:
        parts.append(compute_momentum_features(df))
    if with_risk:
        parts.append(compute_risk_features(df))
    if with_trend:
        parts.append(compute_trend_features(df))
    if with_cost:
        parts.append(compute_cost_features(df))
    return pd.concat(parts, axis=1) if parts else pd.DataFrame(index=df.index)
