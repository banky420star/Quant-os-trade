"""Data-driven per-symbol SL/TP calibration (the "auto-tune later" half of the
USER 2026-07-01 SL/TP-calibration request).

Why this exists: ``core/strategy_entry.py`` now reads per-symbol SL/TP seeds
from ``config.yaml`` (``trading.strategy_entries.sl_tp``). Those seeds are
hand-set by symbol class (FX tight, gold/oil medium, BTC wide). This script
*learns* better values from the bot's own clean closed trades, when there are
enough of them, and writes a live override to ``state/symbol_sltp_live.json``
that ``_sltp_cfg`` honors over the seeds.

Method (MAE/MFE frontier): every closed trade already carries
``mae_R`` (max adverse excursion) and ``mfe_R`` (max favorable excursion) in
units of the *original* risk (``|entry - sl_initial|``). For a candidate
``(k_sl, rr1, rr2)`` — where ``k_sl`` scales the stop vs the original SL and
``rr1/rr2`` are the TP multiples of the *new* risk — we re-resolve each trade:

  * stopped      -> -1.0 new-R            if mae_R >= k_sl
  * hit TP2      -> +(rr1+rr2)/2 new-R    elif mfe_R >= rr2*k_sl   (50/50 scale)
  * hit TP1      -> +rr1 new-R            elif mfe_R >= rr1*k_sl
  * neither      -> realized r_multiple / k_sl   (actual outcome, scaled)

Limitation (honest): we know the maxima, not the *path*, so when both the stop
and a target were touched we pessimistically assume the stop hit first. This
makes the calibration conservative (bias toward wider stops). It is an
approximation, not a backtest. We only ship an override when the best candidate
beats the *seeded default's* expectancy under the same model AND is itself
positive — so this can only improve on the seeds, never ship noise.

Run:
    python scripts/calibrate_sltp.py            # write state/symbol_sltp_live.json
    python scripts/calibrate_sltp.py --dry-run  # print only
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from core.utils import read_json_state, setup_logger, write_json_state  # noqa: E402

STATE = Path(__file__).resolve().parent.parent / "state"

# MAE/MFE frontier grid (kept small — thin data, no point over-searching).
K_SL_GRID = (0.8, 1.0, 1.2, 1.5)      # stop distance vs the original SL
RR1_GRID = (1.0, 1.2, 1.5, 1.6, 2.0)  # TP1 in R
RR2_GRID = (2.0, 2.5, 2.8, 3.0, 3.5)  # TP2 in R
MIN_N = 8                              # need this many clean trades to even evaluate
# An override is only APPLIED (trusted) when it clears a selection-aware bar:
# enough trades *and* the chosen cell's expectancy is bootstrap-CI95 positive.
# This mirrors the project's DSR/SPA/PBO stance: an uncorrected 100-cell search
# on n=17-46 is selection-biased (the regime-alloc "winners" were rescinded by
# PBO for the same reason), so the bar to *apply* is deliberately high.
APPLY_MIN_N = 50
APPLY_BOOTSTRAP = 2000
APPLY_BOOTSTRAP_SEED = 4242


def _seeded_defaults(config: dict[str, Any], symbol: str) -> dict[str, float]:
    se = (config.get("trading") or {}).get("strategy_entries") or {}
    block = se.get("sl_tp") if isinstance(se, dict) else None
    base = {"sl_atr_mult": 0.5, "risk_floor_atr_mult": 1.5, "risk_floor_pct": 0.001,
            "tp1_rr": 1.5, "tp2_rr": 2.5}
    if isinstance(block, dict):
        for k in base:
            if k in block:
                base[k] = block[k]
        per = block.get("per_symbol") or {}
        if symbol in per and isinstance(per[symbol], dict):
            base.update({k: per[symbol][k] for k in base if k in per[symbol]})
    return base


def _outcome(mae_r: float, mfe_r: float, r_mult: float | None,
             k_sl: float, rr1: float, rr2: float) -> float:
    """Resolve one trade to a new-R outcome under candidate (k_sl, rr1, rr2)."""
    if mae_r >= k_sl:
        return -1.0  # stopped (pessimistic when both touched)
    if mfe_r >= rr2 * k_sl:
        return (rr1 + rr2) / 2.0  # hit TP2 (assume 50/50 scale-out)
    if mfe_r >= rr1 * k_sl:
        return rr1  # hit TP1
    # Neither level touched: use the realized outcome scaled to new-R.
    if r_mult is None:
        return 0.0
    return r_mult / k_sl


def _expectancy(trades: list[dict], k_sl: float, rr1: float, rr2: float) -> tuple[float, float]:
    outs = []
    for t in trades:
        mae = t.get("mae_R")
        mfe = t.get("mfe_R")
        if mae is None or mfe is None:
            continue
        try:
            mae = float(mae); mfe = float(mfe)
        except (TypeError, ValueError):
            continue
        outs.append(_outcome(mae, mfe, t.get("r_multiple"), k_sl, rr1, rr2))
    if not outs:
        return (0.0, 0.0)
    wr = sum(1 for o in outs if o > 0) / len(outs)
    return (sum(outs) / len(outs), wr)


def _bootstrap_ci95(trades: list[dict], k_sl: float, rr1: float, rr2: float,
                    n_boot: int = APPLY_BOOTSTRAP, seed: int = APPLY_BOOTSTRAP_SEED) -> tuple[float, float]:
    """Percentile-bootstrap CI95 on the expectancy of ONE pre-chosen cell.

    Note: this is the CI of the *selected* cell, not a selection-corrected
    DSR. Selection correction here is handled by the n>=APPLY_MIN_N gate (thin
    data is the dominant failure mode) plus requiring the CI lower bound > 0.
    Honest about that limitation in the log.
    """
    import random
    rng = random.Random(seed)
    pre = []
    for t in trades:
        mae = t.get("mae_R"); mfe = t.get("mfe_R")
        if mae is None or mfe is None:
            continue
        try:
            pre.append((float(mae), float(mfe), t.get("r_multiple")))
        except (TypeError, ValueError):
            continue
    n = len(pre)
    if n < 2:
        return (0.0, 0.0)
    exps = []
    for _ in range(n_boot):
        outs = [_outcome(pre[rng.randrange(n)][0], pre[rng.randrange(n)][1],
                         pre[rng.randrange(n)][2], k_sl, rr1, rr2)
                for _ in range(n)]
        exps.append(sum(outs) / n)
    exps.sort()
    lo = exps[int(0.025 * len(exps))]
    hi = exps[int(0.975 * len(exps)) - 1]
    return (lo, hi)


def calibrate(config: dict[str, Any], log: logging.Logger) -> dict[str, Any]:
    tlog = read_json_state("trade_log.json", default={}) or {}
    trades = tlog.get("trades", []) if isinstance(tlog, dict) else []
    by_sym: dict[str, list[dict]] = {}
    for t in trades:
        if t.get("archive_polluted"):
            continue
        sym = t.get("symbol")
        if not sym or t.get("mae_R") is None or t.get("mfe_R") is None:
            continue
        by_sym.setdefault(sym, []).append(t)

    out: dict[str, Any] = {"symbols": {}, "updated_at": None}
    for sym, sym_trades in sorted(by_sym.items()):
        n = len(sym_trades)
        if n < MIN_N:
            log.info("%s: n=%d < min_n=%d -> skip (keep seeds)", sym, n, MIN_N)
            continue
        seeds = _seeded_defaults(config, sym)
        # Baseline expectancy under the seeded default (k_sl=1.0 = original SL).
        base_exp, _ = _expectancy(sym_trades, 1.0, float(seeds["tp1_rr"]), float(seeds["tp2_rr"]))
        best = {"exp": base_exp, "wr": 0.0, "k_sl": 1.0, "rr1": float(seeds["tp1_rr"]),
                "rr2": float(seeds["tp2_rr"])}
        for k_sl in K_SL_GRID:
            for rr1 in RR1_GRID:
                for rr2 in RR2_GRID:
                    if rr2 <= rr1:
                        continue
                    exp, wr = _expectancy(sym_trades, k_sl, rr1, rr2)
                    if exp > best["exp"]:
                        best = {"exp": exp, "wr": wr, "k_sl": k_sl, "rr1": rr1, "rr2": rr2}
        # Ship only if the best candidate beats the seeded default, is positive,
        # has enough trades, AND its expectancy is bootstrap-CI95 positive.
        # The n>=APPLY_MIN_N + CI95 gate is the selection-aware bar (a 100-cell
        # search on n=17-46 is selection-biased; this stops it auto-applying).
        beats = best["exp"] > base_exp + 1e-6
        positive = best["exp"] > 0.0
        enough = n >= APPLY_MIN_N
        ci_lo, ci_hi = (0.0, 0.0)
        if enough and beats and positive:
            ci_lo, ci_hi = _bootstrap_ci95(sym_trades, best["k_sl"], best["rr1"], best["rr2"])
        ci_positive = ci_lo > 0.0
        trusted = bool(enough and beats and positive and ci_positive)
        # The live override expresses the new SL as an ATR multiple. We don't
        # know each historical trade's ATR, so we ship ATR multiples that scale
        # the seeded SL by k_sl (k_sl>1 -> wider stop), plus the new RR.
        #
        # BUGFIX 2026-08-04: the effective SL is
        #   sl = min(support - sl_atr_mult*atr, entry - risk_floor)
        # where risk_floor = atr * risk_floor_atr_mult (see strategy_entry.py).
        # risk_floor_atr_mult (1.2-1.6) is the BINDING term in the normal case
        # (it sits further from entry than support - sl_atr_mult*atr), so the
        # previous version — which scaled only sl_atr_mult by k_sl and left
        # risk_floor_atr_mult at the seed — made trusted overrides into no-ops:
        # the effective SL never moved. Scale BOTH by k_sl so the wider-stop
        # finding actually takes effect when a symbol clears the trust gate.
        sl_atr_mult = round(float(seeds["sl_atr_mult"]) * best["k_sl"], 4)
        risk_floor_atr_mult = round(float(seeds["risk_floor_atr_mult"]) * best["k_sl"], 4)
        entry = {
            "n": n,
            "sl_atr_mult": sl_atr_mult,
            "risk_floor_atr_mult": risk_floor_atr_mult,
            "risk_floor_pct": float(seeds["risk_floor_pct"]),
            "tp1_rr": round(best["rr1"], 4),
            "tp2_rr": round(best["rr2"], 4),
            "expectancy_r": round(best["exp"], 4),
            "seed_expectancy_r": round(base_exp, 4),
            "win_rate_pct": round(best["wr"] * 100, 1),
            "k_sl": best["k_sl"],
            "ci95": [round(ci_lo, 4), round(ci_hi, 4)],
            "trusted": trusted,
            "reason": ("data-driven" if trusted else
                       "thin (n<%d)" % APPLY_MIN_N if not enough else
                       "not beating seed" if not beats else
                       "non-positive" if not positive else "ci95_lo<=0"),
        }
        out["symbols"][sym] = entry
        log.info("%s: n=%d baseline_exp=%.3f best_exp=%.3f (k_sl=%.2f rr=%.1f/%.1f) "
                 "ci95=[%.3f,%.3f] -> %s",
                 sym, n, base_exp, best["exp"], best["k_sl"], best["rr1"], best["rr2"],
                 ci_lo, ci_hi, "TRUSTED" if trusted else "keep seed (not applied)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Data-driven per-symbol SL/TP calibration")
    ap.add_argument("--dry-run", action="store_true", help="Print only; write nothing")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()
    log = setup_logger("calibrate_sltp", "calibrate_sltp.log")
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    out = calibrate(config, log)
    from core.utils import utc_now_iso
    out["updated_at"] = utc_now_iso()
    log.info("Calibration: %d symbols, %d trusted overrides",
             len(out["symbols"]), sum(1 for v in out["symbols"].values() if v.get("trusted")))
    if args.dry_run:
        print(json.dumps(out, indent=2, default=str))
        return 0
    write_json_state("symbol_sltp_live.json", out)
    log.info("Wrote state/symbol_sltp_live.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())