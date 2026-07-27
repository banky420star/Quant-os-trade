"""Data-driven per-symbol break-even + trailing calibration.

Companion to ``calibrate_sltp.py`` (the USER 2026-07-01 "same effect for trails
and break-evens" request). ``config.yaml`` already carries per-symbol BE/trail
SEEDS for every symbol; this script learns better values from the bot's own
clean closed trades and writes ``state/symbol_be_trail_live.json``, which
``core/position_manager.py:_merge_live`` honors over the seeds when ``trusted``.

Method (MAE/MFE frontier, direction-normalized — mae_R = adverse, mfe_R =
favorable, both in units of the original risk |entry - sl_initial|):
For a candidate ``(be_trig, be_lock, tr_act, tr_dist)`` in R, resolve each trade:

  * hit TP1            -> +tp1_R                  if mfe_R >= tp1_R
  * stopped, no BE     -> -1.0                    elif mae_R >= 1.0 and mfe_R < be_trig
  * BE saved a loser   -> +be_lock                elif mae_R >= 1.0 and mfe_R >= be_trig
  * trailing locked     -> +(mfe_R - tr_dist)      elif mfe_R >= tr_act
  * BE only            -> +be_lock                elif mfe_R >= be_trig
  * neither resolved   -> realized r_multiple     (actual outcome)

HONEST LIMITATION (read me): trailing depends on the *intrabar path* (where the
peak fell vs the trough), and we only have the maxima. This model assumes the
favorable peak is reached BEFORE the adverse trough ("good-then-bad"), which is
OPTIMISTIC for trailing (the ratchet locks at the best peak). Real exits are
worse than this model predicts. We therefore ship ONLY when the chosen cell
beats the seeded default's expectancy under the SAME model AND clears n>=50 AND
its expectancy is bootstrap-CI95 positive -- so on today's thin data it records
recommendations but applies nothing (the seeds stay active), matching the
project's DSR/SPA/PBO stance against selection artifacts.

Run:
    python scripts/calibrate_be_trail.py            # write state/symbol_be_trail_live.json
    python scripts/calibrate_be_trail.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from core.utils import read_json_state, setup_logger, utc_now_iso, write_json_state  # noqa: E402

# Grids (R units = multiples of the original risk |entry - sl_initial|).
BE_TRIG_GRID = (0.3, 0.4, 0.5, 0.6)
BE_LOCK_GRID = (0.05, 0.08, 0.1, 0.15)
TR_ACT_GRID = (0.5, 0.6, 0.7, 0.8)
TR_DIST_GRID = (0.2, 0.25, 0.3, 0.4)
MIN_N = 8          # evaluate
APPLY_MIN_N = 50   # apply
APPLY_BOOTSTRAP = 2000
APPLY_SEED = 4243


def _seeded(config: dict[str, Any], symbol: str) -> dict[str, dict[str, float]]:
    tr = (config.get("trading") or {})
    be = tr.get("break_even") or {}
    tl = tr.get("trailing") or {}
    be_sym = (be.get("per_symbol") or {}).get(symbol, {}) if isinstance(be.get("per_symbol"), dict) else {}
    tl_sym = (tl.get("per_symbol") or {}).get(symbol, {}) if isinstance(tl.get("per_symbol"), dict) else {}
    return {
        "break_even": {"trigger_atr_mult": float(be_sym.get("trigger_atr_mult", be.get("trigger_atr_mult", 0.5))),
                       "lock_profit_atr_mult": float(be_sym.get("lock_profit_atr_mult", be.get("lock_profit_atr_mult", 0.1)))},
        "trailing": {"activation_atr_mult": float(tl_sym.get("activation_atr_mult", tl.get("activation_atr_mult", 0.75))),
                      "trail_atr_mult": float(tl_sym.get("trail_atr_mult", tl.get("trail_atr_mult", 0.35)))},
    }


def _prep(trades: list[dict]) -> list[tuple]:
    """Extract (mae_R, mfe_R, r_mult, tp1_R) per clean trade (R units)."""
    out = []
    for t in trades:
        if t.get("archive_polluted"):
            continue
        e = t.get("entry"); sl = t.get("sl_initial") or t.get("sl"); tp1 = t.get("tp1")
        mae = t.get("mae_R"); mfe = t.get("mfe_R")
        if e is None or sl is None or mae is None or mfe is None:
            continue
        try:
            e = float(e); sl = float(sl); mae = float(mae); mfe = float(mfe)
            orig_r = abs(e - sl)
            if orig_r <= 0:
                continue
            tp1_r = abs(float(tp1) - e) / orig_r if tp1 is not None else None
        except (TypeError, ValueError):
            continue
        rm = t.get("r_multiple")
        try:
            rm = float(rm) if rm is not None else None
        except (TypeError, ValueError):
            rm = None
        out.append((mae, mfe, rm, tp1_r))
    return out


def _exit(mae: float, mfe: float, r_mult: float | None, tp1_r: float | None,
          be_trig: float, be_lock: float, tr_act: float, tr_dist: float) -> float:
    if tp1_r is not None and mfe >= tp1_r:
        return tp1_r
    if mae >= 1.0:
        return be_lock if mfe >= be_trig else -1.0   # BE saved it, else stopped
    if mfe >= tr_act:
        return max(mfe - tr_dist, -1.0)               # trailing locked profit
    if mfe >= be_trig:
        return be_lock                                 # BE only
    return r_mult if r_mult is not None else 0.0       # neither resolved -> actual


def _expectancy(pre: list[tuple], be_trig: float, be_lock: float,
                tr_act: float, tr_dist: float) -> tuple[float, float]:
    outs = [_exit(*p, be_trig, be_lock, tr_act, tr_dist) for p in pre]
    if not outs:
        return (0.0, 0.0)
    wr = sum(1 for o in outs if o > 0) / len(outs)
    return (sum(outs) / len(outs), wr)


def _bootstrap_ci95(pre: list[tuple], be_trig: float, be_lock: float,
                    tr_act: float, tr_dist: float, n_boot: int = APPLY_BOOTSTRAP,
                    seed: int = APPLY_SEED) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(pre)
    if n < 2:
        return (0.0, 0.0)
    exps = []
    for _ in range(n_boot):
        outs = [_exit(*pre[rng.randrange(n)], be_trig, be_lock, tr_act, tr_dist) for _ in range(n)]
        exps.append(sum(outs) / n)
    exps.sort()
    return (exps[int(0.025 * len(exps))], exps[int(0.975 * len(exps)) - 1])


def _seed_to_R(seeds: dict[str, dict[str, float]], orig_risk_atr: float = 1.5) -> tuple[float, float, float, float]:
    """Approximate the seeded ATR-mults to R units (1R ~ risk_floor_atr_mult*atr,
    assumed ~1.5 atr when the per-trade ATR is unknown). Used only to compute the
    baseline expectancy under the seed, not shipped."""
    be = seeds["break_even"]; tl = seeds["trailing"]
    return (be["trigger_atr_mult"] / orig_risk_atr,
            be["lock_profit_atr_mult"] / orig_risk_atr,
            tl["activation_atr_mult"] / orig_risk_atr,
            tl["trail_atr_mult"] / orig_risk_atr)


def calibrate(config: dict[str, Any], log: logging.Logger) -> dict[str, Any]:
    tlog = read_json_state("trade_log.json", default={}) or {}
    trades = tlog.get("trades", []) if isinstance(tlog, dict) else []
    by_sym: dict[str, list[dict]] = {}
    for t in trades:
        sym = t.get("symbol")
        if sym:
            by_sym.setdefault(sym, []).append(t)

    out: dict[str, Any] = {"symbols": {}, "updated_at": None}
    for sym, sym_trades in sorted(by_sym.items()):
        pre = _prep(sym_trades)
        n = len(pre)
        if n < MIN_N:
            log.info("%s: n=%d < min_n=%d -> skip", sym, n, MIN_N)
            continue
        seeds = _seeded(config, sym)
        b_trig, b_lock, t_act, t_dist = _seed_to_R(seeds)
        base_exp, _ = _expectancy(pre, b_trig, b_lock, t_act, t_dist)
        best = {"exp": base_exp, "wr": 0.0, "bt": b_trig, "bl": b_lock, "ta": t_act, "td": t_dist}
        for bt in BE_TRIG_GRID:
            for bl in BE_LOCK_GRID:
                for ta in TR_ACT_GRID:
                    for td in TR_DIST_GRID:
                        if ta <= bt:  # trailing must activate above BE trigger
                            continue
                        exp, wr = _expectancy(pre, bt, bl, ta, td)
                        if exp > best["exp"]:
                            best = {"exp": exp, "wr": wr, "bt": bt, "bl": bl, "ta": ta, "td": td}
        beats = best["exp"] > base_exp + 1e-6
        positive = best["exp"] > 0.0
        enough = n >= APPLY_MIN_N
        ci_lo, ci_hi = (0.0, 0.0)
        if enough and beats and positive:
            ci_lo, ci_hi = _bootstrap_ci95(pre, best["bt"], best["bl"], best["ta"], best["td"])
        trusted = bool(enough and beats and positive and ci_lo > 0.0)
        entry = {
            "n": n,
            "break_even": {"trigger_atr_mult": round(best["bt"], 4), "lock_profit_atr_mult": round(best["bl"], 4)},
            "trailing": {"activation_atr_mult": round(best["ta"], 4), "trail_atr_mult": round(best["td"], 4)},
            "expectancy_r": round(best["exp"], 4),
            "seed_expectancy_r": round(base_exp, 4),
            "win_rate_pct": round(best["wr"] * 100, 1),
            "ci95": [round(ci_lo, 4), round(ci_hi, 4)],
            "trusted": trusted,
            "reason": ("data-driven" if trusted else
                       "thin (n<%d)" % APPLY_MIN_N if not enough else
                       "not beating seed" if not beats else
                       "non-positive" if not positive else "ci95_lo<=0"),
        }
        out["symbols"][sym] = entry
        log.info("%s: n=%d seed_exp=%.3f best_exp=%.3f (be=%.2f/%.2f tr=%.2f/%.2f) ci95=[%.3f,%.3f] -> %s",
                 sym, n, base_exp, best["exp"], best["bt"], best["bl"], best["ta"], best["td"],
                 ci_lo, ci_hi, "TRUSTED" if trusted else "keep seed (not applied)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Data-driven per-symbol BE/trailing calibration")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()
    log = setup_logger("calibrate_be_trail", "calibrate_be_trail.log")
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    out = calibrate(config, log)
    out["updated_at"] = utc_now_iso()
    log.info("BE/trail calibration: %d symbols, %d trusted",
             len(out["symbols"]), sum(1 for v in out["symbols"].values() if v.get("trusted")))
    if args.dry_run:
        print(json.dumps(out, indent=2, default=str))
        return 0
    write_json_state("symbol_be_trail_live.json", out)
    log.info("Wrote state/symbol_be_trail_live.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())