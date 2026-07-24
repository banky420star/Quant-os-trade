"""Edge verifier — projects per-symbol daily PnL for BE/trail grid against
historic closed trades.

Why this exists
===============
The user asked for a "guaranteed $X per day" market edge. No honest system
can promise that. This script's job is to PROJECT the edge from the best
BE/trail grid cell PER SYMBOL, given the bot's own historic trade outcomes
(696 trades). The output is a numeric estimate with the same bootstrap-CI95
discipline used by calibrate_be_trail.py, so the operator sees an honest
projection rather than a marketing claim.

What it does NOT do
===================
- Does NOT write to config.yaml or learning_config_overrides.json.
- Does NOT auto-apply any tuning. Apply is a separate operator action
  (``scripts/calibrate_be_trail.py --apply`` after this script's draft).
- Does NOT trade live or paper. Pure offline counterfactual replay.

Method
======
For every trade with the four required fields
(entry, sl_initial, mae_R, mfe_R, r_multiple, tp1):

    1. Express outcome as a counterfactual R-multiple under a candidate
       ``(be_trig, be_lock, tr_act, tr_dist)`` (in R units). Same exit
       resolution as ``calibrate_be_trail.py`` so results are comparable.
    2. Compute the per-trade dollar PnL = counterfactual_r * avg_risk_dollar
       for that symbol.
    3. Sum per trade to get per-symbol expectancy + win-rate.
    4. Multiply by trades_per_day (median gap between closed_at per symbol)
       to project daily PnL.
    5. Bootstrap-CI95 the per-trade expectancy AND the per-day projection
       so a single-symbol "edge" with n<50 reports wide error bars and
       never lights up the "trusted" verdict.

Output
======
Writes ``state/edge_projection_<UTC>.json`` with per-symbol rows:

  {
    "symbol": "XAUUSDm",
    "n": 87,
    "trades_per_day": 6.2,
    "avg_risk_dollar": 0.32,
    "seed":   {"be_trig": 0.65, "tr_act": 0.75, "tr_dist": 0.35,
               "expectancy_r": 0.05, "win_rate_pct": 32.0,
               "projected_daily_pnl_usd": 0.99,
               "ci95_daily": [0.10, 1.85]},
    "best":   {"be_trig": 0.30, "tr_act": 0.60, "tr_dist": 0.30,
               "expectancy_r": 0.18, "win_rate_pct": 48.0,
               "projected_daily_pnl_usd": 3.57,
               "ci95_daily": [1.92, 5.21]},
    "delta_daily_pnl_usd": 2.58,
    "trusted": true
  }
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from core.utils import (  # noqa: E402
    read_json_state,
    setup_logger,
    utc_now_iso,
    write_json_state,
)

# Grids (R units, identical to calibrate_be_trail.py for consistency)
BE_TRIG_GRID = (0.2, 0.3, 0.4, 0.5, 0.6, 0.65, 0.8, 1.0)
BE_LOCK_GRID = (0.05, 0.08, 0.10, 0.15)
TR_ACT_GRID = (0.5, 0.6, 0.7, 0.8)
TR_DIST_GRID = (0.20, 0.25, 0.30, 0.40)

MIN_N_EVAL = 8
MIN_N_TRUSTED = 50           # below this: observe-only
BOOTSTRAP_N = 500
BOOTSTRAP_SEED = 4243

# Defaults if config.yaml is missing per-symbol overrides.
DEFAULT_BE_TRIG = 0.65
DEFAULT_BE_LOCK = 0.10
DEFAULT_TR_ACT = 0.75
DEFAULT_TR_DIST = 0.35


# ---------------------------------------------------------------------------
# Helpers (re-use calibrate_be_trail._exit pattern for behavioral parity)
# ---------------------------------------------------------------------------
def _lookup_mae_mfe(t: dict[str, Any]) -> tuple[float | None, float | None]:
    """mae_R / mfe_R live at top level OR inside the position_mgmt sub-dict.

    Trade-log backfill for earlier sessions stored these under
    position_mgmt; later sessions lifted them to top level. Both layouts
    appear in the 696 historic trades on this account, so we check both.
    """
    mae = t.get("mae_R"); mfe = t.get("mfe_R")
    if mae is None or mfe is None:
        pm = t.get("position_mgmt") or {}
        if isinstance(pm, dict):
            mae = mae if mae is not None else pm.get("mae_R") or pm.get("mae_pct_of_risk")
            mfe = mfe if mfe is not None else pm.get("mfe_R") or pm.get("mfe_pct_of_risk")
    return mae, mfe


def _prep_trade(t: dict[str, Any]) -> tuple | None:
    """Return (mae_R, mfe_R, r_mult, tp1_R, abs_risk_dollar) or None."""
    e = t.get("entry"); sl = t.get("sl_initial") or t.get("sl")
    tp1 = t.get("tp1")
    mae, mfe = _lookup_mae_mfe(t)
    if e is None or sl is None or mae is None or mfe is None:
        return None
    try:
        e = float(e); sl = float(sl); mae = float(mae); mfe = float(mfe)
        orig_r = abs(e - sl)
        if orig_r <= 0:
            return None
        tp1_r = abs(float(tp1) - e) / orig_r if tp1 is not None else None
    except (TypeError, ValueError):
        return None
    try:
        r_mult = float(t.get("r_multiple")) if t.get("r_multiple") is not None else None
    except (TypeError, ValueError):
        r_mult = None
    pnl = t.get("pnl")
    try:
        pnl_f = float(pnl) if pnl is not None else None
    except (TypeError, ValueError):
        pnl_f = None
    abs_risk_dollar = abs(pnl_f / r_mult) if (pnl_f is not None and r_mult and abs(r_mult) > 0.001) else orig_r
    return (mae, mfe, r_mult, tp1_r, abs_risk_dollar)


def _exit(mae: float, mfe: float, r_mult: float | None, tp1_r: float | None,
          be_trig: float, be_lock: float, tr_act: float, tr_dist: float) -> float:
    """Resolve a trade's counterfactual R-multiple under (be_trig, ..., tr_dist).

    Mirror of calibrate_be_trail.py's _exit — kept inline here so this
    script has zero cross-script imports. Behavior MUST stay identical.
    """
    if tp1_r is not None and mfe >= tp1_r:
        return float(tp1_r)
    if mae >= 1.0:
        return be_lock if mfe >= be_trig else -1.0
    if mfe >= tr_act:
        return max(mfe - tr_dist, -1.0)
    if mfe >= be_trig:
        return be_lock
    return float(r_mult) if r_mult is not None else 0.0


def _bootstrap_ci95(values: list[float], *, n_boot: int = BOOTSTRAP_N,
                    seed: int = BOOTSTRAP_SEED,
                    stat: str = "mean") -> tuple[float, float]:
    """Bootstrap-CI95 of `stat` over `values`."""
    if len(values) < 2:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    boot: list[float] = []
    for _ in range(n_boot):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        if stat == "mean":
            boot.append(sum(sample) / n)
        elif stat == "sum":
            boot.append(sum(sample))
        else:
            raise ValueError(f"unsupported stat={stat}")
    boot.sort()
    return (boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot)) - 1])


def _trades_per_day(trades: list[dict[str, Any]]) -> float:
    """Median trades-per-day from a symbol's close timestamps."""
    timestamps: list[datetime] = []
    for t in trades:
        ca = t.get("closed_at")
        if not ca:
            continue
        try:
            ts = datetime.fromisoformat(str(ca).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        timestamps.append(ts)
    if len(timestamps) < 2:
        return 0.0
    timestamps.sort()
    day_buckets: dict[str, int] = {}
    for ts in timestamps:
        day_buckets.setdefault(ts.date().isoformat(), 0)
        day_buckets[ts.date().isoformat()] += 1
    counts = list(day_buckets.values())
    return float(statistics.median(counts))


def _seeded_r_units(config: dict[str, Any], symbol: str) -> tuple[float, float, float, float]:
    """Read per-symbol seed from config.yaml trading.{break_even,trailing}."""
    tr = (config.get("trading") or {})
    be = tr.get("break_even") or {}
    tl = tr.get("trailing") or {}
    be_per = be.get("per_symbol") or {}
    tl_per = tl.get("per_symbol") or {}
    be_sym = be_per.get(symbol, {}) if isinstance(be_per, dict) else {}
    tl_sym = tl_per.get(symbol, {}) if isinstance(tl_per, dict) else {}
    return (
        float(be_sym.get("trigger_atr_mult", be.get("trigger_atr_mult", DEFAULT_BE_TRIG))),
        float(be_sym.get("lock_profit_atr_mult", be.get("lock_profit_atr_mult", DEFAULT_BE_LOCK))),
        float(tl_sym.get("activation_atr_mult", tl.get("activation_atr_mult", DEFAULT_TR_ACT))),
        float(tl_sym.get("trail_atr_mult", tl.get("trail_atr_mult", DEFAULT_TR_DIST))),
    )


# ---------------------------------------------------------------------------
# Per-symbol projection
# ---------------------------------------------------------------------------
def project_symbol(
    symbol: str,
    sym_trades: list[dict[str, Any]],
    config: dict[str, Any],
    log: logging.Logger,
) -> dict[str, Any] | None:
    """Run the per-symbol grid search; return the projection row or None if n<MIN_N_EVAL."""
    pre = [p for p in (_prep_trade(t) for t in sym_trades) if p is not None]
    n = len(pre)
    if n < MIN_N_EVAL:
        return None

    abs_risks = [p[4] for p in pre]
    avg_risk = sum(abs_risks) / n

    bt0, bl0, ta0, td0 = _seeded_r_units(config, symbol)
    seed_outs = [_exit(*p[:4], bt0, bl0, ta0, td0) for p in pre]
    seed_dollar = [r * abs_risks[i] for i, r in enumerate(seed_outs)]
    seed_exp = sum(seed_outs) / n
    seed_wr = sum(1 for r in seed_outs if r > 0) / n
    seed_ci_lo, seed_ci_hi = _bootstrap_ci95(seed_outs)

    best = {
        "exp": seed_exp, "wr": seed_wr,
        "bt": bt0, "bl": bl0, "ta": ta0, "td": td0,
        "ci_lo": seed_ci_lo, "ci_hi": seed_ci_hi,
        "delta_dollar": 0.0,
        "ci_daily_lo": 0.0, "ci_daily_hi": 0.0,
    }
    for bt in BE_TRIG_GRID:
        for bl in BE_LOCK_GRID:
            for ta in TR_ACT_GRID:
                for td in TR_DIST_GRID:
                    if ta <= bt:
                        continue
                    outs = [_exit(*p[:4], bt, bl, ta, td) for p in pre]
                    exp = sum(outs) / n
                    if exp > best["exp"]:
                        wr = sum(1 for r in outs if r > 0) / n
                        ci_lo, ci_hi = _bootstrap_ci95(outs)
                        best = {
                            "exp": exp, "wr": wr,
                            "bt": bt, "bl": bl, "ta": ta, "td": td,
                            "ci_lo": ci_lo, "ci_hi": ci_hi,
                        }
    trades_per_day = _trades_per_day(sym_trades)

    seed_daily = trades_per_day * seed_exp * avg_risk
    seed_ci_lo_daily, seed_ci_hi_daily = _bootstrap_ci95(
        seed_dollar, stat="mean"
    )
    seed_daily_low = trades_per_day * seed_ci_lo_daily * avg_risk
    seed_daily_high = trades_per_day * seed_ci_hi_daily * avg_risk

    best_outs = [_exit(*p[:4], best["bt"], best["bl"], best["ta"], best["td"]) for p in pre]
    best_dollar = [r * abs_risks[i] for i, r in enumerate(best_outs)]
    best_daily = trades_per_day * best["exp"] * avg_risk
    best_ci_lo_daily, best_ci_hi_daily = _bootstrap_ci95(
        best_dollar, stat="mean"
    )
    best_daily_low = trades_per_day * best_ci_lo_daily * avg_risk
    best_daily_high = trades_per_day * best_ci_hi_daily * avg_risk

    positive_ci = best["ci_lo"] > 0.0
    beats_seed_ci = best["ci_lo"] > seed_ci_hi
    trusted = bool(n >= MIN_N_TRUSTED and positive_ci and beats_seed_ci)

    log.info(
        "%s: n=%d trades/day=%.2f avg_risk=$%.4f | seed %.2f/%.2f/%.2f/%.2f exp=%.3f $%.4f/day | "
        "best %.2f/%.2f/%.2f/%.2f exp=%.3f $%.4f/day | trusted=%s",
        symbol, n, trades_per_day, avg_risk,
        bt0, bl0, ta0, td0, seed_exp, seed_daily,
        best["bt"], best["bl"], best["ta"], best["td"],
        best["exp"], best_daily, trusted,
    )

    return {
        "symbol": symbol,
        "n": int(n),
        "trades_per_day": round(trades_per_day, 2),
        "avg_risk_dollar": round(avg_risk, 4),
        "seed": {
            "be_trig": round(bt0, 4), "be_lock": round(bl0, 4),
            "tr_act": round(ta0, 4), "tr_dist": round(td0, 4),
            "expectancy_r": round(seed_exp, 4),
            "win_rate_pct": round(seed_wr * 100, 1),
            "projected_daily_pnl_usd": round(seed_daily, 4),
            "ci95_daily": [round(seed_daily_low, 4), round(seed_daily_high, 4)],
        },
        "best": {
            "be_trig": round(best["bt"], 4), "be_lock": round(best["bl"], 4),
            "tr_act": round(best["ta"], 4), "tr_dist": round(best["td"], 4),
            "expectancy_r": round(best["exp"], 4),
            "win_rate_pct": round(best["wr"] * 100, 1),
            "ci95_r": [round(best["ci_lo"], 4), round(best["ci_hi"], 4)],
            "projected_daily_pnl_usd": round(best_daily, 4),
            "ci95_daily": [round(best_daily_low, 4), round(best_daily_high, 4)],
        },
        "delta_daily_pnl_usd": round(best_daily - seed_daily, 4),
        "trusted": trusted,
        "reason": (
            "data-driven: best cell beats seed CI95 + best CI95_lo > 0"
            if trusted else
            f"n<{MIN_N_TRUSTED} ({n})" if n < MIN_N_TRUSTED else
            f"best CI95_lo={best['ci_lo']:.4f} not > 0" if not positive_ci else
            f"best CI95={best['ci_lo']:.4f}..{best['ci_hi']:.4f} doesn't beat seed CI95_high={seed_ci_hi:.4f}"
        ),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Project per-symbol daily PnL under the best BE/trail grid "
            "cell, with bootstrap CI95. Honest estimate; not a guarantee."
        )
    )
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--symbols", nargs="*", default=None,
                    help="Limit to specific symbols (default: all)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    log = setup_logger("verify_edge", "verify_edge.log")
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    trade_log = read_json_state("trade_log.json", default={}) or {}
    trades = trade_log.get("trades", []) if isinstance(trade_log, dict) else []
    log.info("Loaded %d trades from state/trade_log.json", len(trades))

    by_sym: dict[str, list[dict]] = {}
    for t in trades:
        sym = t.get("symbol")
        if sym:
            by_sym.setdefault(str(sym), []).append(t)

    target_syms = args.symbols or sorted(by_sym.keys())
    rows: list[dict[str, Any]] = []
    skipped_below_min = 0
    for sym in target_syms:
        if sym not in by_sym:
            log.warning("Symbol %s not in trade log; skipping", sym)
            continue
        all_n = len(by_sym[sym])
        prep_n = sum(1 for t in by_sym[sym] if _prep_trade(t) is not None)
        if prep_n < MIN_N_EVAL:
            skipped_below_min += 1
            log.info(
                "%s: %d raw trades, %d prepped (mae_R/mfe_R available); "
                "%d < MIN_N_EVAL=%d -> skip",
                sym, all_n, prep_n, prep_n, MIN_N_EVAL,
            )
            continue
        row = project_symbol(sym, by_sym[sym], config, log)
        if row is not None:
            rows.append(row)

    if not rows:
        log.warning(
            "No symbols had prep_n>=MIN_N_EVAL (%d). Run "
            "scripts/backfill_mae_mfe.py first to stamp mae_R/mfe_R onto "
            "trade_log.json trades.",
            MIN_N_EVAL,
        )

    trusted_rows = [r for r in rows if r.get("trusted")]
    daily_total_best = sum(r["best"]["projected_daily_pnl_usd"]
                           for r in trusted_rows) if trusted_rows else 0.0
    daily_total_seed = sum(r["seed"]["projected_daily_pnl_usd"]
                           for r in trusted_rows) if trusted_rows else 0.0

    report = {
        "generated_at": utc_now_iso(),
        "engine": "verify_edge.py",
        "n_trades_total": len(trades),
        "n_symbols_evaluated": len(rows),
        "n_symbols_trusted": len(trusted_rows),
        "honesty_note": (
            "These are PROJECTIONS from historic trade outcomes + a BE/trail "
            "grid search. NOT a guarantee. Trade outcomes are stochastic; "
            "future PnL may diverge from projection, especially when n<50 "
            "per symbol."
        ),
        "daily_pnl_usd": {
            "current_seed_sum": round(daily_total_seed, 4),
            "best_grid_sum_trusted_only": round(daily_total_best, 4),
            "delta": round(daily_total_best - daily_total_seed, 4),
        },
        "per_symbol": rows,
    }
    log.info(
        "Projection: %d symbols evaluated, %d trusted. "
        "Current-seed daily = $%.4f, best-grid daily = $%.4f, delta = $%.4f",
        len(rows), len(trusted_rows),
        daily_total_seed, daily_total_best, daily_total_best - daily_total_seed,
    )

    if args.dry_run:
        print(json.dumps(report, indent=2, default=str))
        return 0

    out_path = "edge_projection_" + utc_now_iso().replace(":", "").replace("-", "")[:13] + ".json"
    write_json_state(out_path, report)
    log.info("Wrote state/%s", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
