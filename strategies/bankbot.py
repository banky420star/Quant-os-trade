"""bankbot: MA-crossover strategy (PineScript port + Python auto-tuning base).

Mirrors the PineScript "bankbot by bank" indicator/strategy:
- Two MA series (one over close, one over open) of a chosen MA type + length.
- Long on crossup of close-MA over open-MA, short on crossdown.
- ATR-volatility filter to skip dead-market entries.
- Optional alternate-resolution multiplier for hierarchical timeframe bias.
- 12 MA variants supported: SMA, EMA, DEMA, TEMA, WMA, VWMA, SMMA,
  HullMA, LSMA, ALMA, SSMA, TMA.
- Pure-numpy/pandas simulator: R-multiple outcome per trade, cost deducted.

Public functions:
    MA_TYPES              list of supported MA-type names
    compute_ma(src, ma, n, **kw)  single MA series (pandas Series)
    atr_series(df, n=14)          ATR (pandas Series)
    detect_signals(df, **kw)      (long_bars, short_bars) int arrays
    simulate(df, **kw)            list[float] R multiples
    fold_positive(rs, folds)      walk-forward fold positive count
    evaluate(df, **kw)            dict-of-summary metrics for one config
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# MA variants
# ---------------------------------------------------------------------------
MA_TYPES = [
    "SMA", "EMA", "DEMA", "TEMA", "WMA",
    "VWMA", "SMMA", "HullMA", "LSMA",
    "ALMA", "SSMA", "TMA",
]


def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _dema(s: pd.Series, n: int) -> pd.Series:
    e1 = _ema(s, n)
    e2 = _ema(e1, n)
    return 2 * e1 - e2


def _tema(s: pd.Series, n: int) -> pd.Series:
    e1 = _ema(s, n)
    e2 = _ema(e1, n)
    e3 = _ema(e2, n)
    return 3 * (e1 - e2) + e3


def _wma(s: pd.Series, n: int) -> pd.Series:
    weights = np.arange(1, n + 1, dtype=float)
    denom = weights.sum()
    return s.rolling(n, min_periods=n).apply(
        lambda x: float(np.dot(x, weights) / denom), raw=True
    )


def _vwma(s: pd.Series, n: int, volume: pd.Series | None) -> pd.Series:
    if volume is None:
        # fall back to SMA if volume is missing
        return _sma(s, n)
    pv = s * volume
    num = pv.rolling(n, min_periods=n).sum()
    den = volume.rolling(n, min_periods=n).sum().replace(0, np.nan)
    return num / den


def _smma(s: pd.Series, n: int) -> pd.Series:
    """Smoothed MA: SMMA_i = (SMMA_{i-1} * (n-1) + src_i) / n, seeded by SMA_n."""
    out = pd.Series(index=s.index, dtype=float)
    sma0 = _sma(s, n)
    out.iloc[n - 1] = sma0.iloc[n - 1]
    for i in range(n, len(s)):
        prev = out.iloc[i - 1]
        if pd.isna(prev):
            out.iloc[i] = sma0.iloc[i]
        else:
            out.iloc[i] = (prev * (n - 1) + s.iloc[i]) / n
    return out


def _hullma(s: pd.Series, n: int) -> pd.Series:
    half = max(int(round(n / 2)), 1)
    sqrt_n = max(int(round(np.sqrt(n))), 1)
    inner = 2 * _wma(s, half) - _wma(s, n)
    return _wma(inner, sqrt_n)


def _lsma(s: pd.Series, n: int) -> pd.Series:
    """Least-squares MA: end-of-window linear-regression value."""
    out = pd.Series(index=s.index, dtype=float)
    vals = s.values
    xs = np.arange(n)
    for i in range(n - 1, len(vals)):
        y = vals[i - n + 1 : i + 1]
        if np.any(~np.isfinite(y)):
            out.iloc[i] = np.nan
            continue
        # fit slope/intercept via least squares
        a_mat = np.vstack([xs, np.ones_like(xs)]).T
        slope, intercept = np.linalg.lstsq(a_mat, y, rcond=None)[0]
        out.iloc[i] = slope * (n - 1) + intercept
    return out


def _alma(s: pd.Series, n: int, offset: float = 0.85, sigma: int = 6) -> pd.Series:
    m = offset * (n - 1)
    w = np.exp(-((np.arange(n) - m) ** 2) / (2.0 * (sigma ** 2)))
    w = w / w.sum()
    return s.rolling(n, min_periods=n).apply(
        lambda x: float(np.dot(x, w)), raw=True
    )


def _ssma(s: pd.Series, n: int) -> pd.Series:
    """Ehlers SuperSmoother (2-pole Butterworth IIR)."""
    a = float(np.exp(-1.414 * np.pi / n))
    b = 2.0 * a * float(np.cos(1.414 * np.pi / n))
    c2 = b
    c3 = -(a * a)
    c1 = 1.0 - c2 - c3
    out = pd.Series(index=s.index, dtype=float)
    src = s.values
    for i in range(len(src)):
        if i < 2 or np.isnan(src[i]) or np.isnan(src[i - 1]):
            out.iloc[i] = src[i] if i < len(src) and np.isfinite(src[i]) else np.nan
            continue
        out.iloc[i] = c1 * (src[i] + src[i - 1]) / 2.0 + c2 * out.iloc[i - 1] + c3 * out.iloc[i - 2]
    return out


def _tma(s: pd.Series, n: int) -> pd.Series:
    return _sma(_sma(s, n), n)


def compute_ma(src: pd.Series, ma_type: str, n: int, *, volume: pd.Series | None = None,
               alma_offset: float = 0.85, alma_sigma: int = 6) -> pd.Series:
    """Return the requested MA series of length ``n`` over ``src``."""
    ma_type = ma_type.upper()
    if ma_type not in {"SMA", "EMA", "DEMA", "TEMA", "WMA", "VWMA", "SMMA",
                        "HULLMA", "LSMA", "ALMA", "SSMA", "TMA"}:
        raise ValueError(f"unsupported ma_type={ma_type!r}; pick from {MA_TYPES}")
    if n < 2:
        raise ValueError(f"ma length must be >= 2; got {n}")
    if ma_type == "SMA":
        return _sma(src, n)
    if ma_type == "EMA":
        return _ema(src, n)
    if ma_type == "DEMA":
        return _dema(src, n)
    if ma_type == "TEMA":
        return _tema(src, n)
    if ma_type == "WMA":
        return _wma(src, n)
    if ma_type == "VWMA":
        return _vwma(src, n, volume)
    if ma_type == "SMMA":
        return _smma(src, n)
    if ma_type == "HULLMA":
        return _hullma(src, n)
    if ma_type == "LSMA":
        return _lsma(src, n)
    if ma_type == "ALMA":
        return _alma(src, n, alma_offset, alma_sigma)
    if ma_type == "SSMA":
        return _ssma(src, n)
    if ma_type == "TMA":
        return _tma(src, n)
    raise AssertionError(f"unreachable ma_type={ma_type}")


# ---------------------------------------------------------------------------
# ATR + volatility filter
# ---------------------------------------------------------------------------
def atr_series(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h = df["high"]
    l = df["low"]
    c = df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def atr_threshold_for(symbol_price_digits: int | None = None) -> float:
    """Default ATR floor. Mirrors PineScript default of 0.0005 (5-decimal FX).

    For 3-digit JPY pairs the threshold scales better by an order of mag;
    leave as 0.0005 default — callers can override.
    """
    return 0.0005


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------
@dataclass
class BankbotParams:
    ma_type: str = "EMA"
    length: int = 8
    mult: int = 3          # alternate-resolution multiplier (1 = same TF)
    atr_len: int = 14
    atr_threshold: float = 0.0005
    alma_offset: float = 0.85
    alma_sigma: int = 6

    def as_dict(self) -> dict[str, Any]:
        return {
            "ma_type": self.ma_type,
            "length": self.length,
            "mult": self.mult,
            "atr_len": self.atr_len,
            "atr_threshold": self.atr_threshold,
            "alma_offset": self.alma_offset,
            "alma_sigma": self.alma_sigma,
        }


def detect_signals(df: pd.DataFrame, params: BankbotParams | dict[str, Any] | None = None,
                   *, warmup: int = 80) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(long_bars, short_bars)`` indices into ``df`` for signal triggers.

    A signal fires on close-of-bar when the close-MA crosses the open-MA in the
    alternate-resolution frame's direction, subject to ATR-volatility filter.
    Trades are normally entered on the NEXT bar's open (handled by simulator).
    """
    if params is None:
        params = BankbotParams()
    if isinstance(params, dict):
        params = BankbotParams(**params)

    close = df["close"].astype(float)
    open_ = df["open"].astype(float)
    vol = df["volume"].astype(float) if "volume" in df.columns else None

    # Base MA (frame on entry timeframe)
    close_ma = compute_ma(close, params.ma_type, params.length, volume=vol,
                          alma_offset=params.alma_offset, alma_sigma=params.alma_sigma)
    open_ma = compute_ma(open_, params.ma_type, params.length, volume=vol,
                         alma_offset=params.alma_offset, alma_sigma=params.alma_sigma)

    # Alternate-resolution MA — handlebars-thicker
    alt_len = max(params.length * params.mult, params.length + 1)
    close_ma_alt = compute_ma(close, params.ma_type, alt_len, volume=vol)
    open_ma_alt = compute_ma(open_, params.ma_type, alt_len, volume=vol)
    bias_long = (close_ma_alt > open_ma_alt).fillna(False).to_numpy()
    bias_short = (close_ma_alt < open_ma_alt).fillna(False).to_numpy()

    diff = (close_ma - open_ma).to_numpy()
    sgn = np.sign(diff)
    # Crossover at index i: sign flipped 0 -> +1 between i-1 and i
    prev = sgn[:-1]
    cur = sgn[1:]
    crossup_bars = np.where((prev <= 0) & (cur > 0))[0] + 1
    crossdn_bars = np.where((prev >= 0) & (cur < 0))[0] + 1

    # ATR volatility filter
    atr = atr_series(df, params.atr_len).to_numpy()
    vol_ok = atr > params.atr_threshold

    def _apply_filter(bars: np.ndarray, bias: np.ndarray) -> np.ndarray:
        mask = (bars >= warmup) & (bars < len(df) - 2)
        cand = bars[mask]
        if cand.size == 0:
            return cand
        keep = np.array([vol_ok[b] and bias[b] for b in cand], dtype=bool)
        return cand[keep]

    long_bars = _apply_filter(crossup_bars, bias_long)
    short_bars = _apply_filter(crossdn_bars, bias_short)
    return long_bars, short_bars


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------
def simulate(df: pd.DataFrame, params: BankbotParams | dict[str, Any] | None = None,
             *, rr: float = 1.5, sl_atr: float = 1.2, cost_r: float = 0.15,
             warmup: int = 80) -> list[float]:
    """Walk forward, simulate R-multiple outcomes for the bankbot strategy.

    Returns a list of floats (one per trade) — net R after cost.
    """
    if params is None:
        params = BankbotParams()
    if isinstance(params, dict):
        params = BankbotParams(**params)

    long_bars, short_bars = detect_signals(df, params, warmup=warmup)
    bars = np.concatenate([long_bars, short_bars])
    sides = np.concatenate([np.ones(len(long_bars), dtype=int),
                            -np.ones(len(short_bars), dtype=int)])
    if bars.size == 0:
        return []

    # Walk in time order; multiple bars per epoch allowed but cursor blocks re-entry
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
        entry_bar = bar + 1
        sl_dist = sl_atr * a[bar]
        if not np.isfinite(sl_dist) or sl_dist <= 0:
            continue
        entry = o[entry_bar]
        sl = entry - direction * sl_dist
        tp = entry + direction * rr * sl_dist
        exit_price = None
        j = entry_bar
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


# ---------------------------------------------------------------------------
# Evaluation helpers
# ---------------------------------------------------------------------------
def fold_positive(rs: list[float], folds: int = 3) -> tuple[int, int]:
    """Return (#pos_folds, total_folds) where a fold is "positive" if mean R > 0."""
    if len(rs) < folds * 5 or folds <= 0:
        return 0, max(folds, 1)
    arr = np.array(rs)
    labels = np.array_split(arr, folds)
    pos = sum(1 for chunk in labels if chunk.size and chunk.mean() > 0)
    return pos, folds


def evaluate(df: pd.DataFrame, params: BankbotParams | dict[str, Any] | None = None,
             *, rr: float = 1.5, sl_atr: float = 1.2, cost_r: float = 0.15,
             warmup: int = 80) -> dict[str, Any]:
    """One-line summary for a config."""
    if params is None:
        params = BankbotParams()
    if isinstance(params, dict):
        params = BankbotParams(**params)

    rs = simulate(df, params, rr=rr, sl_atr=sl_atr, cost_r=cost_r, warmup=warmup)
    n = len(rs)
    if n == 0:
        return {"n": 0, "verdict": "NO_TRADES", "pass": False,
                "params": params.as_dict()}
    wins = sum(1 for r in rs if r > 0)
    wr = wins / n * 100.0
    exp_r = float(np.mean(rs))
    pos, total = fold_positive(rs)
    return {
        "n": n,
        "win_rate_pct": round(wr, 2),
        "expectancy_r": round(exp_r, 4),
        "total_r": round(float(np.sum(rs)), 4),
        "stdout_r": round(float(np.std(rs, ddof=1) if n > 1 else 0.0), 4),
        "fold_positive": f"{pos}/{total}",
        "params": params.as_dict(),
        "verdict": "PASS" if (exp_r > 0 and wr >= 40) else "FAIL",
        # 'pass' is the strict gate (DSR-friendly): positive in >=2/3 folds AND win rate
        "pass": bool(exp_r > cost_r and wr >= 40 and pos >= 2),
    }
