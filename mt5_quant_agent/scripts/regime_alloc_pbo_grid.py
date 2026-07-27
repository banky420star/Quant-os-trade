"""Dense EMA-span grid PBO/SPA confirmatory test for the regime-allocation edge.

WHY: cycle 9 found the first ORGANIC WINNERS (spans 100d and 250d pass all gates under
K=4 cross-trial deflation) but the PRE-REGISTERED 200d spec FAILS K=4 (0.9155) -- so the
passing cells are post-hoc and the confirmatory result is negative. The decisive
family-level question is: is the WHOLE EMA-span family overfit, or is the edge robust
to span specification? Per López de Prado / research agent (cycle 10):

  * CSCV/PBO (Bailey-Borwein-LdP-Zhu) on the dense span-grid is the decisive test.
    PBO > 0.05  -> the whole family is overfit; the 100d/250d wins are selection
    artifacts, end of story. PBO low -> the family is NOT overfit and the edge is
    robust to span choice (real).
  * The dense grid makes cross-trial Sharpe variance COMPUTABLE and non-zero, so
    DSR's n_trials term finally has a real effect (unlike the sr_var=0 K=1 case).
  * Hansen SPA across the grid tests whether ANY span beats HOLD=0 with multiplicity
    control (stationary bootstrap, consistent recentering).

This is HONEST multiple-testing: we pay the K-inflation tax up-front by declaring the
full span grid pre-hoc, so the deflation bar is real. A high PBO + high SPA p would be
the honest end of the regime-alloc edge; a low PBO + low SPA p would upgrade the
characterization from "post-hoc winner" to "family-robust edge".

Grid: EMA spans {50,75,100,...,300}d (11 trials). Cost 30bps RT, same weight-tracking
model as regime_alloc_backtest.py (frozen spec, only EMA span varies). S=16 CSCV
blocks (Bailey-LdP default). NO LIVE TRADING -- this is a research audit.

Usage:
  python scripts/regime_alloc_pbo_grid.py --cost-bps 30 --n-blocks 16 --n-boot 5000
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from scripts.regime_alloc_backtest import build_monthly_returns, COST_BPS_DEFAULT
from scripts.quantum_loop import deflated_sharpe
from scripts.strategy_evaluator import bootstrap_expectancy_ci
from scripts.validation_audit import cscv_pbo, spa_pvalue

SPAN_MIN = 50
SPAN_MAX = 300
SPAN_STEP = 25


def main() -> int:
    ap = argparse.ArgumentParser(description="Dense EMA-span PBO/SPA grid for regime-alloc")
    ap.add_argument("--cost-bps", type=float, default=COST_BPS_DEFAULT)
    ap.add_argument("--n-blocks", type=int, default=16)
    ap.add_argument("--n-boot", type=int, default=5000)
    ap.add_argument("--span-min", type=int, default=SPAN_MIN)
    ap.add_argument("--span-max", type=int, default=SPAN_MAX)
    ap.add_argument("--span-step", type=int, default=SPAN_STEP)
    args = ap.parse_args()

    spans = list(range(args.span_min, args.span_max + 1, args.span_step))
    # Build the monthly return series for each span; align on the common index.
    series_by_span: dict[int, pd.Series] = {}
    for sp in spans:
        port_ret, _, _ = build_monthly_returns(args.cost_bps, sp)
        series_by_span[sp] = port_ret
        print(f"  span {sp:>3}d: {len(port_ret)} months "
              f"({port_ret.index[0].date()}..{port_ret.index[-1].date()})")

    # common (intersection) index -- every span must cover the same months for a
    # valid N x T matrix (CSCV/SPA require trade-ordinal alignment).
    common = None
    for sp in spans:
        idx = series_by_span[sp].index
        common = idx if common is None else common.intersection(idx)
    n_t = len(common)
    print(f"\ncommon aligned months: {n_t}  ({common[0].date()}..{common[-1].date()})  "
          f"across {len(spans)} spans")

    matrix: list[list[float]] = []
    per_span_stats = []
    sharpes: list[float] = []
    for sp in spans:
        r = series_by_span[sp].reindex(common).to_numpy(dtype=float)
        matrix.append(r.tolist())
        mean_r = float(r.mean())
        std = float(r.std(ddof=1))
        sr = mean_r / std if std > 0 else 0.0
        sharpes.append(sr)
        lo, hi = bootstrap_expectancy_ci(r.tolist())
        per_span_stats.append((sp, mean_r, std, sr, lo, hi))

    # cross-trial Sharpe variance -- the REAL deflation input for the dense grid.
    sr_var = float(np.var(sharpes, ddof=1)) if len(sharpes) > 1 else 0.0
    n_trials = len(spans)

    print("\n" + "=" * 96)
    print(f"DENSE SPAN GRID -- {n_trials} spans {spans[0]}..{spans[-1]}d step {args.span_step}, "
          f"cost={args.cost_bps}bps RT, cross-trial Sharpe variance={sr_var:.6f}")
    print("=" * 96)
    print(f"{'span':>5} {'mean/mo':>9} {'std':>8} {'SR_mo':>7} {'CI95lo':>9} {'CI95hi':>9} "
          f"{'DSR_K':>8} {'win':>5}")
    winners = 0
    for sp, mean_r, std, sr, lo, hi in per_span_stats:
        r = series_by_span[sp].reindex(common).to_numpy(dtype=float)
        dsr = deflated_sharpe(r.tolist(), n_trials=n_trials, sr_var_across_trials=sr_var)
        win = (dsr >= 0.95 and lo > 0 and mean_r > 0)
        winners += int(win)
        print(f"{sp:>5}d {mean_r:>9.5f} {std:>8.5f} {sr:>7.4f} {lo:>9.5f} {hi:>9.5f} "
              f"{dsr:>8.4f} {'YES' if win else 'no':>5}")
    print(f"organic winners (DSR>=0.95 + CI95 lo>0 + mean>0, K={n_trials} deflated): {winners}/{n_trials}")

    # CSCV/PBO -- family-level overfit test.
    pbo = cscv_pbo(matrix, n_blocks=args.n_blocks, metric="sharpe")
    print("\n" + "-" * 96)
    print(f"CSCV/PBO (S={args.n_blocks} blocks, metric=sharpe): "
          f"PBO={pbo['pbo']:.4f}  lambda={pbo['lambda']:.4f}  "
          f"combos={pbo['n_combos']}")
    print(f"  OOS rank of best-IS: mean={pbo['oos_rank_mean']:.2f}  "
          f"min={pbo['oos_rank_min']}  max={pbo['oos_rank_max']}  (N={pbo['n_strat']})")
    if pbo["pbo"] is not None:
        if pbo["pbo"] > 0.05:
            print(f"  -> PBO {pbo['pbo']:.4f} > 0.05: the EMA-span FAMILY is overfit; "
                  f"the 100d/250d wins are selection artifacts (NO family-robust edge).")
        else:
            print(f"  -> PBO {pbo['pbo']:.4f} <= 0.05: the family is NOT overfit; "
                  f"the edge is robust to span specification (REAL).")

    # Hansen SPA -- does ANY span beat HOLD=0 with multiplicity control.
    spa = spa_pvalue(matrix, n_boot=args.n_boot)
    print("\n" + "-" * 96)
    print(f"Hansen SPA (stationary bootstrap, n_boot={spa.get('n_boot')}, N={spa.get('n_strat')}): "
          f"p_consistent={spa['p_consistent']:.4f}  best_t={spa.get('best_t', 0):.4f}")
    if spa["p_consistent"] is not None:
        if spa["p_consistent"] < 0.05:
            print(f"  -> SPA p={spa['p_consistent']:.4f} < 0.05: SOME span genuinely beats "
                  f"HOLD after multiplicity correction (real family-level signal).")
        else:
            print(f"  -> SPA p={spa['p_consistent']:.4f} >= 0.05: NO span beats HOLD after "
                  f"multiplicity correction (no family-level signal).")
    print("-" * 96)
    print("Honest bar: a family-robust edge requires PBO<=0.05 AND SPA<0.05. A high PBO or "
          "high SPA p means the post-hoc 100d/250d wins do NOT generalize across the span "
          "specification -- the confirmatory result is NEGATIVE. This is the decisive test "
          "the cycle-9 post-hoc winner needed. NO LIVE TRADING.")
    return 0


if __name__ == "__main__":
    sys.exit(main())