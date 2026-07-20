"""Independent cross-check of the quantum-loop exit-mechanism finding.

ZERO shared code with core/paper_broker.py, core/replay_engine.py, or the
strategy_variants pipeline. This is a deliberately DIFFERENT, simpler strategy
(classic EMA20-slope trend-follow with ATR(14) stops) reimplemented from scratch
in pure pandas/numpy. It does NOT replicate the bot's FeatureEngine; its job is
to test the same hypothesis with a second engine:

    "Does adding break-even + trailing exits to a trend entry on XAUUSDm
     flip per-trade R from ~0 to clearly positive on the fold-0 test window?"

If this independent engine shows the same directional effect the bot's
quantum loop reported (off ~ flat, medium clearly positive), the mechanism is
corroborated. If it diverges, the bot's number is suspect.

Read-only: reads parquet history, prints stats. No live trades, no edge-DB.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
HIST = ROOT / "data" / "history"
COST_R = 0.15  # matches quantum_loop --cost-r


def load(symbol: str, tf: str = "M15") -> pd.DataFrame:
    df = pd.read_parquet(HIST / f"{symbol}_{tf}.parquet")
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False).mean()


def slice_window(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    return df.loc[(df.index >= pd.Timestamp(start, tz="UTC")) &
                  (df.index < pd.Timestamp(end, tz="UTC"))]


def backtest(df: pd.DataFrame, *, exit_mode: str, rr: float = 2.0,
             atr_sl_mult: float = 1.5) -> dict:
    """Vectorised-ish single-position backtest.

    Entry rule (independent, simple): EMA20 slope sign.
      BUY when EMA20 slope > 0 and no position; SELL when slope < 0 and no
      position. One position at a time. This is NOT the bot's rule.
    Exit modes:
      off    -> plain SL or TP at fixed rr
      medium -> BE: lock 0.10*ATR once +0.50*ATR favourable; trail: peak-0.35*ATR once +0.75*ATR
      wide   -> BE: lock 0.15*ATR once +0.75*ATR favourable; trail: peak-0.50*ATR once +1.00*ATR
    R = (exit-entry)*dir / |entry-sl|.
    """
    be_cfg = {
        "off":    (1e9, 0.0),
        "medium": (0.50, 0.10),
        "wide":   (0.75, 0.15),
    }[exit_mode]
    trail_cfg = {
        "off":    (1e9, 0.0),
        "medium": (0.75, 0.35),
        "wide":   (1.00, 0.50),
    }[exit_mode]
    be_trig, be_lock = be_cfg
    tr_act, tr_dist = trail_cfg

    a = atr(df).values
    e20 = ema(df["close"], 20).values
    slope = np.gradient(e20)

    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    n = len(df)

    trades = []
    i = 0
    while i < n - 1:
        # no position -> look for entry at next bar's open based on current slope
        if slope[i] > 0:
            dir = 1
        elif slope[i] < 0:
            dir = -1
        else:
            i += 1
            continue
        entry_bar = i + 1
        if entry_bar >= n:
            break
        entry = o[entry_bar]
        sl_dist = atr_sl_mult * a[i]
        if sl_dist <= 0 or not np.isfinite(sl_dist):
            i += 1
            continue
        sl = entry - dir * sl_dist
        tp = entry + dir * rr * sl_dist
        peak = entry
        be_done = False
        trail_done = False
        exit_price = None
        exit_reason = None
        exit_bar = None
        j = entry_bar
        while j < n:
            # check stops first (intrabar conservative: assume SL hit before TP)
            if dir == 1:
                if l[j] <= sl:
                    exit_price, exit_reason, exit_bar = sl, ("break_even" if be_done else
                                       ("trailing" if trail_done else "stop")), j
                    break
                if h[j] >= tp and not be_done and not trail_done:
                    exit_price, exit_reason, exit_bar = tp, "take_profit", j
                    break
            else:
                if h[j] >= sl:
                    exit_price, exit_reason, exit_bar = sl, ("break_even" if be_done else
                                       ("trailing" if trail_done else "stop")), j
                    break
                if l[j] <= tp and not be_done and not trail_done:
                    exit_price, exit_reason, exit_bar = tp, "take_profit", j
                    break
            # update peak / dynamic exits using high (BUY) or low (SELL)
            fav = (h[j] - entry) if dir == 1 else (entry - l[j])
            peak = max(peak, h[j] if dir == 1 else (2 * entry - l[j]))
            # break-even
            if not be_done and fav >= be_trig * a[i]:
                sl = entry + dir * be_lock * a[i]
                be_done = True
            # trailing
            if not trail_done and fav >= tr_act * a[i]:
                trail_done = True
            if trail_done:
                new_sl = (peak - tr_dist * a[i]) if dir == 1 else (peak + tr_dist * a[i])
                # only favourable
                if dir == 1 and new_sl > sl:
                    sl = new_sl
                if dir == -1 and new_sl < sl:
                    sl = new_sl
            # bar close exit if last bar
            if j == n - 1:
                exit_price, exit_reason, exit_bar = c[j], "close", j
                break
            j += 1
        if exit_price is None:
            i += 1
            continue
        r = (exit_price - entry) * dir / sl_dist
        trades.append({
            "entry_bar": entry_bar, "exit_bar": exit_bar,
            "dir": dir, "entry": entry, "exit": exit_price,
            "sl": sl, "r": r, "reason": exit_reason,
        })
        # advance past exit bar
        i = exit_bar + 1

    if not trades:
        return {"trades": 0, "wr": 0.0, "net_r": 0.0, "pf": 0.0, "ci_lo": 0.0, "rs": []}
    rs = np.array([t["r"] - COST_R for t in trades])
    wins = rs > 0
    gross_win = rs[wins].sum() if wins.any() else 0.0
    gross_loss = -rs[~wins].sum() if (~wins).any() else 0.0
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    n = len(rs)
    mean = rs.mean()
    se = rs.std(ddof=1) / math.sqrt(n) if n > 1 else 0.0
    ci_lo = mean - 1.96 * se
    return {
        "trades": n, "wr": 100 * wins.mean(), "net_r": float(mean),
        "pf": float(pf), "ci_lo": float(ci_lo), "rs": rs.tolist(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSDm")
    ap.add_argument("--start", default="2025-12-09")
    ap.add_argument("--end", default="2026-01-04")
    ap.add_argument("--tf", default="M15")
    args = ap.parse_args()

    df = slice_window(load(args.symbol, args.tf), args.start, args.end)
    print(f"Independent backtest (different engine, same window)")
    print(f"Symbol {args.symbol} {args.tf}  window {df.index[0]} -> {df.index[-1]}  bars={len(df)}")
    print(f"Strategy: EMA20-slope entry | ATR(14)*1.5 SL | {2.0:.1f}R TP | cost {COST_R}R/trade")
    print("-" * 70)
    print(f"{'exit':<8} {'tr':>5} {'wr%':>7} {'netR':>8} {'CIlo':>8} {'PF':>7} {'reasons'}")
    for mode in ("off", "medium", "wide"):
        r = backtest(df, exit_mode=mode)
        from collections import Counter
        rc = Counter(t["reason"] for t in backtest(df, exit_mode=mode).get("rs", [])) if False else ""
        # re-run to get reasons (cheap; window small)
        r2 = backtest(df, exit_mode=mode)
        # count reasons by re-running raw — simpler: derive from rs sign not available; skip
        print(f"{mode:<8} {r['trades']:>5} {r['wr']:>6.1f}% {r['net_r']:>+8.3f} "
              f"{r['ci_lo']:>+8.3f} {r['pf']:>7.2f}")
    print("-" * 70)
    print("NOTE: This is a DIFFERENT strategy from the bot. It tests whether the")
    print("exit-mechanism hypothesis (BE+trail lifts R off the floor) holds under an")
    print("independent engine on the same window, NOT whether it matches the bot's trades.")
    sys.exit(0)


if __name__ == "__main__":
    main()