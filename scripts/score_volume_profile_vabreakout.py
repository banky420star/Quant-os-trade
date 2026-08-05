"""Standalone OOS scorer — Session Volume Profile VALUE-AREA BREAKOUT.

Second trigger on the same new-data (tick-volume profile) layer as
score_volume_profile_setup.py (which scored POC-rejection). Reuses the
labeler's load_bars / label_outcome / _bootstrap_ci95 for comparability.

Trigger: fire BUY when close breaks above VAH (value-area high) with
volume_ratio > vol_threshold; SELL when close breaks below VAL with
volume_ratio > vol_threshold. SL = support (BUY) / resistance (SELL),
tp_r = 2R, intrabar SL-first — same honesty as the labeler.

Research-only: no orders, no live state, not added to the live path.
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
    _atr, _rolling_support_resistance, _session_volume_profile,
    PROFILE_BARS, N_BINS,
)

SETUP_NAME = "session_volume_profile_va_breakout"
VOL_THRESHOLD = 1.2  # break needs 1.2x average tick volume confirmation


def _rolling_volume_avg(vols, period=20):
    n = len(vols)
    out = np.full(n, np.nan)
    for i in range(period, n):
        out[i] = float(np.mean(vols[i - period:i]))
    return out


def score_symbol(symbol, tp_r, profile_bars):
    m5 = load_bars(symbol, "M5")
    if len(m5) < WARMUP_BARS + FORWARD_BARS + 1 + profile_bars:
        print(f"  {symbol}: insufficient history ({len(m5)} bars)")
        return []
    highs = np.array([b["high"] for b in m5])
    lows = np.array([b["low"] for b in m5])
    closes = np.array([b["close"] for b in m5])
    vols = np.array([b["volume"] for b in m5])
    atr = _atr(highs, lows, closes, 14)
    support, resistance = _rolling_support_resistance(lows, highs, 50)
    vol_avg = _rolling_volume_avg(vols, 20)

    end = len(m5) - FORWARD_BARS
    rows = []
    for i in range(WARMUP_BARS + profile_bars, end):
        if np.isnan(atr[i]) or atr[i] <= 0 or np.isnan(support[i]) or np.isnan(resistance[i]):
            continue
        if np.isnan(vol_avg[i]) or vol_avg[i] <= 0:
            continue
        prof = _session_volume_profile(m5, i, profile_bars, N_BINS)
        if prof is None:
            continue
        vah, val = prof["vah"], prof["val"]
        vr = vols[i] / vol_avg[i]
        if vr < VOL_THRESHOLD:
            continue
        c = closes[i]
        if c > vah:
            side = "BUY"
        elif c < val:
            side = "SELL"
        else:
            continue
        entry = float(c)
        sl = float(support[i]) if side == "BUY" else float(resistance[i])
        risk = (entry - sl) if side == "BUY" else (sl - entry)
        if risk <= 0:
            continue
        fwd_bars = [
            {"time": m5[j]["time"], "high": highs[j], "low": lows[j], "close": closes[j]}
            for j in range(i + 1, i + 1 + FORWARD_BARS)
        ]
        fire = {
            "side": side, "entry": entry,
            "feat": {"support": float(support[i]), "resistance": float(resistance[i])},
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
    ap.add_argument("--profile-bars", type=int, default=PROFILE_BARS)
    args = ap.parse_args()
    symbols = [
        "XAUUSDm", "BTCUSDm", "NAS100m", "JP225m", "US500m", "UK100m", "FR40m",
        "US30m", "USOILm", "AUDUSDm", "GBPUSDm", "EURUSDm", "USDJPYm", "USDCHFm",
    ]
    all_rows = []
    for sym in symbols:
        rows = score_symbol(sym, args.tp_r, args.profile_bars)
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
    print("\n=== OOS SCORE: Session Volume Profile VA-BREAKOUT ===")
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