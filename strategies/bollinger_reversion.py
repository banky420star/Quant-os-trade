"""Bollinger Band mean-reversion strategy — FX-tuned, contrarian.

Logic:
  - Compute 20-period SMA and standard deviation.
  - Lower band = SMA - 2σ, upper band = SMA + 2σ.
  - Long: close crosses UP through lower band (i.e. price was oversold, now mean-reverting).
  - Short: close crosses DOWN through upper band.
  - Stochastic confirmation (k<20 long, k>80 short).
  - Target: band midline (= SMA).

Correlated with: range-trades, fade-extremes, MR setups.
NOT correlated with: trend-breakouts (Donchian), volatility expansion (ATR Expansion).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class BollingerParams:
    period: int = 20
    std_mult: float = 2.0
    atr_len: int = 14
    atr_threshold: float = 0.0005
    rr: float = 0.8            # target band midline; conservative
    sl_atr: float = 1.0        # SL beyond band
    require_stoch: bool = True  # gate: stoch<20 long, stoch>80 short

    def as_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "std_mult": self.std_mult,
            "atr_len": self.atr_len,
            "atr_threshold": self.atr_threshold,
            "rr": self.rr,
            "sl_atr": self.sl_atr,
            "require_stoch": self.require_stoch,
        }


def bollinger_bands(df: pd.DataFrame, period: int = 20,
                    std_mult: float = 2.0) -> dict[str, pd.Series]:
    close = df["close"]
    middle = close.rolling(period, min_periods=period).mean()
    std = close.rolling(period, min_periods=period).std()
    upper = middle + std_mult * std
    lower = middle - std_mult * std
    return {"middle": middle, "upper": upper, "lower": lower}


def stochastic_k(df: pd.DataFrame, k_period: int = 14) -> pd.Series:
    low_min = df["low"].rolling(k_period, min_periods=k_period).min()
    high_max = df["high"].rolling(k_period, min_periods=k_period).max()
    num = df["close"] - low_min
    denom = (high_max - low_min).replace(0, np.nan)
    k = 100.0 * num / denom
    return k.fillna(50.0)


def atr_series(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h = df["high"]; l = df["low"]; c = df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def detect_signals(
    df: pd.DataFrame,
    params: BollingerParams | dict[str, Any] | None = None,
    *,
    warmup: int = 60,
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(long_bars, short_bars)``. Long when price rebounds off lower band."""
    if params is None:
        params = BollingerParams()
    if isinstance(params, dict):
        params = BollingerParams(**params)

    bands = bollinger_bands(df, params.period, params.std_mult)
    middle = bands["middle"].to_numpy()
    upper = bands["upper"].to_numpy()
    lower = bands["lower"].to_numpy()

    close = df["close"].to_numpy()
    open_ = df["open"].to_numpy()

    stoch = stochastic_k(df).to_numpy() if params.require_stoch else None
    atr = atr_series(df, params.atr_len).to_numpy()
    vol_ok = atr > params.atr_threshold

    # Long: previous bar close at/below lower band & current bar closed IN channel
    # (i.e. close > lower band). Indicates reversion start.
    long_cond = (
        (close[:-1] <= lower[:-1])  # previous bar touched lower band
        & (close[1:] > lower[1:])    # current bar closed inside channel
        & vol_ok[1:]
    )
    if stoch is not None:
        long_cond = long_cond & (stoch[:-1] < 30)

    long_bars = np.where(long_cond)[0] + 1
    long_bars = long_bars[(long_bars > warmup) & (long_bars < len(df) - 2)]

    # Short: previous bar close at/above upper band & current bar closed IN channel
    short_cond = (
        (close[:-1] >= upper[:-1])
        & (close[1:] < upper[1:])
        & vol_ok[1:]
    )
    if stoch is not None:
        short_cond = short_cond & (stoch[:-1] > 70)

    short_bars = np.where(short_cond)[0] + 1
    short_bars = short_bars[(short_bars > warmup) & (short_bars < len(df) - 2)]

    return long_bars, short_bars


def simulate(
    df: pd.DataFrame,
    params: BollingerParams | dict[str, Any] | None = None,
    *,
    cost_r: float = 0.10,
    warmup: int = 60,
) -> list[float]:
    if params is None:
        params = BollingerParams()
    if isinstance(params, dict):
        params = BollingerParams(**params)

    long_bars, short_bars = detect_signals(df, params, warmup=warmup)
    bars = np.concatenate([long_bars, short_bars])
    sides = np.concatenate([np.ones(len(long_bars), dtype=int),
                            -np.ones(len(short_bars), dtype=int)])
    if bars.size == 0:
        return []

    order = np.argsort(bars, kind="mergesort")
    bars = bars[order]
    sides = sides[order]

    bands = bollinger_bands(df, params.period, params.std_mult)
    middle = bands["middle"].to_numpy()
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    a = atr_series(df, params.atr_len).to_numpy()
    n = len(df)
    rs: list[float] = []
    cursor = warmup
    for bar, direction in zip(bars, sides):
        if bar < cursor or bar >= n - 2:
            continue
        sl_dist = params.sl_atr * a[bar]
        if not np.isfinite(sl_dist) or sl_dist <= 0:
            continue
        entry = o[bar + 1]
        sl = entry - direction * sl_dist
        tp = entry + direction * params.rr * sl_dist
        exit_price = None
        j = bar + 1
        while j < n:
            if direction == 1:
                if l[j] <= sl:
                    exit_price = sl
                    break
                if h[j] >= tp:
                    exit_price = tp
                    break
            else:
                if h[j] >= sl:
                    exit_price = sl
                    break
                if l[j] <= tp:
                    exit_price = tp
                    break
            j += 1
        if exit_price is None:
            exit_price = c[min(j, n - 1)]
        rs.append(float(direction * (exit_price - entry) / sl_dist - cost_r))
        cursor = max(cursor, j)
    return rs


def evaluate(
    df: pd.DataFrame,
    params: BollingerParams | dict[str, Any] | None = None,
    *,
    cost_r: float = 0.10,
    warmup: int = 60,
    folds: int = 3,
) -> dict[str, Any]:
    if params is None:
        params = BollingerParams()
    if isinstance(params, dict):
        params = BollingerParams(**params)

    rs = simulate(df, params, cost_r=cost_r, warmup=warmup)
    n = len(rs)
    if n == 0:
        return {"n": 0, "verdict": "NO_TRADES", "pass": False,
                "params": params.as_dict()}
    wins = sum(1 for r in rs if r > 0)
    wr = wins / n * 100.0
    exp_r = float(np.mean(rs))
    pos = 0
    if n >= folds * 5:
        chunks = np.array_split(np.array(rs), folds)
        pos = sum(1 for ch in chunks if ch.size and ch.mean() > 0)
    return {
        "n": n,
        "win_rate_pct": round(wr, 2),
        "expectancy_r": round(exp_r, 4),
        "total_r": round(float(np.sum(rs)), 4),
        "std_r": round(float(np.std(rs, ddof=1) if n > 1 else 0.0), 4),
        "fold_positive": f"{pos}/{folds}",
        "params": params.as_dict(),
        "verdict": "PASS" if (exp_r > 0 and wr >= 38) else "FAIL",
        "pass": bool(exp_r > cost_r and wr >= 38 and pos >= 2),
    }
