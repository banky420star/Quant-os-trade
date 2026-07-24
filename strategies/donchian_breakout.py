"""Donchian breakout strategy — trend-following via N-bar channel.

Classic Turtle-style breakout:
  - Compute prior N-bar high (high_N) and prior N-bar low (low_N).
  - Long signal: close breaks above high_N (close > high_N).
  - Short signal: close breaks below low_N (close < low_N).
  - Optional second band (M-bar) for early exit / re-entry.
  - ATR-volatility filter to skip dead markets.

Multi-band dual length so the strategy can simultaneously:
  - Capture slow trends via long-N band.
  - Take small swings via short-N band.

Correlated with: trend-continuation, breakout, momentum.
NOT correlated with: mean-reversion, Bollinger reversion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class DonchianParams:
    entry_len: int = 20           # bars for breakout channel
    exit_len: int = 10             # bars for early exit channel
    atr_len: int = 14
    atr_threshold: float = 0.0005  # absolute price-floor volatility filter
    use_double_band: bool = True   # true = 10-bar signals also fire
    rr: float = 1.8                # reward / risk target
    sl_atr: float = 1.5            # SL distance = sl_atr × ATR

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_len": self.entry_len,
            "exit_len": self.exit_len,
            "atr_len": self.atr_len,
            "atr_threshold": self.atr_threshold,
            "use_double_band": self.use_double_band,
            "rr": self.rr,
            "sl_atr": self.sl_atr,
        }


def atr_series(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Standard 14-period ATR (EWM)."""
    h = df["high"]
    l = df["low"]
    c = df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def chan_high(df: pd.DataFrame, n: int) -> pd.Series:
    """Highest high of the prior n bars (rolling max with shift(1)).

    The shift(1) entry/exit_len bars. That means the value at index i is the
    highest high from i-n .. i-1 (excluding current bar). A breakout on bar i+1
    is the FIRST bar that closes above that level.
    """
    return df["high"].rolling(n, min_periods=n).max().shift(1)


def chan_low(df: pd.DataFrame, n: int) -> pd.Series:
    return df["low"].rolling(n, min_periods=n).min().shift(1)


def detect_signals(
    df: pd.DataFrame,
    params: DonchianParams | dict[str, Any] | None = None,
    *,
    warmup: int = 60,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(long_bars, short_bars, band)``.

    band[i] is 0 = long-N, 1 = short-N. Long-N signals are stronger.
    """
    if params is None:
        params = DonchianParams()
    if isinstance(params, dict):
        params = DonchianParams(**params)

    n_main = params.entry_len
    n_fast = max(params.exit_len, 5)

    high_main = chan_high(df, n_main).to_numpy()
    low_main = chan_low(df, n_main).to_numpy()

    high_fast = chan_high(df, n_fast).to_numpy()
    low_fast = chan_low(df, n_fast).to_numpy()

    close = df["close"].to_numpy()
    atr = atr_series(df, params.atr_len).to_numpy()
    vol_ok = atr > params.atr_threshold

    def breakout_idx(cond: np.ndarray, start: int) -> np.ndarray:
        """First index where cond transitions False -> True along the array."""
        prev = np.concatenate(([False], cond[:-1]))
        return np.where(prev & cond)[0]

    cond_long_main = (close > high_main) & vol_ok & np.isfinite(high_main)
    cond_short_main = (close < low_main) & vol_ok & np.isfinite(low_main)

    long_main = breakout_idx(cond_long_main, warmup)
    short_main = breakout_idx(cond_short_main, warmup)

    if not params.use_double_band:
        long_bars = long_main
        short_bars = short_main
        bands = np.zeros_like(long_bars)
        bands_short = np.zeros_like(short_bars)

        long_bars = long_bars[long_bars > warmup]
        short_bars = short_bars[short_bars > warmup]
        return long_bars, short_bars, np.zeros(len(long_bars) + len(short_bars), dtype=int)

    # Fast band
    cond_long_fast = (close > high_fast) & (close <= high_main * 1.5) & vol_ok
    cond_short_fast = (close < low_fast) & (close >= low_main * 0.5) & vol_ok
    long_fast = breakout_idx(cond_long_fast, warmup)
    short_fast = breakout_idx(cond_short_fast, warmup)

    long_bars = np.concatenate([long_main, long_fast])
    short_bars = np.concatenate([short_main, short_fast])
    bands = np.concatenate([
        np.zeros(len(long_main), dtype=int),
        np.ones(len(long_fast), dtype=int),
    ])
    bands_short = np.concatenate([
        np.zeros(len(short_main), dtype=int),
        np.ones(len(short_fast), dtype=int),
    ])

    # Mask warmup and end-of-series (need entry bar)
    mask_l = long_bars > warmup
    mask_s = short_bars > warmup
    return (
        long_bars[mask_l],
        short_bars[mask_s],
        np.concatenate([bands[mask_l], bands_short[mask_s]]),
    )


def simulate(
    df: pd.DataFrame,
    params: DonchianParams | dict[str, Any] | None = None,
    *,
    cost_r: float = 0.15,
    warmup: int = 60,
) -> list[float]:
    """Walk forward; return net R-multiples (after cost_r)."""
    if params is None:
        params = DonchianParams()
    if isinstance(params, dict):
        params = DonchianParams(**params)

    long_bars, short_bars, _ = detect_signals(df, params, warmup=warmup)
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
    params: DonchianParams | dict[str, Any] | None = None,
    *,
    cost_r: float = 0.15,
    warmup: int = 60,
    folds: int = 3,
) -> dict[str, Any]:
    if params is None:
        params = DonchianParams()
    if isinstance(params, dict):
        params = DonchianParams(**params)

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
