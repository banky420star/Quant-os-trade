"""Per-symbol BE trigger tuner from realised risk/cost trade-off math.

Reads ``state/trade_log.json``, computes per-symbol dollar risk averages and
full-SL hit rates, then determines the optimal BE trigger R that minimises
expected loss given the symbol's risk profile. Writes
``state/symbol_be_trail_live.json`` in the same format as ``calibrate_be_trail.py``
so ``position_manager._merge_live()`` honours the values when ``trusted``.

Method (trade-off model — entry/exit prices only, no MAE/MFE needed):

  For each symbol, given average dollar risk R_avg and the set of closed trades:

    * Full-SL losers (r <= -0.8): price ran to SL, never retraced enough for BE.
      A tighter BE trigger would save N% of these by locking at +be_trigger_R before
      price reverses to SL.

    * Winners: a tighter BE trigger caps profit at +be_trigger_R instead of the
      realised avg_win. The "cap cost" is (avg_win - be_trigger_R * R_avg) per winner.

    * Net impact per 100 trades:
        Net = (save_pct * full_sl_pct * 100 * 1.0R * R_avg)
            - (cap_pct * win_pct * 100 * max(0, avg_win - be_trigger_R * R_avg))

    The optimal BE trigger minimises expected loss, considering that tighter triggers
    save more full-SL losers but also cap more winners. The grid search evaluates
    BE trigger R in {0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0}.

Run:
    python scripts/tune_persymbol_be.py            # write state/symbol_be_trail_live.json
    python scripts/tune_persymbol_be.py --dry-run   # preview only
    python scripts/tune_persymbol_be.py --apply     # also set trusted=True (skips safety gate)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import read_json_state, setup_logger, utc_now_iso, write_json_state  # noqa: E402

# BE trigger grid in R-units (multiples of risk distance |entry - sl|).
BE_TRIGGER_GRID = (0.2, 0.3, 0.4, 0.5, 0.6, 0.65, 0.8, 1.0)
# Trailing params use the calibrated defaults (we only tune BE here).
TRAIL_ACT_GRID = (0.6, 0.8)
TRAIL_DIST_GRID = (0.25, 0.35)

# Safety gates.
MIN_N_TRUSTED = 30       # minimum trades to mark trusted
MIN_RISK_DOLLAR = 0.05   # minimum avg dollar risk to override (otherwise risk is noise)
SAVE_PCT_ESTIMATE = 0.30  # conservative estimate: 30% of full-SL losers would be saved by tighter BE

# Defaults (used when there's insufficient data for a symbol).
DEFAULT_BE_TRIGGER = 0.65  # from config.yaml default


def _per_symbol_metrics(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Compute risk/cost metrics per symbol from closed trades."""
    sym_data: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "n": 0, "total_pnl": 0.0,
            "wins": 0, "losses": 0,
            "total_risk": 0.0, "total_win_pnl": 0.0,
            "full_sl_count": 0, "full_sl_total_pnl": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0,
            "exit_prices": [],
        }
    )

    for t in trades:
        sym = str(t.get("symbol", "?")).strip()
        d = sym_data[sym]
        d["n"] += 1

        try:
            pnl = float(t.get("pnl", 0))
        except (TypeError, ValueError):
            pnl = 0.0
        d["total_pnl"] += pnl

        # Risk from PnL / R-multiple (when available).
        r = t.get("r_multiple")
        try:
            r_val = float(r) if r is not None else None
        except (TypeError, ValueError):
            r_val = None

        if r_val is not None and abs(r_val) > 0.001:
            risk_approx = abs(pnl) / abs(r_val)
            d["total_risk"] += risk_approx

        if pnl > 0:
            d["wins"] += 1
            d["avg_win"] = (d["avg_win"] * (d["wins"] - 1) + pnl) / d["wins"]
            d["total_win_pnl"] += pnl
        else:
            d["losses"] += 1
            d["avg_loss"] = (d["avg_loss"] * (d["losses"] - 1) + pnl) / d["losses"]

        # Full SL hit: R <= -0.8 and exit matches SL price.
        if r_val is not None and r_val <= -0.8:
            d["full_sl_count"] += 1
            d["full_sl_total_pnl"] += pnl

        # Track exit price for density analysis.
        try:
            d["exit_prices"].append(float(t.get("exit", 0)))
        except (TypeError, ValueError):
            pass

    # Compute derived metrics.
    out: dict[str, dict[str, Any]] = {}
    for sym, d in sym_data.items():
        if d["n"] == 0:
            continue
        risk_avg = d["total_risk"] / max(d["n"], 1)
        full_sl_pct = d["full_sl_count"] / max(d["n"], 1) * 100
        wr = d["wins"] / max(d["n"], 1) * 100
        avg_pnl = d["total_pnl"] / max(d["n"], 1)
        payoff = abs(d["avg_win"] / max(abs(d["avg_loss"]), 0.001)) if d["wins"] > 0 and d["losses"] > 0 else 0.0

        out[sym] = {
            "n": d["n"],
            "wr_pct": round(wr, 1),
            "total_pnl": round(d["total_pnl"], 2),
            "avg_pnl": round(avg_pnl, 4),
            "avg_risk_dollar": round(risk_avg, 4),
            "avg_win": round(d["avg_win"], 4),
            "avg_loss": round(d["avg_loss"], 4),
            "payoff": round(payoff, 2),
            "full_sl_n": d["full_sl_count"],
            "full_sl_pct": round(full_sl_pct, 1),
            "full_sl_avg_cost": round(d["full_sl_total_pnl"] / max(d["full_sl_count"], 1), 4) if d["full_sl_count"] > 0 else 0.0,
        }
    return out


def _simulate_be_net_impact(
    metrics: dict[str, Any],
    be_trigger_R: float,
    *,
    save_pct: float = SAVE_PCT_ESTIMATE,
) -> dict[str, Any]:
    """Simulate the net PnL impact (per 100 trades) of a BE trigger at `be_trigger_R`.

    The trade-off model:
      * ``save_pct`` of full-SL losers are saved from -1R → +be_trigger_R
      * ``save_pct`` of winners are capped from avg_win → be_trigger_R * R_avg
      * Net = saved_losers - capped_winners
    """
    n = metrics["n"]
    if n < 3:
        return {"net_per_100": 0.0, "losers_saved_per_100": 0.0, "saved_gain_per_100": 0.0, "winners_capped_per_100": 0.0, "cap_loss_per_100": 0.0}

    R = metrics["avg_risk_dollar"]
    if R <= 0:
        return {"net_per_100": 0.0, "losers_saved_per_100": 0.0, "winners_capped_per_100": 0.0}

    full_sl_pct = metrics["full_sl_pct"] / 100.0
    wr_pct = metrics["wr_pct"] / 100.0
    avg_win = metrics["avg_win"]
    avg_loss = abs(metrics["avg_loss"])

    # Per 100 trades:
    #   full_sl_losers = full_sl_pct * 100
    #   winners = wr_pct * 100
    n_full_sl = full_sl_pct * 100
    n_wins = wr_pct * 100

    # Saved losers: save_pct of full-SL losers become +be_trigger_R winners
    saved = save_pct * n_full_sl
    saved_gain = saved * (be_trigger_R * R - avg_loss)

    # Capped winners: save_pct of winners get capped at +be_trigger_R
    capped = save_pct * n_wins  # same save_pct assumption for symmetry
    uncapped_win = max(avg_win, be_trigger_R * R)
    cap_loss = capped * (avg_win - be_trigger_R * R)

    net = saved_gain - cap_loss

    return {
        "net_per_100": round(net, 4),
        "losers_saved_per_100": round(saved, 2),
        "saved_gain_per_100": round(saved_gain, 4),
        "winners_capped_per_100": round(capped, 2),
        "cap_loss_per_100": round(cap_loss, 4),
    }


def _optimal_be(metrics: dict[str, Any], log: Any) -> dict[str, Any]:
    """Grid-search over BE triggers to find the one with highest net impact."""
    best_net = -999999.0
    best_trigger = DEFAULT_BE_TRIGGER
    best_sim: dict[str, Any] = {}

    R = metrics["avg_risk_dollar"]
    full_sl_pct = metrics["full_sl_pct"]
    n = metrics["n"]
    wr = metrics["wr_pct"]

    # Log-level diagnostic.
    log.info(
        "  %s: n=%d R=$%.4f fullSL=%.1f%% WR=%.1f%% win=$%.4f loss=$%.4f",
        metrics.get("_symbol", "?"), n, R, full_sl_pct, wr,
        metrics["avg_win"], abs(metrics["avg_loss"]),
    )

    for be_r in BE_TRIGGER_GRID:
        sim = _simulate_be_net_impact(metrics, be_r)
        net = sim["net_per_100"]
        log.info(
            "    BE@%.1fR: net=%.4f (save %.1f losers gain=%.4f | cap %.1f winners loss=%.4f)",
            be_r, net,
            sim["losers_saved_per_100"], sim["saved_gain_per_100"],
            sim["winners_capped_per_100"], sim["cap_loss_per_100"],
        )
        if net > best_net:
            best_net = net
            best_trigger = be_r
            best_sim = sim

    # Decision logic:
    #   Tighten BE when avg risk is high AND full-SL hits are common.
    #   Keep seed when risk is noise-level or full-SL is rare.
    #   The "best" trigger from grid is applied only if it materially improves
    #   on the default (0.65R).
    delta_vs_default = best_net
    default_sim = _simulate_be_net_impact(metrics, DEFAULT_BE_TRIGGER)
    improvement = delta_vs_default - default_sim.get("net_per_100", 0)

    trusted = (
        n >= MIN_N_TRUSTED
        and R >= MIN_RISK_DOLLAR
        and improvement > 0.01  # at least $0.01/100t improvement
    )

    reason_parts = []
    if R < MIN_RISK_DOLLAR:
        reason_parts.append(f"risk=${R:.4f} < ${MIN_RISK_DOLLAR} min")
    if n < MIN_N_TRUSTED:
        reason_parts.append(f"n={n} < {MIN_N_TRUSTED}")
    if improvement <= 0.01:
        reason_parts.append(f"improvement=${improvement:.4f}/100t negligible")

    return {
        "recommended_trigger_R": best_trigger,
        "default_trigger_R": DEFAULT_BE_TRIGGER,
        "net_per_100_at_best": round(best_net, 4),
        "net_per_100_at_default": round(default_sim.get("net_per_100", 0), 4),
        "improvement_per_100": round(best_net - default_sim.get("net_per_100", 0), 4),
        "detail": best_sim,
        "trusted": trusted,
        "reason": "data-driven, optimal" if trusted else "; ".join(reason_parts) if reason_parts else "insufficient data",
    }


def tune(trades: list[dict[str, Any]], log: Any, *, force_trusted: bool = False) -> dict[str, Any]:
    """Run the full per-symbol BE tuning pipeline."""
    metrics_by_sym = _per_symbol_metrics(trades)
    out: dict[str, Any] = {"symbols": {}, "updated_at": None}

    for sym in sorted(metrics_by_sym.keys()):
        m = metrics_by_sym[sym]
        m["_symbol"] = sym
        opt = _optimal_be(m, log)

        # Convert the optimal BE trigger R to ATR-mult for the override file.
        # BE trigger in config.yaml is in ATR-mult units (e.g., 0.65 means 0.65 * ATR).
        # Our R-unit trigger is scaled by risk_distance / ATR-ratio.
        # For the override, we write trigger_atr_mult = trigger_R.
        # This maps directly because the compute_managed_sl uses ATR-mult as the
        # distance threshold, and the payoff paradox patch uses min_r_multiple_win
        # in R-units. The _merge_live function merges trigger_atr_mult.
        be_trigger_atr = opt["recommended_trigger_R"]
        be_lock_atr = min(0.15, be_trigger_atr * 0.15)  # lock ~15% of trigger

        is_trusted = bool(force_trusted or opt["trusted"])

        entry = {
            "n": m["n"],
            "source": "tune_persymbol_be",
            "break_even": {
                "trigger_atr_mult": round(be_trigger_atr, 4),
                "lock_profit_atr_mult": round(be_lock_atr, 4),
            },
            # Trailing values are kept at calibrated defaults — this script only tunes BE.
            "trailing": {
                "activation_atr_mult": 0.75,
                "trail_atr_mult": 0.35,
            },
            "metrics": {
                "wr_pct": m["wr_pct"],
                "avg_risk_dollar": m["avg_risk_dollar"],
                "full_sl_pct": m["full_sl_pct"],
                "full_sl_n": m["full_sl_n"],
                "total_pnl": m["total_pnl"],
            },
            "optimisation": {
                "default_trigger_R": DEFAULT_BE_TRIGGER,
                "recommended_trigger_R": opt["recommended_trigger_R"],
                "net_per_100_at_default": opt["net_per_100_at_default"],
                "net_per_100_at_best": opt["net_per_100_at_best"],
                "improvement_per_100": opt["improvement_per_100"],
            },
            "trusted": is_trusted,
            "reason": opt["reason"],
        }
        out["symbols"][sym] = entry

        status = "TRUSTED" if is_trusted else "observe (not trusted)"
        log.info(
            "%s: n=%d risk=$%.4f fullSL=%.1f%% -> BE@%.1fR (default=%.1fR, net=%.4f/100t) -> %s",
            sym, m["n"], m["avg_risk_dollar"], m["full_sl_pct"],
            opt["recommended_trigger_R"], DEFAULT_BE_TRIGGER,
            opt["improvement_per_100"], status,
        )

    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Per-symbol BE trigger tuner from risk/cost trade-off math"
    )
    ap.add_argument("--dry-run", action="store_true", help="Preview only, no write")
    ap.add_argument("--apply", action="store_true", help="Force trusted=True (skip safety gate)")
    args = ap.parse_args()

    log = setup_logger("tune_persymbol_be", "tune_persymbol_be.log")

    trade_log = read_json_state("trade_log.json", default={}) or {}
    trades = trade_log.get("trades", []) if isinstance(trade_log, dict) else []

    if not trades:
        log.error("No trades found in state/trade_log.json")
        return 1

    log.info("Loaded %d trades from trade_log.json", len(trades))

    result = tune(trades, log, force_trusted=bool(args.apply))
    result["updated_at"] = utc_now_iso()

    trusted_count = sum(1 for v in result["symbols"].values() if v.get("trusted"))
    log.info(
        "Tuning complete: %d symbols, %d trusted -> would apply BE override",
        len(result["symbols"]), trusted_count,
    )

    if args.dry_run:
        print(json.dumps(result, indent=2, default=str))
        return 0

    # Write to the same file that _merge_live reads (symbol_be_trail_live.json).
    write_json_state("symbol_be_trail_live.json", result)
    log.info("Wrote state/symbol_be_trail_live.json")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
