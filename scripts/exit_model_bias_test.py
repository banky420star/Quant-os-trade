"""Decisive test: is the bot's 'exits help' finding an artifact of its
close-to-close exit model?

The bot's PaperBroker._check_exits evaluates peak/BE/trail/SL/TP all against a
single per-bar price (the close) and never inspects intrabar high/low. That
misses intrabar stop-runs and trailing-stop whipsaw. An independent engine using
intrabar high/low checks found exits HURT; the bot (close-to-close) found exits
HELP. This script runs the SAME simple strategy in BOTH exit models to see if
the exit model alone flips the verdict.

  intrabar  (realistic): check bar high/low against SL/TP; update peak from high/low.
  close     (bot-like)  : check only the close against SL/TP; update peak/BE/trail from close.

If close-to-close flips exits from hurt -> help, the bot's +0.34R is an exit-model
artifact, not a real edge. NO LIVE TRADING.
"""
from __future__ import annotations
import math
from pathlib import Path
import numpy as np
import pandas as pd

HIST = Path(__file__).resolve().parents[1] / "data" / "history"
COST_R = 0.15


def load(symbol, tf="M15"):
    df = pd.read_parquet(HIST / f"{symbol}_{tf}.parquet")
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time").sort_index()[["open", "high", "low", "close", "volume"]].astype(float)


def atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h - l), (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def ema(s, n):
    return s.ewm(alpha=1 / n, adjust=False).mean()


def slice_win(df, start, end):
    return df.loc[(df.index >= pd.Timestamp(start, tz="UTC")) & (df.index < pd.Timestamp(end, tz="UTC"))]


def backtest(df, *, exit_mode, exit_model, rr=2.0, atr_sl=1.5):
    be_trig, be_lock = {"off": (1e9, 0.0), "medium": (0.50, 0.10), "wide": (0.75, 0.15)}[exit_mode]
    tr_act, tr_dist = {"off": (1e9, 0.0), "medium": (0.75, 0.35), "wide": (1.00, 0.50)}[exit_mode]
    a = atr(df).values; e20 = ema(df["close"], 20).values; slope = np.gradient(e20)
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    trades = []
    i = 0
    while i < n - 1:
        d = 1 if slope[i] > 0 else (-1 if slope[i] < 0 else 0)
        if d == 0:
            i += 1; continue
        eb = i + 1
        if eb >= n: break
        entry = o[eb]
        sd = atr_sl * a[i]
        if sd <= 0 or not np.isfinite(sd):
            i += 1; continue
        sl = entry - d * sd
        tp = entry + d * rr * sd
        peak = entry; be_done = False; trail_done = False
        exit_price = None; exit_reason = None; exit_bar = None
        j = eb
        while j < n:
            # reference price for peak/BE/trail triggers
            trig_price = c[j] if exit_model == "close" else (h[j] if d == 1 else l[j])
            fav = (trig_price - entry) * d
            if d == 1:
                peak = max(peak, trig_price)
            else:
                peak = min(peak, trig_price)
            if not be_done and fav >= be_trig * a[i]:
                sl = entry + d * be_lock * a[i]; be_done = True
            if not trail_done and fav >= tr_act * a[i]:
                trail_done = True
            if trail_done:
                new_sl = peak - d * tr_dist * a[i]
                if (d == 1 and new_sl > sl) or (d == -1 and new_sl < sl):
                    sl = new_sl
            # exit check
            if exit_model == "close":
                px = c[j]
                if d == 1 and px <= sl:
                    exit_price, exit_reason, exit_bar = sl, ("trail" if trail_done else ("be" if be_done else "stop")), j; break
                if d == 1 and px >= tp and not be_done and not trail_done:
                    exit_price, exit_reason, exit_bar = tp, "tp", j; break
                if d == -1 and px >= sl:
                    exit_price, exit_reason, exit_bar = sl, ("trail" if trail_done else ("be" if be_done else "stop")), j; break
                if d == -1 and px <= tp and not be_done and not trail_done:
                    exit_price, exit_reason, exit_bar = tp, "tp", j; break
            else:  # intrabar: assume SL hit before TP if both in range (conservative)
                if d == 1:
                    if l[j] <= sl:
                        exit_price, exit_reason, exit_bar = sl, ("trail" if trail_done else ("be" if be_done else "stop")), j; break
                    if h[j] >= tp and not be_done and not trail_done:
                        exit_price, exit_reason, exit_bar = tp, "tp", j; break
                else:
                    if h[j] >= sl:
                        exit_price, exit_reason, exit_bar = sl, ("trail" if trail_done else ("be" if be_done else "stop")), j; break
                    if l[j] <= tp and not be_done and not trail_done:
                        exit_price, exit_reason, exit_bar = tp, "tp", j; break
            if j == n - 1:
                exit_price, exit_reason, exit_bar = c[j], "close", j; break
            j += 1
        if exit_price is None:
            i += 1; continue
        r = (exit_price - entry) * d / sd
        trades.append(r)
        i = exit_bar + 1
    if not trades:
        return (0, 0.0, 0.0, 0.0, 0.0)
    rs = np.array(trades) - COST_R
    n = len(rs); mean = rs.mean()
    se = rs.std(ddof=1) / math.sqrt(n) if n > 1 else 0.0
    pf = (rs[rs > 0].sum() / -rs[rs < 0].sum()) if (rs < 0).any() else float("inf")
    return (n, 100 * (rs > 0).mean(), float(mean), float(mean - 1.96 * se), float(pf))


def main():
    df = slice_win(load("XAUUSDm"), "2025-12-09", "2026-01-04")
    print(f"XAUUSDm M15  2025-12-09 -> 2026-01-04  bars={len(df)}  cost={COST_R}R")
    print("Same strategy (EMA20-slope + ATR*1.5 SL + 2R TP). Only the exit MODEL differs.")
    print("-" * 78)
    print(f"{'exit_model':<11}{'exit':<8}{'tr':>5}{'wr%':>7}{'netR':>8}{'CIlo':>8}{'PF':>7}")
    print("-" * 78)
    for model in ("intrabar", "close"):
        for mode in ("off", "medium", "wide"):
            tr, wr, nr, ci, pf = backtest(df, exit_mode=mode, exit_model=model)
            print(f"{model:<11}{mode:<8}{tr:>5}{wr:>6.1f}%{nr:>+8.3f}{ci:>+8.3f}{pf:>7.2f}")
        print("-" * 78)
    print()
    print("READ: if 'close' shows medium/wide >> off (exits help) while 'intrabar' shows")
    print("medium/wide <= off (exits don't help), the bot's exit edge is an artifact of")
    print("its close-to-close exit model, NOT a real tradeable edge.")


if __name__ == "__main__":
    main()