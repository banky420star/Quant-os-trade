"""Counterfactual BE/trail PnL curve for MT5 Quant OS.

User task (2026-07-20): run a calibration against the LAST 236 closed trades and
propose new per-symbol BE trigger/lock/trail numbers that would have flipped the
overall PnL to positive — show the counterfactual PnL curve.

WHY THIS SCRIPT IS DIFFERENT FROM scripts/calibrate_be_trail.py
- `calibrate_be_trail.py` keys on ``mae_R`` / ``mfe_R``. The bot never wrote
  those to ``state/trade_log.json`` (``mfe_R=0``, ``mae_R=0``, ``risk_amount=0``,
  ``tp1=0`` out of 236). Its ``_prep`` rejects the entire history and returns an
  empty symbol table.
- We can't fabricate path data we don't have. Instead we sweep the broker-side
  dollar knobs actually configured in config.yaml (lock_profit_usd, trigger
  points, trail_points) and model the only thing the realized data lets us model
  cleanly: dust winners (``pnl > 0`` but ``pnl < be_lock_usd``) get FLOORED to
  ``be_lock_usd``. Losers are realised losses (assumed initial-sl hit), kept as is.
- The 4-axis grid ``(be_trig, be_lock, tr_act, tr_dist)`` reduces to a
  1-axis sweep on ``be_lock_usd`` because:
    * ``be_trig`` is satisfied whenever ``pnl > 0`` (the trade passed the trigger
      to exit positive).
    * ``tr_act`` / ``tr_dist`` cannot be modelled without mfe_R; we therefore
      state the assumption explicitly and only recommend BE knobs.
- OUTPUT: ASCII cumulative PnL curve (baseline vs best be_lock_usd) + per-symbol
  flip-to-positive proposal + JSON dump consumable by ``_merge_live`` in
  ``core/position_manager.py``.

HONEST LIMITATIONS
- Dust winners with pnl > be_lock_usd get NO boost under the new model (path
  unknown; trade peaked above be_lock_usd but exited at higher realised value).
- Trail activation cannot be confidently modelled; recommendations stop at BE.
- Reversal risk NOT modelled. A winner boosted to ``be_lock_usd`` could in
  reality have continued to ``-1R`` under a different path. We surface this in
  the printed report.

Usage:
    python scripts/calibrate_be_trail_curve.py                # last 236, console
    python scripts/calibrate_be_trail_curve.py --window 100   # custom window
    python scripts/calibrate_be_trail_curve.py --write-live   # write JSON
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import setup_logger  # noqa: E402

# USD-dollar BE_LOCK grid — the only dimension we can model cleanly from realised pnl.
LOCK_GRID_USD: tuple[float, ...] = (
    0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.75, 1.00, 1.25, 1.50,
)
DEFAULT_WINDOW = 236
MIN_N_PER_SYMBOL = 8


def _load_last_n(window: int, log: logging.Logger) -> list[dict[str, Any]]:
    data = json.loads((ROOT / "state" / "trade_log.json").read_text(encoding="utf-8"))
    trades = data.get("trades", []) if isinstance(data, dict) else data
    if not isinstance(trades, list) or not trades:
        raise SystemExit("trade_log.json has no trades")
    last = trades[-window:] if window > 0 else trades
    log.info("Loaded %d trades (window=%d of %d total)", len(last), window, len(trades))
    return last


def _baseline(trades: list[dict[str, Any]]) -> dict[str, Any]:
    pnl = [float(t.get("pnl", 0) or 0) for t in trades]
    sym = [t.get("symbol", "?") for t in trades]
    n_wins = sum(1 for p in pnl if p > 0)
    n_losses = sum(1 for p in pnl if p <= 0)
    avg_win = sum(p for p in pnl if p > 0) / max(n_wins, 1)
    avg_loss = sum(p for p in pnl if p <= 0) / max(n_losses, 1)
    out = {
        "n": len(trades),
        "n_wins": n_wins,
        "n_losses": n_losses,
        "wr_pct": 100.0 * n_wins / max(len(trades), 1),
        "avg_win_usd": avg_win,
        "avg_loss_usd": avg_loss,
        "payoff_ratio": abs(avg_win / avg_loss) if avg_loss else float("inf"),
        "total_pnl_usd": sum(pnl),
        "by_symbol": {},
    }
    by_sym_pnl: dict[str, list[float]] = defaultdict(list)
    for s, p in zip(sym, pnl):
        by_sym_pnl[s].append(p)
    for s, lst in sorted(by_sym_pnl.items()):
        w = [p for p in lst if p > 0]
        l = [p for p in lst if p <= 0]
        avg_w = sum(w) / max(len(w), 1) if w else 0.0
        avg_l = sum(l) / max(len(l), 1) if l else 0.0
        # Breakeven WR = 1 / (1 + |avg_loss|/avg_win) — needed WR for $0 PnL.
        payoff = abs(avg_w / avg_l) if avg_l else float("inf")
        breakeven_wr_pct = (100.0 / (1.0 + payoff)) if avg_l and avg_w else 100.0
        out["by_symbol"][s] = {
            "n": len(lst),
            "n_wins": len(w),
            "n_losses": len(l),
            "wr_pct": 100 * len(w) / max(len(lst), 1),
            "avg_win_usd": round(avg_w, 4),
            "avg_loss_usd": round(avg_l, 4),
            "payoff_ratio": round(payoff, 4),
            "breakeven_wr_pct": round(breakeven_wr_pct, 2),
            "total_pnl_usd": round(sum(lst), 4),
        }
    return out


def _project(  # noqa: PLR0913
    trades: list[dict[str, Any]],
    lock_usd: float,
    baseline: dict[str, Any],
) -> tuple[dict[str, Any], list[float]]:
    """Floor dust winners (0 < pnl < lock_usd) to be_lock_usd. Losers unchanged.
    Caller pre-computes ``baseline`` via :func:`_baseline` to avoid re-walking.
    """
    new_pnl: list[float] = []
    n_boosted = 0
    boost_sum_usd = 0.0
    for t in trades:
        p = float(t.get("pnl", 0) or 0)
        if 0.0 < p < lock_usd:
            n_boosted += 1
            boost_sum_usd += lock_usd - p
            new_pnl.append(lock_usd)
        else:
            new_pnl.append(p)
    out = dict(baseline)
    out["be_lock_usd"] = lock_usd
    out["n_dust_boosted"] = n_boosted
    out["boost_sum_usd"] = round(boost_sum_usd, 2)
    out["projected_total_pnl_usd"] = round(sum(new_pnl), 2)
    out["delta_vs_baseline_usd"] = round(sum(new_pnl) - baseline["total_pnl_usd"], 2)
    out["actually_flipped"] = bool(baseline["total_pnl_usd"] <= 0 and sum(new_pnl) > 0)
    return out, new_pnl


def _ascii_curve(
    baseline_pnl_series: list[float],
    projected_pnl_series: list[float],
    width: int = 60,
    height: int = 18,
) -> str:
    """Two-line ASCII cumulative PnL curve. y-axis shared; x-axis = trade index."""
    cum_base: list[float] = []
    cum_proj: list[float] = []
    s = 0.0
    for p in baseline_pnl_series:
        s += p
        cum_base.append(s)
    s = 0.0
    for p in projected_pnl_series:
        s += p
        cum_proj.append(s)
    if not cum_base:
        return "<no data>"

    all_vals = cum_base + cum_proj + [0.0]
    y_min = min(all_vals)
    y_max = max(all_vals)
    if y_max == y_min:
        y_max += 1.0
        y_min -= 1.0

    n = len(cum_base)
    if n < 2:
        return "<too few points>"

    grid: list[list[str]] = [[" "] * width for _ in range(height)]
    # Zero line
    zero_y = int(round((y_max - 0.0) / (y_max - y_min) * (height - 1)))
    zero_y = max(0, min(height - 1, zero_y))
    for x in range(width):
        grid[zero_y][x] = "-"
    # Plot baseline (B) and projected (P)
    for series, ch in ((cum_base, "B"), (cum_proj, "P")):
        for i in range(n):
            x = int(round(i * (width - 1) / (n - 1)))
            y = int(round((y_max - series[i]) / (y_max - y_min) * (height - 1)))
            y = max(0, min(height - 1, y))
            # newer plot wins on tie
            grid[y][x] = ch
    legend = (
        f"  B = baseline cum PnL   P = projected (best lock)   "
        f"x-axis = trade #   y-axis: ${y_min:+.0f} .. ${y_max:+.0f}"
    )
    rows = ["|" + "".join(row) + "|" for row in grid]
    frame_top = "+" + "-" * width + "+"
    frame_bot = "+" + "=" * width + "+"
    return "\n".join([frame_top, *rows, frame_bot, legend])


def _per_symbol_proposal(
    trades: list[dict[str, Any]],
    log: logging.Logger,
    min_n: int = MIN_N_PER_SYMBOL,
    baseline_total_by_sym: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """For each symbol with >= ``min_n`` trades, find the lock_usd that maximises
    the projected PnL AND flips it positive if possible."""
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by_sym[t.get("symbol", "?")].append(t)
    out: list[dict[str, Any]] = []
    for sym, lst in sorted(by_sym.items()):
        if len(lst) < min_n:
            continue
        sym_baseline = (baseline_total_by_sym or {}).get(sym, _baseline(lst))
        base_total = sym_baseline["total_pnl_usd"]
        best = {
            "lock_usd": 0.0,
            "projected_total_usd": base_total,
            "delta_usd": 0.0,
            "n_dust_boosted": 0,
        }
        flip_lock_usd: float | None = None
        for lock_usd in LOCK_GRID_USD:
            proj, _ = _project(lst, lock_usd, sym_baseline)
            if proj["projected_total_pnl_usd"] > best["projected_total_usd"]:
                best = {
                    "lock_usd": lock_usd,
                    "projected_total_usd": proj["projected_total_pnl_usd"],
                    "delta_usd": proj["delta_vs_baseline_usd"],
                    "n_dust_boosted": proj["n_dust_boosted"],
                }
            if base_total <= 0 and proj["projected_total_pnl_usd"] > 0 and flip_lock_usd is None:
                flip_lock_usd = lock_usd
        losers = [t["pnl"] for t in lst if t["pnl"] <= 0]
        avg_loss_usd = sum(losers) / max(len(losers), 1) if losers else 0.0
        actually_flipped = bool(base_total <= 0 and best["projected_total_usd"] > 0)
        proposal = {
            "symbol": sym,
            "n": len(lst),
            "baseline_total_usd": round(base_total, 2),
            "best_lock_usd": round(best["lock_usd"], 2),
            "projected_total_usd": round(best["projected_total_usd"], 2),
            "delta_usd": round(best["delta_usd"], 2),
            "n_dust_boosted": best["n_dust_boosted"],
            "avg_loss_usd": round(avg_loss_usd, 2),
            "floor_above_loss": 0.0 < best["lock_usd"] < abs(avg_loss_usd),
            "flipped_positive": bool(best["projected_total_usd"] > 0),
            "actually_flipped": actually_flipped,
            "smallest_flip_lock_usd": flip_lock_usd,
        }
        out.append(proposal)
        log.info(
            "%s: baseline=$%.2f  best@lock=%.2f: $%.2f (Delta=%+.2f, %d dust)  flip@$%.2f  actually_flipped=%s",
            sym, base_total, best["lock_usd"], best["projected_total_usd"],
            best["delta_usd"], best["n_dust_boosted"],
            flip_lock_usd if flip_lock_usd is not None else -1,
            actually_flipped,
        )
    return out


def _global_flip(
    trades: list[dict[str, Any]],
    log: logging.Logger,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Find the single lock_usd on the entire window that maximises total PnL
    or first flips it positive. ``baseline`` is pre-computed by caller."""
    best_lock = 0.0
    best_total = baseline["total_pnl_usd"]
    best_delta = 0.0
    n_dust = 0
    for lock_usd in LOCK_GRID_USD:
        proj, _ = _project(trades, lock_usd, baseline)
        if proj["projected_total_pnl_usd"] > best_total:
            best_total = proj["projected_total_pnl_usd"]
            best_lock = lock_usd
            best_delta = proj["delta_vs_baseline_usd"]
            n_dust = proj["n_dust_boosted"]
    flip_lock_usd: float | None = None
    if baseline["total_pnl_usd"] <= 0:
        for lock_usd in LOCK_GRID_USD:
            proj, _ = _project(trades, lock_usd, baseline)
            if proj["projected_total_pnl_usd"] > 0:
                flip_lock_usd = lock_usd
                break
    log.info(
        "GLOBAL last-%d: baseline $%.2f  best@lock=$%.2f -> $%.2f (Delta=%+.2f, %d dust)  flip@$%.2f",
        len(trades), baseline["total_pnl_usd"], best_lock, best_total, best_delta, n_dust,
        flip_lock_usd if flip_lock_usd is not None else -1,
    )
    return {
        "n": baseline["n"],
        "baseline_total_usd": round(baseline["total_pnl_usd"], 2),
        "n_wins": baseline["n_wins"], "n_losses": baseline["n_losses"],
        "wr_pct": round(baseline["wr_pct"], 1),
        "avg_win_usd": round(baseline["avg_win_usd"], 2),
        "avg_loss_usd": round(baseline["avg_loss_usd"], 2),
        "best_lock_usd": round(best_lock, 2),
        "projected_total_usd": round(best_total, 2),
        "delta_usd": round(best_delta, 2),
        "n_dust_boosted": n_dust,
        "smallest_flip_lock_usd": flip_lock_usd,
        "flipped_positive": bool(best_total > 0),
        "actually_flipped": bool(baseline["total_pnl_usd"] <= 0 and best_total > 0),
    }


def main() -> int:  # noqa: PLR0915
    ap = argparse.ArgumentParser(description="Counterfactual PnL curve + per-symbol proposal")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--write-live", action="store_true",
                    help="write JSON proposals to state/symbol_be_trail_live.json")
    args = ap.parse_args()
    log = setup_logger("calibrate_be_trail_curve", "calibrate_be_trail_curve.log")

    trades = _load_last_n(args.window, log)
    base = _baseline(trades)

    print("=== BASELINE (last %d) ===" % len(trades))
    print(f"  n={base['n']}  wins={base['n_wins']}  losses={base['n_losses']}"
          f"  WR={base['wr_pct']:.1f}%"
          f"  avg_win=${base['avg_win_usd']:+.2f}"
          f"  avg_loss=${base['avg_loss_usd']:+.2f}"
          f"  payoff={base['payoff_ratio']:.2f}")
    print(f"  total PnL = ${base['total_pnl_usd']:+.2f}")
    print()
    print("Per-symbol baseline:")
    print(f"  {'symbol':<10s} {'n':>4s} {'WR':>6s} {'avg_win':>9s} {'avg_loss':>9s} {'total':>10s}")
    for s, d in sorted(base["by_symbol"].items(), key=lambda kv: -kv[1]["n"]):
        print(f"  {s:<10s} {d['n']:>4d} {d['wr_pct']:>5.0f}%"
              f" ${d['avg_win_usd']:>+7.2f} ${d['avg_loss_usd']:>+7.2f}"
              f" ${d['total_pnl_usd']:>+8.2f}")
    print()

    # Full sweep report
    print("=== SWEEP be_lock_usd (global) ===")
    print(f"  {'lock_$':>8s} {'total_$':>10s} {'Delta':>8s} {'# boosted':>9s} {'FLIP':>5s}")
    sweep_rows = []
    for lock_usd in LOCK_GRID_USD:
        proj, _ = _project(trades, lock_usd, base)
        sweep_rows.append((lock_usd, proj["projected_total_pnl_usd"], proj["delta_vs_baseline_usd"], proj["n_dust_boosted"]))
        flip_mark = "*" if proj["actually_flipped"] else " "
        print(f"  ${lock_usd:>6.2f}  ${proj['projected_total_pnl_usd']:>+8.2f}"
              f"  ${proj['delta_vs_baseline_usd']:>+6.2f}  {proj['n_dust_boosted']:>7d}  {flip_mark:>4s}")
    print()

    g = _global_flip(trades, log, base)
    print("=== GLOBAL BEST ===")
    base_sign = "-" if g["baseline_total_usd"] < 0 else "+"
    print(f"  baseline total:              ${base_sign}${abs(g['baseline_total_usd']):.2f}")
    print(f"  best lock_usd (maximises total): ${g['best_lock_usd']:+.2f}"
          f" -> ${g['projected_total_usd']:+.2f}"
          f"  (Delta=${g['delta_usd']:+.2f}, {g['n_dust_boosted']} dust boosted)")
    if g["smallest_flip_lock_usd"] is not None:
        print(f"  smallest lock_usd that ACTUALLY FLIPS positive: ${g['smallest_flip_lock_usd']:.2f}")
    else:
        print(f"  smallest lock_usd that ACTUALLY FLIPS positive: (no flip available in grid)")
    print(f"  flipped_positive:            {g['flipped_positive']}  actually_flipped: {g['actually_flipped']}")
    print()

    # Per-symbol best
    print("=== PER-SYMBOL BEST (n >= {}) ===".format(MIN_N_PER_SYMBOL))
    print(f"  {'symbol':<10s} {'n':>3s} {'baseline':>10s} {'lock_$':>7s}"
          f" {'projected':>10s} {'Delta':>8s} {'#':>3s} {'FLIP':>5s} {'!FLIP':>6s}")
    ps = _per_symbol_proposal(trades, log, baseline_total_by_sym=base["by_symbol"])
    for p in ps:
        flip_mark = "YES" if p["flipped_positive"] else "-"
        actual_mark = "YES" if p["actually_flipped"] else "-"
        warn = " W" if p["floor_above_loss"] else "  "
        print(f"  {p['symbol']:<10s} {p['n']:>3d} ${p['baseline_total_usd']:>+8.2f}"
              f"  ${p['best_lock_usd']:>5.2f} ${p['projected_total_usd']:>+8.2f}"
              f"  ${p['delta_usd']:>+6.2f} {p['n_dust_boosted']:>3d}  {flip_mark:>4s}"
              f" {actual_mark:>5s}{warn}")
    print()

    # Cumulative PnL curve
    base_pnl_series = [float(t.get("pnl", 0) or 0) for t in trades]
    best_lock_usd = g["best_lock_usd"]
    _, proj_pnl_series = _project(trades, best_lock_usd, base)
    print(f"=== CUMULATIVE PnL CURVE (baseline 'B' vs projected at lock=${best_lock_usd:.2f} 'P') ===")
    print(_ascii_curve(base_pnl_series, proj_pnl_series))
    print()

    # Honest summary stats
    print("=== HONEST CAVEATS ===")
    print("  - Trail activation not modelled (mfe_R missing); recommendation limited to BE.")
    print("  - Trade-level path unknown; dust winners floor is an UPPER BOUND.")
    print("  - Reversal risk NOT modelled: a winner boosted to be_lock_usd could in")
    print("    reality have continued to -1R under a different path.")
    print(f"  - Avg loss = ${base['avg_loss_usd']:+.2f}; for a clean +E setup, lock_usd")
    print("    should generally stay below |avg_loss_usd| to avoid returning losers.")
    print(f"  - Per-symbol: any 'floor_above_loss'=True (column '!FLIP' has trailing ' W')")
    print("    means the lock is EQUAL to or LARGER than the symbol's average loss and")
    print("    would amplify damage on losers if path collapses. Treat with caution.")
    print(f"  - Symbols with '!FLIP=YES' above flip baseline NEGATIVE -> POSITIVE.")
    print(f"  - Symbols without '!FLIP=YES' cannot be flipped within this grid.")
    risk_amp = [p["symbol"] for p in ps if p["floor_above_loss"]]
    no_flip = [p["symbol"] for p in ps if not p["actually_flipped"]]
    if risk_amp:
        print(f"  - WARN risk-amplify: {', '.join(risk_amp)}")
    if no_flip:
        print(f"  - No flip available: {', '.join(no_flip)}")
    print()

    if args.write_live:
        # Sibling file `counterfactual_curve.json` keeps the live schema clean.
        out_payload: dict[str, Any] = {
            "source": "calibrate_be_trail_curve.py (counterfactual mode)",
            "note": (
                "Per-symbol lock_profit_usd proposals derived from the realised-pnl "
                "dust-winner floor model. trusted=False on every entry so core/"
                "position_manager.py:_merge_live will not auto-apply them. Path data "
                "(mfe_R/mae_R) is missing; use only as offline analysis."
            ),
            "window": args.window,
            "global": {
                "baseline_total_usd": g["baseline_total_usd"],
                "best_lock_usd": g["best_lock_usd"],
                "projected_total_usd": g["projected_total_usd"],
                "delta_usd": g["delta_usd"],
                "smallest_flip_lock_usd": g["smallest_flip_lock_usd"],
                "actually_flipped": g["actually_flipped"],
            },
            "symbols": {
                p["symbol"]: {
                    "n": p["n"],
                    "break_even": {
                        "lock_profit_usd": round(p["best_lock_usd"], 2),
                        "trigger_profit_usd": max(round(p["best_lock_usd"] * 0.6, 2), 0.05),
                    },
                    "trailing": {},
                    "expectancy_usd": round(p["projected_total_usd"], 2),
                    "seed_expectancy_usd": round(p["baseline_total_usd"], 2),
                    "delta_usd": round(p["delta_usd"], 2),
                    "flipped_positive": p["flipped_positive"],
                    "actually_flipped": p["actually_flipped"],
                    "floor_above_loss": p["floor_above_loss"],
                    "ci95": [0.0, 0.0],
                    "trusted": False,
                    "reason": "counterfactual: dust-winner floor; trail/be_trig not modelled",
                }
                for p in ps
            },
        }
        out_path = ROOT / "state" / "counterfactual_curve.json"
        out_path.write_text(json.dumps(out_payload, indent=2, default=str), encoding="utf-8")
        log.info("Wrote %s", out_path)
        print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
