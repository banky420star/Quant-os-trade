"""Standalone OOS scorer — Intermarket Pair DIVERGENCE (zero-crossing model).

NEW-DATA lever (per VERDICT.md): uses a CROSS-SYMBOL relationship — a second
symbol as an anchor — distinct from every single-symbol setup scored so far AND
from the falsified cross-asset rotation (that was relative-strength across a
basket; this is mean-reversion of a specific correlated pair). Method from
Ruggiero's intermarket-divergence framework: smooth both markets, form the
intermarket difference, and trade the TARGET when the difference crosses zero
(the statistically significant trigger — Ruggiero found non-zero thresholds do
NOT improve results). ForexOP: requires |corr| > 0.6, 4H/daily preferred (M5 is
noisier -> we smooth). Academic work shows edge only on cointegrated pairs, so
the honest prior on correlated-but-not-cointegrated CFDs is weak.

For each positively-correlated pair (target A, anchor B):
  * EMA-smooth both closes (period P), z-score each over window W (handles
    different price scales, e.g. US30 ~ 53000 vs NAS100 ~ 20000).
  * spread d = zA - zB (oscillates ~0 for a stable correlation).
  * zero-crossing up   -> A reverting up from weak-vs-B   -> BUY A
  * zero-crossing down -> A reverting down from strong-vs-B -> SELL A
SL = rolling support (BUY) / resistance (SELL) on A, tp_r=2R, intrabar SL-first.
One fire per cross with a cooldown. Research-only, not in the registry.

Run:
    python scripts/score_intermarket_divergence.py [--tp-r 2.0] [--ema 20] [--win 50]
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

SETUP_NAME = "intermarket_divergence_zero_cross"
EMA_P_DEFAULT = 20
WIN_DEFAULT = 50
COOLDOWN = 6  # min bars between fires on the same pair

# Positively-correlated (target, anchor) pairs. Both directions tested where
# the reverse is also a distinct trade.
PAIRS = [
    ("US30m",   "NAS100m"),
    ("NAS100m", "US30m"),
    ("US500m",  "US30m"),
    ("EURUSDm", "GBPUSDm"),
    ("GBPUSDm", "EURUSDm"),
    ("UK100m",  "FR40m"),
    ("FR40m",   "UK100m"),
    ("USOILm",  "XAUUSDm"),
    ("BTCUSDm", "NAS100m"),
]


def _ema(x, period):
    n = len(x)
    out = np.full(n, np.nan)
    if n < period:
        return out
    a = 2.0 / (period + 1)
    out[period - 1] = float(np.mean(x[:period]))
    for i in range(period, n):
        out[i] = a * float(x[i]) + (1 - a) * out[i - 1]
    return out


def _rolling_zscore(x, win):
    n = len(x)
    out = np.full(n, np.nan)
    for i in range(win, n):
        w = x[i - win + 1:i + 1]
        m = float(np.mean(w))
        sd = float(np.std(w))
        out[i] = (float(x[i]) - m) / sd if sd > 1e-12 else 0.0
    return out


def _bar_time_key(t):
    """Stable M5-grid key from a bar time (Timestamp or epoch)."""
    if hasattr(t, "timestamp"):
        ts = int(t.timestamp())
    else:
        ts = int(t)
    return ts  # M5 bars are aligned on the same epoch grid across symbols


def score_pair(target, anchor, tp_r, ema_p, win):
    a_bars = load_bars(target, "M5")
    b_bars = load_bars(anchor, "M5")
    if len(a_bars) < WARMUP_BARS + FORWARD_BARS + 1 + win + ema_p:
        return []
    # Build anchor time -> bar index for alignment.
    b_idx = {}
    for j, b in enumerate(b_bars):
        b_idx[_bar_time_key(b["time"])] = j
    a_highs = np.array([b["high"] for b in a_bars])
    a_lows = np.array([b["low"] for b in a_bars])
    a_closes = np.array([b["close"] for b in a_bars])
    b_closes = np.array([b["close"] for b in b_bars])
    a_atr = _atr(a_highs, a_lows, a_closes, 14)
    a_sup, a_res = _rolling_support_resistance(a_lows, a_highs, 50)

    a_ema = _ema(a_closes, ema_p)
    b_ema = _ema(b_closes, ema_p)
    a_z = _rolling_zscore(a_ema, win)
    b_z = _rolling_zscore(b_ema, win)

    end = len(a_bars) - FORWARD_BARS
    rows = []
    last_fire = -10_000
    prev_d = None
    for i in range(WARMUP_BARS + win, end):
        # need aligned anchor bar at the same M5 timestamp
        key = _bar_time_key(a_bars[i]["time"])
        if key not in b_idx:
            prev_d = None
            continue
        j = b_idx[key]
        if np.isnan(a_z[i]) or np.isnan(b_z[j]):
            prev_d = None
            continue
        d = a_z[i] - b_z[j]
        if prev_d is None:
            prev_d = d
            continue
        side = None
        if prev_d < 0.0 and d >= 0.0:
            side = "BUY"
        elif prev_d > 0.0 and d <= 0.0:
            side = "SELL"
        prev_d = d
        if side is None:
            continue
        if i - last_fire < COOLDOWN:
            continue
        if np.isnan(a_atr[i]) or a_atr[i] <= 0:
            continue
        if np.isnan(a_sup[i]) or np.isnan(a_res[i]):
            continue
        entry = float(a_closes[i])
        sl = float(a_sup[i]) if side == "BUY" else float(a_res[i])
        risk = (entry - sl) if side == "BUY" else (sl - entry)
        if risk <= 0:
            continue
        last_fire = i
        fwd_bars = [
            {"time": a_bars[k]["time"], "high": a_highs[k], "low": a_lows[k], "close": a_closes[k]}
            for k in range(i + 1, i + 1 + FORWARD_BARS)
        ]
        fire = {
            "side": side, "entry": entry,
            "feat": {"support": float(a_sup[i]), "resistance": float(a_res[i]),
                     "anchor": anchor},
            "tp_r": tp_r, "symbol": target, "setup_type": SETUP_NAME,
            "utc_hour": -1, "confidence": 0.5,
        }
        res = label_outcome(fire, fwd_bars, max_bars=FORWARD_BARS, tp_r=tp_r)
        if res is not None:
            rows.append({"symbol": target, "setup_type": SETUP_NAME,
                         "r_multiple": res["r_multiple"], "exit_reason": res["exit_reason"]})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tp-r", type=float, default=DEFAULT_TP_R)
    ap.add_argument("--ema", type=int, default=EMA_P_DEFAULT)
    ap.add_argument("--win", type=int, default=WIN_DEFAULT)
    args = ap.parse_args()
    all_rows = []
    for target, anchor in PAIRS:
        rows = score_pair(target, anchor, args.tp_r, args.ema, args.win)
        all_rows.extend(rows)
        n = len(rows)
        if n:
            wr = 100 * sum(1 for r in rows if r["r_multiple"] > 0) / n
            exp = sum(r["r_multiple"] for r in rows) / n
            print(f"  {target}<-{anchor}: fires={n} win_rate={wr:.1f}% exp={exp:+.4f}R")
        else:
            print(f"  {target}<-{anchor}: fires=0")
    cells = defaultdict(list)
    for r in all_rows:
        cells[(r["symbol"], r["setup_type"])].append(r["r_multiple"])
    print("\n=== OOS SCORE: Intermarket Divergence (zero-crossing) ===")
    print(f"{'cell':<22} {'n':>6} {'win%':>6} {'expR':>8} {'ci95_lo':>8} {'ci95_hi':>8} {'gate':>6}")
    cleared = []
    for (sym, setup), rs in sorted(cells.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        ci = _bootstrap_ci95(rs, reps=1000) if n >= 2 else None
        wr = 100 * sum(1 for r in rs if r > 0) / n if n else 0
        exp = sum(rs) / n if n else 0
        lo = ci[0] if ci else float("nan")
        hi = ci[1] if ci else float("nan")
        gate = n >= 8 and exp > 0 and ci is not None and ci[0] > 0
        print(f"{sym:<22} {n:>6} {wr:>6.1f} {exp:>+8.4f} {lo:>+8.4f} {hi:>+8.4f} {'YES' if gate else 'no':>6}")
        if gate:
            cleared.append((sym, n, exp, lo, hi))
    total_n = len(all_rows)
    total_exp = sum(r["r_multiple"] for r in all_rows) / total_n if total_n else 0.0
    print(f"\nTOTAL fires={total_n} pooled_exp={total_exp:+.4f}R  cells_clearing_gate={len(cleared)}/{len(cells)}")
    print("CAVEAT: per-cell CI95 lo>0 is selection-biased; NOT a deploy signal. R-multiples PRE-COST.")
    if cleared:
        print("CANDIDATES (shadow only):")
        for sym, n, exp, lo, hi in cleared:
            print(f"  {sym}: n={n} exp={exp:+.4f} ci95=[{lo:+.4f},{hi:+.4f}]")
    else:
        print("No cell clears the gate — honest no-edge; NOT wiring in.")


if __name__ == "__main__":
    main()