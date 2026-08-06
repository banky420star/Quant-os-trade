"""Feature computation — momentum, risk, trend, cost, cross-market, context.

All feature functions accept a DataFrame with OHLCV columns and return
a DataFrame (or Series) aligned with the input index.  No lookahead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ── momentum features ────────────────────────────────────────────────────────

def compute_momentum_features(
    df: pd.DataFrame,
    *,
    horizons: tuple[int, ...] = (5, 20, 60, 120),
    high_col: str = "high",
) -> pd.DataFrame:
    """
    Build momentum features for the requested lookback horizons.
    
    Parameters:
    	horizons (tuple[int, ...]): Lookback periods used to calculate close-price returns.
    	high_col (str): Column containing high prices used to calculate distance from the rolling 252-bar high.
    
    Returns:
    	pd.DataFrame: An index-aligned DataFrame containing return columns for eligible horizons and the distance from the rolling 252-bar high.
    """
    close = df["close"]
    high = df[high_col]
    out = pd.DataFrame(index=df.index)

    for h in horizons:
        if len(close) <= h:
            continue
        out[f"return_{h}d"] = close.pct_change(periods=h)
        out[f"dist_52w_high"] = (close - high.rolling(window=252, min_periods=1).max()) / close

    return out


# ── risk features ────────────────────────────────────────────────────────────

def compute_risk_features(
    df: pd.DataFrame,
    *,
    vol_lookback: int = 20,
    dd_lookback: int = 252,
) -> pd.DataFrame:
    """
    Compute volatility, downside volatility, drawdown, and price-gap frequency features.
    
    Parameters:
        vol_lookback (int): Window used for volatility and gap-frequency calculations.
        dd_lookback (int): Window used to determine the rolling maximum close for drawdown.
    
    Returns:
        pd.DataFrame: DataFrame indexed like ``df`` with columns for volatility, downside
            volatility, current drawdown, and gap frequency.
    """
    close = df["close"]
    rets = close.pct_change()
    out = pd.DataFrame(index=df.index)

    out["volatility_20d"] = rets.rolling(vol_lookback, min_periods=5).std()
    out["downside_vol"] = rets[rets < 0].rolling(vol_lookback, min_periods=5).std()
    rolling_max = close.rolling(dd_lookback, min_periods=1).max()
    out["current_drawdown"] = (close - rolling_max) / rolling_max

    # gap frequency: proportion of bars where open != previous close
    gaps = (df["open"] != close.shift(1)).astype(int)
    out["gap_frequency"] = gaps.rolling(vol_lookback, min_periods=5).mean()

    return out


# ── trend features ───────────────────────────────────────────────────────────

def compute_trend_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute moving-average trend and breakout-distance features from OHLC data.
    
    Returns:
    	pd.DataFrame: Index-aligned features containing relative 50- and 200-period moving-average slopes, a trend-agreement indicator, and close distance from the 20-period high normalized by its high-low range.
    """
    close = df["close"]
    high = df["high"]
    low = df["low"]
    out = pd.DataFrame(index=df.index)

    # MA slope
    ma_50 = close.rolling(50, min_periods=5).mean()
    ma_200 = close.rolling(200, min_periods=5).mean()
    out["ma_slope_50"] = ma_50.diff(5) / ma_50.shift(5)
    out["ma_slope_200"] = ma_200.diff(20) / ma_200.shift(20)
    out["trend_agreement"] = ((close > ma_50) & (ma_50 > ma_200)).astype(float)

    # Breakout distance: close vs 20-bar high/low range
    high_20 = high.rolling(20, min_periods=5).max()
    low_20 = low.rolling(20, min_periods=5).min()
    range_20 = (high_20 - low_20).replace(0, np.nan)
    out["breakout_distance"] = (close - high_20) / range_20

    return out


# ── cost features ────────────────────────────────────────────────────────────

def compute_cost_features(
    df: pd.DataFrame,
    *,
    spread_col: str = "spread",
    point: float | None = None,
) -> pd.DataFrame:
    """
    Compute trading-cost features from the high-low range and optional spread data.
    
    Parameters:
        spread_col (str): Name of the column containing spread values.
        point (float | None): Optional multiplier used to convert spread values into price units.
    
    Returns:
        pd.DataFrame: An index-aligned DataFrame containing spread relative to the
            14-period average high-low range and the 20-period average spread when
            the spread column is available.
    """
    out = pd.DataFrame(index=df.index)
    atr = (df["high"] - df["low"]).rolling(14, min_periods=5).mean()

    if spread_col in df.columns:
        spread = df[spread_col]
        if point:
            spread = spread * point
        out["spread_atr"] = spread / atr.replace(0, np.nan)
        out["spread_avg_20"] = spread.rolling(20, min_periods=5).mean()

    return out


# ── cross-market features ────────────────────────────────────────────────────

def compute_cross_market_features(
    prices: dict[str, pd.DataFrame],
    target_symbol: str,
) -> pd.DataFrame:
    """
    Compute rolling close-return correlations between the target symbol and other symbols.
    
    Parameters:
        prices (dict[str, pd.DataFrame]): Price data keyed by symbol.
        target_symbol (str): Symbol whose correlations are computed.
    
    Returns:
        pd.DataFrame: Target-indexed columns containing 20-period rolling correlations for symbols with at least 20 overlapping timestamps.
    """
    target_close = prices[target_symbol]["close"]
    out = pd.DataFrame(index=target_close.index)

    for sym, pdf in prices.items():
        if sym == target_symbol:
            continue
        common = target_close.index.intersection(pdf.index)
        if len(common) < 20:
            continue
        t = target_close.reindex(common).pct_change()
        o = pdf["close"].reindex(common).pct_change()
        corr = t.rolling(20, min_periods=10).corr(o)
        out[f"corr_{sym}"] = corr.reindex(target_close.index)

    return out


# ── combined feature matrix ──────────────────────────────────────────────────

def feature_dataframe(
    df: pd.DataFrame,
    *,
    with_momentum: bool = True,
    with_risk: bool = True,
    with_trend: bool = True,
    with_cost: bool = True,
) -> pd.DataFrame:
    """
    Assemble the selected feature groups into a single feature matrix for one symbol.
    
    Parameters:
        df (pd.DataFrame): Input market data indexed by timestamp.
        with_momentum (bool): Whether to include momentum features.
        with_risk (bool): Whether to include risk features.
        with_trend (bool): Whether to include trend features.
        with_cost (bool): Whether to include trading-cost features.
    
    Returns:
        pd.DataFrame: Feature matrix aligned to the input index, or an empty
            DataFrame with the same index when all feature groups are disabled.
    """
    parts: list[pd.DataFrame] = []

    if with_momentum:
        parts.append(compute_momentum_features(df))
    if with_risk:
        parts.append(compute_risk_features(df))
    if with_trend:
        parts.append(compute_trend_features(df))
    if with_cost:
        parts.append(compute_cost_features(df))

    if not parts:
        return pd.DataFrame(index=df.index)

    return pd.concat(parts, axis=1)
