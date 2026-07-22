"""Solo backtests for candidate detection/timing strategies.

Each strategy is tested IN ISOLATION before any live integration.
Focus: volatility regime + time-of-day (UTC session) + 5 complementary patterns.

Pass gate (per symbol, solo):
  - n_trades >= 40
  - expectancy_r >= 0.05
  - win_rate_pct >= 46
  - walk-forward: >= 2/3 folds with positive mean R

Run:
  python scripts/strategy_probe_solo.py
  python scripts/strategy_probe_solo.py --symbols XAUUSDm USOILm EURUSDm
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.session_scorer import resolve_trading_session  # noqa: E402

HIST = ROOT / "data" / "history"
COST_R = 0.15
RR = 1.5
SL_ATR = 1.2
MIN_TRADES = 40
MIN_EXP_R = 0.05
MIN_WR = 46.0
FOLDS = 3
WARMUP = 80

PRIME_SESSIONS = frozenset({"london_open", "overlap_london_ny", "london_mid", "new_york"})


def load_m5(symbol: str) -> pd.DataFrame:
    path = HIST / f"{symbol}_M5.parquet"
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_parquet(path)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def atr_series(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _session_label(hours: pd.Index) -> np.ndarray:
    return np.array([resolve_trading_session(int(h)) for h in hours], dtype=object)


def prep_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["atr"] = atr_series(out)
    out["atr_pct"] = out["atr"].rolling(100, min_periods=20).rank(pct=True)
    out["atr_ma"] = out["atr"].rolling(20).mean()
    out["vol_ma"] = out["volume"].rolling(20).mean()
    out["vol_ratio"] = out["volume"] / out["vol_ma"].replace(0, np.nan)

    close = out["close"]
    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    lower = mid - 2 * std
    width = (4 * std).replace(0, np.nan)
    out["bb_position"] = (close - lower) / width

    ema20 = ema(close, 20)
    out["ema20_slope"] = ema20.diff(5)
    out["m5_bull"] = out["ema20_slope"] > 0
    out["m5_bear"] = out["ema20_slope"] < 0
    out["m15_bull"] = ema(close, 60).diff(15) > 0
    out["m15_bear"] = ema(close, 60).diff(15) < 0

    low_min = out["low"].rolling(14).min()
    high_max = out["high"].rolling(14).max()
    denom = (high_max - low_min).replace(0, np.nan)
    k = 100 * (close - low_min) / denom
    d = k.rolling(3).mean()
    out["stoch_k"] = k
    out["stoch_bull_cross"] = (k.shift(1) <= d.shift(1)) & (k > d)
    out["stoch_bear_cross"] = (k.shift(1) >= d.shift(1)) & (k < d)

    sup = out["low"].rolling(50).min()
    res = out["high"].rolling(50).max()
    prev_close = close.shift(1)
    out["breakout"] = (prev_close <= res.shift(1)) & (close > res.shift(1))
    out["breakdown"] = (prev_close >= sup.shift(1)) & (close < sup.shift(1))

    sessions = _session_label(out.index.hour)
    out["session"] = sessions
    out["prime_session"] = np.isin(sessions, list(PRIME_SESSIONS))
    out["bar_range"] = out["high"] - out["low"]
    out["spike_bar"] = out["bar_range"] > (2.0 * out["atr"])

    hour = out.index.hour
    out["orb_high"] = out["high"].where(hour == 7).ffill()
    out["orb_low"] = out["low"].where(hour == 7).ffill()
    out["hour"] = hour

    return out


@dataclass
class Signal:
    bar: int
    side: int


def simulate(df: pd.DataFrame, bars: np.ndarray, sides: np.ndarray, *, rr: float = RR, sl_atr: float = SL_ATR) -> list[float]:
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    a = df["atr"].to_numpy()
    n = len(df)
    rs: list[float] = []
    cursor = WARMUP
    for bar, direction in zip(bars, sides):
        if bar < cursor or bar >= n - 2:
            continue
        entry_bar = bar + 1
        entry = o[entry_bar]
        sl_dist = sl_atr * a[bar]
        if not np.isfinite(sl_dist) or sl_dist <= 0:
            continue
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
        rs.append(float(direction * (exit_price - entry) / sl_dist - COST_R))
        cursor = max(cursor, j)
    return rs


def _bars_sides(mask: pd.Series, buy: pd.Series, sell: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    m = mask.fillna(False).to_numpy()
    b = buy.fillna(False).to_numpy()
    s = sell.fillna(False).to_numpy()
    idx = np.where(m & (b | s))[0]
    sides = np.where(b[idx], 1, -1)
    return idx, sides


def detect_session_vol_breakout(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    m = df["prime_session"] & (df["atr_pct"] > 0.55) & (df["vol_ratio"] > 1.15)
    return _bars_sides(m, df["breakout"], df["breakdown"])


def detect_low_vol_compression_fade(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    m = df["atr_pct"] < 0.30
    return _bars_sides(m, df["bb_position"] <= 0.08, df["bb_position"] >= 0.92)


def detect_session_momentum_continuation(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    m = df["prime_session"] & (df["atr"] > df["atr_ma"])
    buy = m & df["m5_bull"] & df["m15_bull"] & (df["ema20_slope"] > 0)
    sell = m & df["m5_bear"] & df["m15_bear"] & (df["ema20_slope"] < 0)
    return _bars_sides(m, buy, sell)


def detect_opening_range_breakout(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    m = (df["hour"] >= 8) & (df["hour"] < 10)
    prev_close = df["close"].shift(1)
    buy = m & (prev_close <= df["orb_high"]) & (df["close"] > df["orb_high"])
    sell = m & (prev_close >= df["orb_low"]) & (df["close"] < df["orb_low"])
    return _bars_sides(m, buy, sell)


def detect_vol_spike_exhaustion_fade(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    spike = df["spike_bar"].shift(1)
    sp_close = df["close"].shift(1)
    sp_open = df["open"].shift(1)
    sp_range = df["bar_range"].shift(1).replace(0, np.nan)
    retrace = (df["close"] - sp_close).abs() / sp_range
    m = spike & (retrace >= 0.5)
    buy = m & (sp_close < sp_open)
    sell = m & (sp_close > sp_open)
    return _bars_sides(m, buy, sell)


def detect_session_stoch_pullback(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    m = df["prime_session"]
    prev_k = df["stoch_k"].shift(1)
    buy = m & df["m5_bull"] & df["m15_bull"] & (prev_k < 40) & df["stoch_bull_cross"]
    sell = m & df["m5_bear"] & df["m15_bear"] & (prev_k > 60) & df["stoch_bear_cross"]
    return _bars_sides(m, buy, sell)


STRATEGIES: dict[str, tuple[str, Callable[[pd.DataFrame], tuple[np.ndarray, np.ndarray]]]] = {
    "session_vol_breakout": (
        "Breakout in prime session when ATR percentile >55% and volume >1.15x",
        detect_session_vol_breakout,
    ),
    "low_vol_compression_fade": (
        "Fade BB extremes when ATR percentile <30% (compression)",
        detect_low_vol_compression_fade,
    ),
    "session_momentum_continuation": (
        "Trend continuation in prime session with ATR > 20-bar mean",
        detect_session_momentum_continuation,
    ),
    "opening_range_breakout": (
        "London 07-08 UTC ORB break during 08-10 UTC",
        detect_opening_range_breakout,
    ),
    "vol_spike_exhaustion_fade": (
        "Fade after 2xATR spike bar with >50% retrace",
        detect_vol_spike_exhaustion_fade,
    ),
    "session_stoch_pullback": (
        "Stoch cross back with trend in prime session (pullback timing)",
        detect_session_stoch_pullback,
    ),
}


def fold_positive(rs: list[float], folds: int = FOLDS) -> tuple[int, int]:
    if len(rs) < folds * 5:
        return 0, folds
    labels = pd.qcut(np.arange(len(rs)), folds, labels=False)
    arr = np.array(rs)
    pos = sum(1 for f in range(folds) if arr[labels == f].mean() > 0)
    return pos, folds


def evaluate_strategy(symbol: str, name: str, df: pd.DataFrame, detect: Callable) -> dict[str, Any]:
    bars, sides = detect(df)
    keep = bars >= WARMUP
    bars = bars[keep]
    sides = sides[keep]
    rs = simulate(df, bars, sides)
    n = len(rs)
    if n == 0:
        return {"symbol": symbol, "strategy": name, "n_trades": 0, "verdict": "NO_TRADES", "pass": False}
    wins = sum(1 for r in rs if r > 0)
    wr = wins / n * 100
    exp_r = float(np.mean(rs))
    pos, total = fold_positive(rs)
    passed = n >= MIN_TRADES and exp_r >= MIN_EXP_R and wr >= MIN_WR and pos >= 2
    return {
        "symbol": symbol,
        "strategy": name,
        "n_trades": n,
        "win_rate_pct": round(wr, 1),
        "expectancy_r": round(exp_r, 3),
        "total_r": round(float(np.sum(rs)), 2),
        "fold_positive": f"{pos}/{total}",
        "pass": passed,
        "verdict": "PASS" if passed else "FAIL",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Solo strategy probe before live integration")
    ap.add_argument(
        "--symbols",
        nargs="+",
        default=["XAUUSDm", "USOILm", "EURUSDm", "US500m", "BTCUSDm"],
    )
    ap.add_argument("--out", default=str(ROOT / "state" / "strategy_probe_report.json"))
    args = ap.parse_args()

    results: list[dict[str, Any]] = []
    print(f"Solo strategy probe  symbols={args.symbols}  cost={COST_R}R")
    print("-" * 88)

    for sym in args.symbols:
        try:
            df = prep_features(load_m5(sym))
        except FileNotFoundError as exc:
            print(f"  SKIP {sym}: {exc}")
            continue
        for name, (desc, detect) in STRATEGIES.items():
            row = evaluate_strategy(sym, name, df, detect)
            row["description"] = desc
            results.append(row)
            flag = "PASS" if row["pass"] else "FAIL"
            print(
                f"  {sym:<10} {name:<32} n={row['n_trades']:4}  "
                f"wr={row.get('win_rate_pct', 0):5.1f}%  exp={row.get('expectancy_r', 0):+.3f}R  "
                f"folds={row.get('fold_positive', '0/3')}  {flag}"
            )

    by_strategy: dict[str, list[dict]] = {}
    for r in results:
        by_strategy.setdefault(r["strategy"], []).append(r)

    recommendations = []
    for name, rows in by_strategy.items():
        passed_syms = [r["symbol"] for r in rows if r.get("pass")]
        best = max(rows, key=lambda x: (x.get("expectancy_r", -99), x.get("n_trades", 0)))
        recommendations.append({
            "strategy": name,
            "description": STRATEGIES[name][0],
            "passed_symbols": passed_syms,
            "best_symbol": best["symbol"],
            "best_expectancy_r": best.get("expectancy_r"),
            "integrate": len(passed_syms) > 0,
        })

    report = {
        "cost_r": COST_R,
        "gate": {"min_trades": MIN_TRADES, "min_expectancy_r": MIN_EXP_R, "min_win_rate_pct": MIN_WR},
        "results": results,
        "recommendations": recommendations,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("-" * 88)
    print("RECOMMENDATIONS (integrate only if integrate=true):")
    for rec in recommendations:
        status = "ADD" if rec["integrate"] else "HOLD"
        syms = ", ".join(rec["passed_symbols"]) or "none"
        print(f"  [{status}] {rec['strategy']:<32} passed: {syms}")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()