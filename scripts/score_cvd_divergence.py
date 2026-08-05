"""Standalone OOS scorer — Cumulative Volume Delta (CVD) DIVERGENCE reversal.

NEW-DATA lever (per VERDICT.md, NOT a relabeled OHLCV setup): delta = buy-minus-
sell volume pressure, estimated from tick_volume + candle direction via the
tick rule (Lee-Ready). CVD is the cumulative sum of delta with a daily (UTC)
anchor reset. This is an order-flow *proxy* on Exness CFD tick_volume — wider
error bars than a centralized tape, but a genuinely different data dimension
from OHLCV, so it is worth an honest OOS score.

Setup (divergence reversal, the canonical CVD trade):
  - Bearish divergence: price makes a HIGHER high, CVD makes a LOWER high ->
    rally on thinning aggressive buying -> SELL.
  - Bullish divergence: price makes a LOWER low, CVD makes a HIGHER low ->
    selloff on thinning aggressive selling -> BUY.
Pivots are confirmed L bars after the extreme (no lookahead): fire at the
close of the confirmation bar. SL = support (BUY) / resistance (SELL), tp_r=2R,
intrabar SL-first — same honesty as the existing labeler.

Research-only: no orders, no live state, not added to the live path. Reuses
the labeler's load_bars / label_outcome / _bootstrap_ci95 for comparability.

Run:
    python scripts/score_cvd_divergence.py [--tp-r 2.0] [--pivot-l 5]
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
from scripts.score_volume_profile_setup import (  # noqa: E402
    _atr, _rolling_support_resistance,
)

SETUP_NAME = "cvd_divergence_reversal"
PIVOT_L = 5          # bars left/right to confirm a pivot (5 x M5 = 25 min)
DELTA_SMOOTH = 3     # EMA smoothing of per-bar delta before cumulating (optional)


def _bar_delta(opens, closes, vols):
    """Tick-rule delta proxy: +volume if close>open, -volume if close<open,
    split 0 on doji. Scaled to a unitless signed volume."""
    sign = np.sign(closes - opens)
    return sign * vols


def _cvd(bars, delta):
    """Cumulative Volume Delta with daily (UTC midnight) anchor reset."""
    cvd = np.full(len(bars), np.nan)
    running = 0.0
    prev_day = None
    for i, b in enumerate(bars):
        # b["time"] may be a pandas Timestamp or epoch seconds; daily reset on
        # UTC date rollover.
        t = b["time"]
        day = int(t.timestamp()) // 86400 if hasattr(t, "timestamp") else int(t) // 86400
        if prev_day is not None and day != prev_day:
            running = 0.0
        running += float(delta[i])
        cvd[i] = running
        prev_day = day
    return cvd


def _find_pivots(arr, lo, hi, L, kind):
    """Return list of (pivot_index, pivot_value) confirmed at index+L.
    kind='high' -> local max; 'low' -> local min. Only pivots where the full
    [i-L, i+L] window is within [lo, hi] (no lookahead beyond hi)."""
    pivots = []
    for i in range(lo + L, hi - L + 1):
        left = arr[i - L:i]
        right = arr[i + 1:i + L + 1]
        if kind == "high":
            cond = arr[i] > left.max() and arr[i] >= right.max()
        else:
            cond = arr[i] < left.min() and arr[i] <= right.min()
        if cond:
            pivots.append((i, float(arr[i])))
    return pivots


def score_symbol(symbol, tp_r, pivot_l):
    m5 = load_bars(symbol, "M5")
    if len(m5) < WARMUP_BARS + FORWARD_BARS + 1 + 2 * pivot_l + 4:
        print(f"  {symbol}: insufficient history ({len(m5)} bars)")
        return []
    highs = np.array([b["high"] for b in m5])
    lows = np.array([b["low"] for b in m5])
    closes = np.array([b["close"] for b in m5])
    opens = np.array([b["open"] for b in m5])
    vols = np.array([b["volume"] for b in m5])
    atr = _atr(highs, lows, closes, 14)
    support, resistance = _rolling_support_resistance(lows, highs, 50)

    delta = _bar_delta(opens, closes, vols)
    cvd = _cvd(m5, delta)

    end = len(m5) - FORWARD_BARS
    rows = []
    # Iterate candidate pivot-center indices; confirm at i+pivot_l; fire at i+pivot_l.
    last_pivot_high = None   # (idx, price, cvd)
    last_pivot_low = None
    for i in range(WARMUP_BARS + pivot_l, end - pivot_l):
        fire_idx = i + pivot_l
        if fire_idx >= end:
            break
        if np.isnan(atr[fire_idx]) or atr[fire_idx] <= 0:
            continue
        if np.isnan(support[fire_idx]) or np.isnan(resistance[fire_idx]):
            continue
        # Pivot high at i?
        left_h = highs[i - pivot_l:i]
        right_h = highs[i + 1:i + pivot_l + 1]
        is_ph = highs[i] > left_h.max() and highs[i] >= right_h.max()
        left_l = lows[i - pivot_l:i]
        right_l = lows[i + 1:i + pivot_l + 1]
        is_pl = lows[i] < left_l.min() and lows[i] <= right_l.min()

        side = None
        if is_ph and last_pivot_high is not None:
            # Bearish divergence: price higher high, CVD lower high
            prev_i, prev_p, prev_c = last_pivot_high
            if highs[i] > prev_p and cvd[i] < prev_c:
                side = "SELL"
            last_pivot_high = (i, float(highs[i]), float(cvd[i]))
        elif is_ph:
            last_pivot_high = (i, float(highs[i]), float(cvd[i]))

        if is_pl and last_pivot_low is not None:
            prev_i, prev_p, prev_c = last_pivot_low
            if lows[i] < prev_p and cvd[i] > prev_c:
                if side is None:
                    side = "BUY"
            last_pivot_low = (i, float(lows[i]), float(cvd[i]))
        elif is_pl:
            last_pivot_low = (i, float(lows[i]), float(cvd[i]))

        if side is None:
            continue
        # Cooldown: don't fire on consecutive pivots that overlap the forward window
        entry = float(closes[fire_idx])
        sl = float(support[fire_idx]) if side == "BUY" else float(resistance[fire_idx])
        risk = (entry - sl) if side == "BUY" else (sl - entry)
        if risk <= 0:
            continue
        fwd_bars = [
            {"time": m5[j]["time"], "high": highs[j], "low": lows[j], "close": closes[j]}
            for j in range(fire_idx + 1, fire_idx + 1 + FORWARD_BARS)
        ]
        fire = {
            "side": side, "entry": entry,
            "feat": {"support": float(support[fire_idx]),
                     "resistance": float(resistance[fire_idx]),
                     "cvd": float(cvd[i])},
            "tp_r": tp_r, "symbol": symbol, "setup_type": SETUP_NAME,
            "utc_hour": -1, "confidence": 0.5,
        }
        res = label_outcome(fire, fwd_bars, max_bars=FORWARD_BARS, tp_r=tp_r)
        if res is None:
            continue
        rows.append({"symbol": symbol, "setup_type": SETUP_NAME,
                     "r_multiple": res["r_multiple"], "exit_reason": res["exit_reason"]})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tp-r", type=float, default=DEFAULT_TP_R)
    ap.add_argument("--pivot-l", type=int, default=PIVOT_L)
    args = ap.parse_args()
    symbols = [
        "XAUUSDm", "BTCUSDm", "NAS100m", "JP225m", "US500m", "UK100m", "FR40m",
        "US30m", "USOILm", "AUDUSDm", "GBPUSDm", "EURUSDm", "USDJPYm", "USDCHFm",
    ]
    all_rows = []
    for sym in symbols:
        rows = score_symbol(sym, args.tp_r, args.pivot_l)
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
    print("\n=== OOS SCORE: CVD Divergence Reversal ===")
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