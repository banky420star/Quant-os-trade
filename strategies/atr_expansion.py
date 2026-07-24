"""ATR volatility-expansion strategy — captures the start of big moves.

Logic:
  - Compute N-bar ATR ratio = ATR(14) / close.
  - Filter: only fire when ATR ratio > 0.5× recent_mean_atr_ratio (expansion).
  - Long: close breaks above N-bar high.
  - Short: close breaks below N-bar low.
  - Backoff: when ATR ratio is contracting (vol compression), skip — this strategy
    is dormant in quiet markets and purposefully reactive to vol expansion.

Correlated with: breakout, momentum (volatility triggers trends).
NOT correlated with: Donchian trend (independent SL/TP), Bollinger reversion (the
opposite thesis).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class AtrExpansionParams:
    breakout_len: int = 14         # bars for breakout channel
    atr_len: int = 14
    atr_ratio_lookback: int = 20   # bars for vol-regime baseline
    expansion_mult: float = 1.2    # ATR ratio must be > mult × baseline
    rr: float = 2.0
    sl_atr: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "breakout_len": self.breakout_len,
            "atr_len": self.atr_len,
            "atr_ratio_lookback": self.atr_ratio_lookback,
            "expansion_mult": self.expansion_mult,
            "rr": self.rr,
            "sl_atr": self.sl_atr,
        }


def atr_series(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h = df["high"]; l = df["low"]; c = df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def chan_high(df: pd.DataFrame, n: int) -> pd.Series:
    return df["high"].rolling(n, min_periods=n).max().shift(1)


def chan_low(df: pd.DataFrame, n: int) -> pd.Series:
    return df["low"].rolling(n, min_periods=n).min().shift(1)


def detect_signals(
    df: pd.DataFrame,
    params: AtrExpansionParams | dict[str, Any] | None = None,
    *,
    warmup: int = 80,
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(long_bars, short_bars)`` where ATR has just expanded."""
    if params is None:
        params = AtrExpansionParams()
    if isinstance(params, dict):
        params = AtrExpansionParams(**params)

    n = params.breakout_len
    high_n = chan_high(df, n).to_numpy()
    low_n = chan_low(df, n).to_numpy()
    close = df["close"].to_numpy()
    atr = atr_series(df, params.atr_len).to_numpy()
    atr_ratio = atr / np.where(close > 0, close, 1.0)

    baseline = pd.Series(atr_ratio).rolling(
        params.atr_ratio_lookback, min_periods=params.atr_ratio_lookback
    ).mean().shift(1).to_numpy()
    expanding = atr_ratio > params.expansion_mult * baseline

    cond_long = (close > high_n) & expanding & np.isfinite(high_n)
    cond_short = (close < low_n) & expanding & np.isfinite(low_n)

    prev_long = np.concatenate(([False], cond_long[:-1]))
    long_bars = np.where(prev_long & cond_long)[0] + 1
    prev_short = np.concatenate(([False], cond_short[:-1]))
    short_bars = np.where(prev_short & cond_short)[0] + 1

    long_bars = long_bars[(long_bars > warmup) & (long_bars < len(df) - 2)]
    short_bars = short_bars[(short_bars > warmup) & (short_bars < len(df) - 2)]
    return long_bars, short_bars


def simulate(
    df: pd.DataFrame,
    params: AtrExpansionParams | dict[str, Any] | None = None,
    *,
    cost_r: float = 0.20,
    warmup: int = 80,
) -> list[float]:
    """Returns list of net R-multiples after cost."""
    if params is None:
        params = AtrExpansionParams()
    if isinstance(params, dict):
        params = AtrExpansionParams(**params)

    long_bars, short_bars = detect_signals(df, params, warmup=warmup)
    bars = np.concatenate([long_bars, short_bars])
    sides = np.concatenate([np.ones(len(long_bars), dtype=int),
                            -np.ones(len(short_bars), dtype=int)])
    if bars.size == 0:
        return []

    order = np.argsort(bars, kind="mergesort")
    bars = bars[order]
    sides = sides[order]

    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    a = atr_series(df, params.atr_len).to_numpy()
    n_bars = len(df)
    rs: list[float] = []
    cursor = warmup
    for bar, direction in zip(bars, sides):
        if bar < cursor or bar >= n_bars - 2:
            continue
        sl_dist = params.sl_atr * a[bar]
        if not np.isfinite(sl_dist) or sl_dist <= 0:
            continue
        entry = o[bar + 1]
        sl = entry - direction * sl_dist
        tp = entry + direction * params.rr * sl_dist
        exit_price = None
        j = bar + 1
        while j < n_bars:
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
            exit_price = c[min(j, n_bars - 1)]
        rs.append(float(direction * (exit_price - entry) / sl_dist - cost_r))
        cursor = max(cursor, j)
    return rs


def evaluate(
    df: pd.DataFrame,
    params: AtrExpansionParams | dict[str, Any] | None = None,
    *,
    cost_r: float = 0.20,
    warmup: int = 80,
    folds: int = 3,
) -> dict[str, Any]:
    if params is None:
        params = AtrExpansionParams()
    if isinstance(params, dict):
        params = AtrExpansionParams(**params)

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
        "verdict": "PASS" if (exp_r > 0 and wr >= 35) else "FAIL",
        "pass": bool(exp_r > cost_r and wr >= 35 and pos >= 2),
    }
