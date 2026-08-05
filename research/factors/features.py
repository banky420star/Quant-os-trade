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
    """Return, distance-from-high, and rate-of-change for each horizon."""
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
    """Volatility, downside vol, drawdown, gap frequency."""
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
    """ADX proxy, MA slope, breakout distance."""
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
    """Current spread vs ATR, session average spread, estimated fill drift."""
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
    """Rolling correlation, cluster momentum, USD proxy for *target_symbol*."""
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
    """Compute the full feature matrix for a single symbol."""
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
