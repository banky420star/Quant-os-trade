"""Standalone OOS scorer — Opening Range Breakout WITH FILTERS.

Follows the raw-ORB score (score_opening_range_breakout.py), which was weakly
positive pre-cost (pooled +0.005R) but no cell cleared the gate. The 2026
literature unanimously says raw ORB is coin-flip and FILTERS unlock the edge:
volume confirmation is the strongest single filter (Finexus: 51.4->63.8% wr,
PF 1.10->1.85), VWAP alignment is weaker/regime-dependent (Vortex: +0.4% longs,
+3.3% shorts), and the stack VWAP+volume+retest yields PF 1.47 / Sharpe 2.28 on
NQ with a LOW 33.9% win rate (edge in expectancy, not hit rate).

Per GrandAlgo "add filters one at a time" discipline, this scores THREE variants
in one run for honest attribution vs the raw baseline:
  A) vwap_only   — BUY needs close>VWAP & VWAP slope>0; SELL the mirror.
  B) vol_only    — breakout bar volume >= 1.5x the prior 20-bar avg.
  C) vwap+vol    — both gates.

Same honesty as the labeler: session VWAP reset at the cash-session open, SL =
opposite OR end, tp_r=2R, intrabar SL-first. Research-only, not in the registry.

Run:
    python scripts/score_orb_filtered.py [--tp-r 2.0] [--or-bars 12]
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
from scripts.score_opening_range_breakout import (  # noqa: E402
    _bar_utc_hm, SESSION_OPEN_UTC, OR_BARS_DEFAULT, MAX_TRADE_BARS,
)

VOL_MULT = 1.5          # breakout volume >= 1.5x prior 20-bar avg
VOL_LOOKBACK = 20
SETUP_PREFIX = "orb_filtered"


def _session_vwap(bars, opens, highs, lows, closes, vols, open_min_of_day):
    """Per-bar session VWAP, reset at the cash-session open (UTC)."""
    n = len(bars)
    vwap = np.full(n, np.nan)
    cum_pv = 0.0
    cum_v = 0.0
    prev_day = None
    for i in range(n):
        h, m_, day = _bar_utc_hm(bars[i]["time"])
        cur_min = h * 60 + m_
        # reset at the first bar at/after the session open of a new day
        if prev_day is None or day != prev_day:
            # start fresh at session open; if this bar is before open, carry NaN
            if cur_min >= open_min_of_day:
                cum_pv = 0.0
                cum_v = 0.0
                prev_day = day
            else:
                vwap[i] = np.nan
                continue
        tp = (highs[i] + lows[i] + closes[i]) / 3.0
        cum_pv += float(tp * vols[i])
        cum_v += float(vols[i])
        vwap[i] = cum_pv / cum_v if cum_v > 0 else np.nan
    return vwap


def _rolling_vol_avg(vols, period=VOL_LOOKBACK):
    n = len(vols)
    out = np.full(n, np.nan)
    for i in range(period, n):
        out[i] = float(np.mean(vols[i - period:i]))
    return out


def score_symbol(symbol, tp_r, or_bars, variant):
    m5 = load_bars(symbol, "M5")
    if len(m5) < WARMUP_BARS + FORWARD_BARS + 1 + or_bars + MAX_TRADE_BARS:
        return []
    highs = np.array([b["high"] for b in m5])
    lows = np.array([b["low"] for b in m5])
    closes = np.array([b["close"] for b in m5])
    opens = np.array([b["open"] for b in m5])
    vols = np.array([b["volume"] for b in m5])
    oh, om = SESSION_OPEN_UTC.get(symbol, (0, 0))
    open_min_of_day = oh * 60 + om

    vwap = _session_vwap(m5, opens, highs, lows, closes, vols, open_min_of_day)
    vol_avg = _rolling_vol_avg(vols, VOL_LOOKBACK)

    n = len(m5)
    end = n - FORWARD_BARS
    rows = []
    i = 0
    while i < end:
        h, m_, day = _bar_utc_hm(m5[i]["time"])
        cur_min = h * 60 + m_
        if cur_min < open_min_of_day:
            i += 1
            continue
        open_idx = i
        or_end = open_idx + or_bars - 1
        if or_end >= end:
            break
        _, _, d2 = _bar_utc_hm(m5[or_end]["time"])
        if d2 != day:
            i = or_end + 1
            continue
        or_high = float(highs[open_idx:or_end + 1].max())
        or_low = float(lows[open_idx:or_end + 1].min())
        if or_high <= or_low:
            i = or_end + 1
            continue
        window_end = min(or_end + MAX_TRADE_BARS, end - 1)
        for j in range(or_end + 1, window_end + 1):
            hj, mj, dj = _bar_utc_hm(m5[j]["time"])
            if dj != day:
                break
            c = float(closes[j])
            if c > or_high:
                side = "BUY"; sl = or_low; entry = c
            elif c < or_low:
                side = "SELL"; sl = or_high; entry = c
            else:
                continue
            # ---- apply variant filter ----
            if variant == "vwap_only":
                if np.isnan(vwap[j]) or np.isnan(vwap[j - 1]):
                    continue
                slope_up = vwap[j] > vwap[j - 1]
                if side == "BUY" and not (c > vwap[j] and slope_up):
                    continue
                if side == "SELL" and not (c < vwap[j] and not slope_up):
                    continue
            elif variant == "vol_only":
                if np.isnan(vol_avg[j]) or vol_avg[j] <= 0:
                    continue
                if vols[j] < VOL_MULT * vol_avg[j]:
                    continue
            elif variant == "vwap_vol":
                if np.isnan(vwap[j]) or np.isnan(vwap[j - 1]):
                    continue
                slope_up = vwap[j] > vwap[j - 1]
                vwap_ok = (c > vwap[j] and slope_up) if side == "BUY" else (c < vwap[j] and not slope_up)
                if not vwap_ok:
                    continue
                if np.isnan(vol_avg[j]) or vol_avg[j] <= 0:
                    continue
                if vols[j] < VOL_MULT * vol_avg[j]:
                    continue
            # ---- label ----
            risk = (entry - sl) if side == "BUY" else (sl - entry)
            if risk <= 0:
                continue
            fwd_bars = [
                {"time": m5[k]["time"], "high": highs[k], "low": lows[k], "close": closes[k]}
                for k in range(j + 1, j + 1 + FORWARD_BARS)
            ]
            fire = {
                "side": side, "entry": entry,
                "feat": {"support": or_low, "resistance": or_high},
                "tp_r": tp_r, "symbol": symbol,
                "setup_type": f"{SETUP_PREFIX}_{variant}",
                "utc_hour": hj, "confidence": 0.5,
            }
            res = label_outcome(fire, fwd_bars, max_bars=FORWARD_BARS, tp_r=tp_r)
            if res is not None:
                rows.append({"symbol": symbol,
                             "setup_type": f"{SETUP_PREFIX}_{variant}",
                             "r_multiple": res["r_multiple"]})
            break  # one fire per day
        i = window_end + 1
        while i < end:
            _, _, dd = _bar_utc_hm(m5[i]["time"])
            if dd != day:
                break
            i += 1
    return rows


def _report(label, all_rows):
    cells = defaultdict(list)
    for r in all_rows:
        cells[(r["symbol"], r["setup_type"])].append(r["r_multiple"])
    print(f"\n=== OOS SCORE: ORB filter={label} ===")
    print(f"{'symbol':<10} {'n':>6} {'win%':>6} {'expR':>8} {'ci95_lo':>8} {'ci95_hi':>8} {'gate':>6}")
    cleared = []
    for (sym, _), rs in sorted(cells.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        ci = _bootstrap_ci95(rs, reps=1000) if n >= 2 else None
        wr = 100 * sum(1 for r in rs if r > 0) / n if n else 0
        exp = sum(rs) / n if n else 0
        lo = ci[0] if ci else float("nan")
        hi = ci[1] if ci else float("nan")
        gate = n >= 8 and exp > 0 and ci is not None and ci[0] > 0
        print(f"{sym:<10} {n:>6} {wr:>6.1f} {exp:>+8.4f} {lo:>+8.4f} {hi:>+8.4f} {'YES' if gate else 'no':>6}")
        if gate:
            cleared.append((sym, n, exp, lo, hi))
    total_n = len(all_rows)
    total_exp = sum(r["r_multiple"] for r in all_rows) / total_n if total_n else 0.0
    print(f"TOTAL fires={total_n} pooled_exp={total_exp:+.4f}R  cells_clearing_gate={len(cleared)}/{len(cells)}")
    if cleared:
        print("CANDIDATES (shadow only):")
        for sym, n, exp, lo, hi in cleared:
            print(f"  {sym}: n={n} exp={exp:+.4f} ci95=[{lo:+.4f},{hi:+.4f}]")
    else:
        print("No cell clears the gate — honest no-edge; NOT wiring in.")
    return total_n, total_exp, len(cleared), len(cells)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tp-r", type=float, default=DEFAULT_TP_R)
    ap.add_argument("--or-bars", type=int, default=OR_BARS_DEFAULT)
    args = ap.parse_args()
    symbols = [
        "XAUUSDm", "BTCUSDm", "NAS100m", "JP225m", "US500m", "UK100m", "FR40m",
        "US30m", "USOILm", "AUDUSDm", "GBPUSDm", "EURUSDm", "USDJPYm", "USDCHFm",
    ]
    variants = ["vwap_only", "vol_only", "vwap_vol"]
    print("Baseline (raw ORB, prior tick): pooled +0.0051R, 0/14 cells.")
    for v in variants:
        all_rows = []
        for sym in symbols:
            all_rows.extend(score_symbol(sym, args.tp_r, args.or_bars, v))
        _report(v, all_rows)
    print("\nCAVEAT: per-cell CI95 lo>0 is selection-biased across cells; NOT a deploy signal.")
    print("Caveat: R-multiples are PRE-COST (no 30bps deduction) — net expectancy is lower.")


if __name__ == "__main__":
    main()