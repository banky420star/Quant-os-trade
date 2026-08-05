"""Standalone OOS scorer — Opening Range Breakout (ORB).

A TIME/SESSION-ANCHORED structural trigger (distinct from the falsified
rolling-OHLCV relabel space): define each symbol's opening range as the first
``OR_BARS`` M5 bars after its cash-session open, then fire on the first close
that breaks the OR high (BUY) / low (SELL). SL = opposite end of the OR, tp_r=2R,
intrabar SL-first — same honesty as the labeler. One fire per day (first break).

Session opens are per-symbol (UTC), the real cash opens — NOT a generic
UTC-midnight, which would make the "opening range" meaningless for US/EU
indices. FX/metals (24h) anchor to the Asian session start (00:00 UTC).

Research-only: no orders, no live state, not in the registry. Reuses the
labeler's load_bars / label_outcome / _bootstrap_ci95 for comparability.

Run:
    python scripts/score_opening_range_breakout.py [--tp-r 2.0] [--or-bars 12]
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from core.specialized_replay_labeler import (  # noqa: E402
    load_bars, _bootstrap_ci95, WARMUP_BARS, FORWARD_BARS, DEFAULT_TP_R,
)
from core.specialized_outcome_labeler import label_outcome  # noqa: E402

SETUP_NAME = "opening_range_breakout"
OR_BARS_DEFAULT = 12        # 12 x M5 = 60 min opening range ("first hour")
MAX_TRADE_BARS = 48         # only fire within 4h after the OR completes

# Per-symbol cash-session open, UTC (hour, minute).
SESSION_OPEN_UTC = {
    "JP225m":  (0, 0),     # Tokyo 09:00 JST
    "AUDUSDm": (0, 0),     # Sydney/Tokyo Asian start (FX 24h -> anchor 00:00)
    "US30m":   (13, 30),   # US cash open 09:30 ET
    "NAS100m": (13, 30),
    "US500m":  (13, 30),
    "UK100m":  (8, 0),     # London 09:00 BST
    "FR40m":   (8, 0),
    "XAUUSDm": (0, 0),     # gold 24h -> anchor 00:00
    "USOILm":  (0, 0),
    "GBPUSDm": (0, 0),
    "EURUSDm": (0, 0),
    "USDJPYm": (0, 0),
    "USDCHFm": (0, 0),
}


def _bar_utc_hm(t):
    """Return (hour, minute, day_epoch) for a bar time (Timestamp or epoch)."""
    if hasattr(t, "timestamp"):
        import datetime as _dt
        ts = int(t.timestamp())
    else:
        ts = int(t)
    day = ts // 86400
    sod = day * 86400
    rem = ts - sod
    return rem // 3600, (rem % 3600) // 60, day


def score_symbol(symbol, tp_r, or_bars):
    m5 = load_bars(symbol, "M5")
    if len(m5) < WARMUP_BARS + FORWARD_BARS + 1 + or_bars + MAX_TRADE_BARS:
        print(f"  {symbol}: insufficient history ({len(m5)} bars)")
        return []
    highs = np.array([b["high"] for b in m5])
    lows = np.array([b["low"] for b in m5])
    closes = np.array([b["close"] for b in m5])
    oh, om = SESSION_OPEN_UTC.get(symbol, (0, 0))
    open_min_of_day = oh * 60 + om

    rows = []
    # Walk bars, group by UTC day, find session open, define OR, then scan for break.
    i = 0
    n = len(m5)
    end = n - FORWARD_BARS  # need FORWARD_BARS bars after a fire to label
    while i < end:
        h, m_, day = _bar_utc_hm(m5[i]["time"])
        cur_min = h * 60 + m_
        # advance to the first bar at/after the session open on this day
        if cur_min < open_min_of_day:
            i += 1
            continue
        open_idx = i
        # need or_bars bars still in the same UTC day
        or_end = open_idx + or_bars - 1
        if or_end >= end:
            break
        # if OR crosses into next UTC day, abort this day and skip to next
        _, _, day_or_end = _bar_utc_hm(m5[or_end]["time"])
        if day_or_end != day:
            i = or_end + 1
            continue
        or_high = float(highs[open_idx:or_end + 1].max())
        or_low = float(lows[open_idx:or_end + 1].min())
        if or_high <= or_low:
            i = or_end + 1
            continue
        # scan the trade window for the first breakout
        window_end = min(or_end + MAX_TRADE_BARS, end - 1)
        fired = False
        for j in range(or_end + 1, window_end + 1):
            hj, mj, dj = _bar_utc_hm(m5[j]["time"])
            if dj != day:  # don't fire into next day
                break
            c = float(closes[j])
            if c > or_high:
                side = "BUY"; sl = or_low; entry = c
            elif c < or_low:
                side = "SELL"; sl = or_high; entry = c
            else:
                continue
            risk = (entry - sl) if side == "BUY" else (sl - entry)
            if risk <= 0:
                continue
            fwd_bars = [
                {"time": m5[k]["time"], "high": highs[k], "low": lows[k], "close": closes[k]}
                for k in range(j + 1, j + 1 + FORWARD_BARS)
            ]
            fire = {
                "side": side, "entry": entry,
                # label_outcome computes SL from feat support(BUY)/resistance(SELL),
                # so map OR low->support, OR high->resistance (SL = opposite OR end).
                "feat": {"support": or_low, "resistance": or_high,
                         "or_high": or_high, "or_low": or_low},
                "tp_r": tp_r, "symbol": symbol, "setup_type": SETUP_NAME,
                "utc_hour": hj, "confidence": 0.5,
            }
            res = label_outcome(fire, fwd_bars, max_bars=FORWARD_BARS, tp_r=tp_r)
            if res is not None:
                rows.append({"symbol": symbol, "setup_type": SETUP_NAME,
                             "r_multiple": res["r_multiple"], "exit_reason": res["exit_reason"]})
            fired = True
            break  # one fire per day (first break)
        # advance to next day
        i = window_end + 1
        # skip forward to next UTC day boundary if still same day
        while i < end:
            _, _, d2 = _bar_utc_hm(m5[i]["time"])
            if d2 != day:
                break
            i += 1
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tp-r", type=float, default=DEFAULT_TP_R)
    ap.add_argument("--or-bars", type=int, default=OR_BARS_DEFAULT)
    args = ap.parse_args()
    symbols = [
        "XAUUSDm", "BTCUSDm", "NAS100m", "JP225m", "US500m", "UK100m", "FR40m",
        "US30m", "USOILm", "AUDUSDm", "GBPUSDm", "EURUSDm", "USDJPYm", "USDCHFm",
    ]
    all_rows = []
    for sym in symbols:
        rows = score_symbol(sym, args.tp_r, args.or_bars)
        all_rows.extend(rows)
        n = len(rows)
        if n:
            wr = 100 * sum(1 for r in rows if r["r_multiple"] > 0) / n
            exp = sum(r["r_multiple"] for r in rows) / n
            print(f"  {sym}: fires={n} win_rate={wr:.1f}% exp={exp:+.4f}R")
        else:
            print(f"  {sym}: fires=0")
    cells = defaultdict(list)
    for r in all_rows:
        cells[(r["symbol"], r["setup_type"])].append(r["r_multiple"])
    print("\n=== OOS SCORE: Opening Range Breakout (first-hour) ===")
    print(f"{'symbol':<10} {'n':>6} {'win%':>6} {'expR':>8} {'ci95_lo':>8} {'ci95_hi':>8} {'gate':>6}")
    cleared = []
    for (sym, setup), rs in sorted(cells.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        ci = _bootstrap_ci95(rs, reps=1000) if n >= 2 else None
        wr = 100 * sum(1 for r in rs if r > 0) / n
        exp = sum(rs) / n
        lo = ci[0] if ci else float("nan")
        hi = ci[1] if ci else float("nan")
        gate = n >= 8 and exp > 0 and ci is not None and ci[0] > 0
        print(f"{sym:<10} {n:>6} {wr:>6.1f} {exp:>+8.4f} {lo:>+8.4f} {hi:>+8.4f} {'YES' if gate else 'no':>6}")
        if gate:
            cleared.append((sym, n, exp, lo, hi))
    total_n = len(all_rows)
    total_exp = sum(r["r_multiple"] for r in all_rows) / total_n if total_n else 0.0
    print(f"\nTOTAL fires={total_n} pooled_exp={total_exp:+.4f}R  cells_clearing_gate={len(cleared)}/{len(cells)}")
    print("CAVEAT: per-cell CI95 lo>0 is selection-biased across %d cells; NOT a deploy signal." % len(cells))
    if cleared:
        print("CANDIDATES (shadow-catalog only, not live):")
        for sym, n, exp, lo, hi in cleared:
            print(f"  {sym}: n={n} exp={exp:+.4f} ci95=[{lo:+.4f},{hi:+.4f}]")
    else:
        print("No cell clears the gate — honest no-edge result; NOT wiring into the registry.")


if __name__ == "__main__":
    main()