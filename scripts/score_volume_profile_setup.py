"""Standalone OOS scorer for a NEW setup family: Session Volume Profile
POC-rejection (tick-volume based — the "new data" lever per VERDICT.md, NOT a
relabeled OHLCV setup).

Honesty contract: reuses the SAME data loader (load_bars), outcome labeler
(label_outcome: intrabar SL-first, tp_r=2R, SL=support/resistance), and
bootstrap CI95 (specialized_replay_labeler._bootstrap_ci95) as the existing
42 specialized-setup iterations, so the numbers are directly comparable.

This is a RESEARCH script. It places no orders, writes no live state, and does
NOT add anything to the live strategy path. If a cell clears the gate
(n>=8 + exp>0 + CI95 lo>0) it's a CANDIDATE for wiring into the
specialized_setups registry as a shadow setup — not a deploy signal
(per-cell CI lo>0 is selection-biased across ~N cells; DSR/SPA is the real bar).

Setup: for each M5 bar, build a rolling session volume profile over the prior
``profile_bars`` bars (default 96 = 8h ≈ one session). POC = price bin with max
tick volume. VAH/VAL = 70% value area around POC. Fire POC-rejection when the
bar's wick touches POC and closes back on the away side (bullish close > POC
after dipping to it -> BUY; bearish close < POC after spiking to it -> SELL).

Run:
    python scripts/score_volume_profile_setup.py [--tp-r 2.0] [--profile-bars 96]
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

# Reuse the labeler's own data loader, outcome labeler, and bootstrap CI95
# so results are directly comparable to the existing 42 iterations.
from core.specialized_replay_labeler import (  # noqa: E402
    load_bars, _bootstrap_ci95, WARMUP_BARS, FORWARD_BARS, DEFAULT_TP_R,
)
from core.specialized_outcome_labeler import label_outcome  # noqa: E402


SETUP_NAME = "session_volume_profile_poc_rejection"
PROFILE_BARS = 96      # 96 x M5 = 8h rolling session window
N_BINS = 50            # price-level bins for the volume profile
VALUE_AREA_PCT = 0.70  # POC-centered value area
NEAR_POC_ATR = 0.5     # "wick touched POC" tolerance in ATR units


def _atr(highs, lows, closes, period=14):
    atr = np.full(len(highs), np.nan)
    if len(highs) < period + 1:
        return atr
    tr = np.maximum(
        highs - lows,
        np.maximum(
            np.abs(highs - np.roll(closes, 1)),
            np.abs(lows - np.roll(closes, 1)),
        ),
    )
    tr[0] = highs[0] - lows[0]
    # Wilder smoothing
    atr[period] = np.mean(tr[1:period + 1])
    for i in range(period + 1, len(highs)):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def _rolling_support_resistance(lows, highs, period=50):
    n = len(lows)
    support = np.full(n, np.nan)
    resistance = np.full(n, np.nan)
    for i in range(period, n):
        support[i] = np.min(lows[i - period:i])
        resistance[i] = np.max(highs[i - period:i])
    return support, resistance


def _session_volume_profile(bars, i, profile_bars, n_bins):
    """POC, VAH, VAL for a rolling window ending at bar i (inclusive of bar i)."""
    lo = max(0, i - profile_bars + 1)
    if i - lo + 1 < 20:  # need a minimum of bars to form a profile
        return None
    window = bars[lo:i + 1]
    price_min = min(b["low"] for b in window)
    price_max = max(b["high"] for b in window)
    if price_max <= price_min:
        return None
    bin_w = (price_max - price_min) / n_bins
    vol = np.zeros(n_bins)
    for b in window:
        tp = (b["high"] + b["low"] + b["close"]) / 3.0
        idx = int((tp - price_min) / bin_w)
        if 0 <= idx < n_bins:
            vol[idx] += b["volume"]
    total = vol.sum()
    if total <= 0:
        return None
    poc_idx = int(np.argmax(vol))
    poc = price_min + (poc_idx + 0.5) * bin_w
    # 70% value area around POC
    target = VALUE_AREA_PCT * total
    cum = vol[poc_idx]
    lo_idx, hi_idx = poc_idx, poc_idx
    while cum < target and (lo_idx > 0 or hi_idx < n_bins - 1):
        if hi_idx + 1 < n_bins and (lo_idx == 0 or vol[hi_idx + 1] >= vol[lo_idx - 1]):
            hi_idx += 1
            cum += vol[hi_idx]
        elif lo_idx > 0:
            lo_idx -= 1
            cum += vol[lo_idx]
    vah = price_min + (hi_idx + 1) * bin_w
    val = price_min + lo_idx * bin_w
    return {"poc": poc, "vah": vah, "val": val, "bin_w": bin_w}


def score_symbol(symbol, tp_r, profile_bars):
    m5 = load_bars(symbol, "M5")
    if len(m5) < WARMUP_BARS + FORWARD_BARS + 1 + profile_bars:
        print(f"  {symbol}: insufficient history ({len(m5)} bars)")
        return []
    highs = np.array([b["high"] for b in m5])
    lows = np.array([b["low"] for b in m5])
    closes = np.array([b["close"] for b in m5])
    opens = np.array([b["open"] for b in m5])
    atr = _atr(highs, lows, closes, 14)
    support, resistance = _rolling_support_resistance(lows, highs, 50)

    end = len(m5) - FORWARD_BARS
    rows = []
    for i in range(WARMUP_BARS + profile_bars, end):
        if np.isnan(atr[i]) or atr[i] <= 0 or np.isnan(support[i]) or np.isnan(resistance[i]):
            continue
        prof = _session_volume_profile(m5, i, profile_bars, N_BINS)
        if prof is None:
            continue
        poc = prof["poc"]
        # Wick touched POC (within NEAR_POC_ATR * ATR)
        tol = NEAR_POC_ATR * atr[i]
        wick_touched_poc = (lows[i] <= poc + tol) and (highs[i] >= poc - tol)
        if not wick_touched_poc:
            continue
        body = closes[i] - opens[i]
        rng = max(highs[i] - lows[i], 1e-9)
        # Bullish rejection of POC from above: closed above POC, bullish body,
        # lower wick reached into POC, close in upper portion of range.
        bull = (closes[i] > poc) and (body > 0) and (lows[i] <= poc + tol) and ((closes[i] - lows[i]) / rng > 0.5)
        # Bearish rejection of POC from below: closed below POC, bearish body,
        # upper wick reached into POC, close in lower portion of range.
        bear = (closes[i] < poc) and (body < 0) and (highs[i] >= poc - tol) and ((highs[i] - closes[i]) / rng > 0.5)
        if not (bull or bear):
            continue
        side = "BUY" if bull else "SELL"
        entry = float(closes[i])
        # SL = support (BUY) / resistance (SELL) — same as the labeler's setup fires.
        sl = float(support[i]) if side == "BUY" else float(resistance[i])
        risk = (entry - sl) if side == "BUY" else (sl - entry)
        if risk <= 0:
            continue  # invalid risk -> skip (not counted), matches labeler honesty
        fwd_bars = [
            {"time": m5[j]["time"], "high": highs[j], "low": lows[j], "close": closes[j]}
            for j in range(i + 1, i + 1 + FORWARD_BARS)
        ]
        fire = {
            "side": side,
            "entry": entry,
            "feat": {"support": float(support[i]), "resistance": float(resistance[i])},
            "tp_r": tp_r,
            "symbol": symbol,
            "setup_type": SETUP_NAME,
            "utc_hour": -1,
            "confidence": 0.5,
        }
        res = label_outcome(fire, fwd_bars, max_bars=FORWARD_BARS, tp_r=tp_r)
        if res is None:
            continue
        rows.append({
            "symbol": symbol,
            "setup_type": SETUP_NAME,
            "side": side,
            "r_multiple": res["r_multiple"],
            "exit_reason": res["exit_reason"],
            "bars_held": res["bars_held"],
        })
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

    # Aggregate per (symbol, setup) cell — same shape as the labeler.
    cells = defaultdict(list)
    for r in all_rows:
        cells[(r["symbol"], r["setup_type"])].append(r["r_multiple"])

    print("\n=== OOS SCORE: Session Volume Profile POC-rejection ===")
    print(f"{'symbol':<10} {'n':>5} {'win%':>6} {'expR':>8} {'ci95_lo':>8} {'ci95_hi':>8} {'clears_gate':>12}")
    cleared = []
    for (sym, setup), rs in sorted(cells.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        if n < 2:
            ci = None
        else:
            ci = _bootstrap_ci95(rs, reps=1000)
        wr = 100 * sum(1 for r in rs if r > 0) / n
        exp = sum(rs) / n
        lo = ci[0] if ci else float("nan")
        hi = ci[1] if ci else float("nan")
        gate = n >= 8 and exp > 0 and ci is not None and ci[0] > 0
        print(f"{sym:<10} {n:>5} {wr:>6.1f} {exp:>+8.4f} {lo:>+8.4f} {hi:>+8.4f} {'YES' if gate else 'no':>12}")
        if gate:
            cleared.append((sym, n, exp, lo, hi))

    total_n = len(all_rows)
    total_exp = sum(r["r_multiple"] for r in all_rows) / total_n if total_n else 0.0
    print(f"\nTOTAL fires={total_n} pooled_exp={total_exp:+.4f}R  cells_clearing_gate={len(cleared)}/{len(cells)}")
    print("CAVEAT: per-cell CI95 lo>0 is selection-biased across %d cells; NOT a deploy signal." % len(cells))
    if cleared:
        print("CANDIDATES (would wire into specialized_setups as SHADOW, not live):")
        for sym, n, exp, lo, hi in cleared:
            print(f"  {sym}: n={n} exp={exp:+.4f} ci95=[{lo:+.4f},{hi:+.4f}]")
    else:
        print("No cell clears the gate — honest no-edge result; NOT wiring into the registry.")


if __name__ == "__main__":
    main()