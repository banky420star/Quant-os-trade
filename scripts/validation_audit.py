"""CSCV / PBO overfitting audit (Bailey, Lopez de Prado, Zhu 2014; Bailey et al.
2015, "The Probability of Backtest Overfitting").

Reads a quantum_loop DUMP_RSERIES dump (set DUMP_RSERIES=<path> when running
quantum_loop.py) and tests whether the WHOLE grid is overfit, independent of any
single-cell metric (DSR tests one cell at a time; PBO tests the selection
procedure across all cells).

Combinatorially Symmetric Cross-Validation (CSCV):
  - Build an N x T matrix of strategy returns (N = cells, T = trade-ordinal index).
  - Partition T into S even blocks. For each of the C(S, S/2) ways to choose half
    the blocks as In-Sample (IS) and the rest as Out-of-Sample (OOS):
      * rank strategies by IS performance (sum of R over IS blocks);
      * take the best-IS strategy i*;
      * compute i*'s OOS rank (1 = best OOS);
      * overfit event iff i* falls in the BOTTOM half OOS (rank > N/2);
  - PBO = fraction of combinations that are overfit events.
  - lambda = mean over combinations of ln(p / (1-p)), where p is i*'s OOS relative
    rank (the paper's sharper, symmetric metric; >0 implies overfit).

DATA NOTE
  The dump holds per-trade NET R series without timestamps. CSCV is defined on
  time-aligned returns; we align by TRADE ORDINAL (each cell's chronologically
  ordered trades, truncated to the shortest cell). This is a documented
  approximation valid when trade frequency is roughly comparable across cells
  (true here: 40-200 trades/cell). It tests overfit of the SELECTION PROCEDURE,
  which is the question that matters. For a strict time-aligned CSCV, re-run with
  per-bar PnL (TODO if purgedcv adoption lands).

Read-only: prints stats. No live trades, no edge-DB, no deploy.
"""
from __future__ import annotations

import argparse
import json
import math
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _sharpe(rs: list[float]) -> float:
    if len(rs) < 2:
        return 0.0
    m = sum(rs) / len(rs)
    v = sum((x - m) ** 2 for x in rs) / len(rs)
    return m / math.sqrt(v) if v > 0 else 0.0


def spa_pvalue(matrix: list[list[float]], *, n_boot: int = 2000, seed: int = 20260626) -> dict:
    """Hansen (2005) Superior Predictive Ability test, "consistent" p-value.

    Tests whether ANY of the N strategies beats the benchmark (HOLD = 0 R per
    trade). Hand-rolled (no arch dependency) on the trade-ordinal-aligned N x T
    matrix (same alignment caveat as CSCV).

    statistic      t_i = mean(R_i) / se(R_i) ; benchmark R0 = 0.
    best           k* = argmax_i t_i.
    consistent     recentering: mu_i = mean(R_i) if t_i > 0 else 0 (so strategies no
                   better than HOLD are centered to 0 under the null).
    bootstrap      Politis-Romano STATIONARY bootstrap (geometric block length, expected
                   length T**(1/3)) of the time index; for each draw b recompute t_i^b
                   with mu_i subtracted; T^b = max_i t_i^b. Stationary (not iid) so the
                   resample preserves the weak serial dependence of monthly returns.
    p_consistent   = fraction of draws with T^b >= t_k* (observed best).
    < 0.05 -> some strategy is genuinely superior to HOLD (rejects the null).
    """
    import random
    n_strat = len(matrix)
    t = min((len(r) for r in matrix), default=0)
    if n_strat < 1 or t < 2:
        return {"p_consistent": None, "reason": "need >=1 strategy and T>=2"}

    mat = [r[:t] for r in matrix]
    means = [sum(s) / t for s in mat]
    ses = []
    for s in mat:
        m = sum(s) / t
        v = sum((x - m) ** 2 for x in s) / (t - 1) if t > 1 else 0.0
        ses.append(math.sqrt(v / t) if v > 0 else 1e-9)
    t_stats = [means[i] / ses[i] if ses[i] > 0 else 0.0 for i in range(n_strat)]
    # consistent recentering under the null (no positive superiority)
    mu = [means[i] if t_stats[i] > 0 else 0.0 for i in range(n_strat)]
    best_obs = max(t_stats)
    if best_obs <= 0:
        return {"p_consistent": 1.0, "reason": "no strategy beats HOLD in-sample",
                "best_t": best_obs}

    rng = random.Random(seed)
    # Politis-Romano stationary bootstrap: expected block length b = T**(1/3).
    # Each new index either follows the previous (prob 1-1/b, wrapping) or is a fresh
    # uniform draw (prob 1/b), so block lengths are geometric with mean b.
    block_len = max(2.0, t ** (1.0 / 3.0))
    p_restart = 1.0 / block_len
    ge = 0
    for _ in range(n_boot):
        idx = [0] * t
        idx[0] = rng.randrange(t)
        for k in range(1, t):
            if rng.random() < p_restart:
                idx[k] = rng.randrange(t)
            else:
                idx[k] = (idx[k - 1] + 1) % t  # continue block, circular
        boot_max = -1e18
        for i in range(n_strat):
            s = mat[i]
            ms = sum(s[j] for j in idx) / t
            vs = sum((s[j] - ms) ** 2 for j in idx) / (t - 1) if t > 1 else 0.0
            se = math.sqrt(vs / t) if vs > 0 else 1e-9
            ti = (ms - mu[i]) / se if se > 0 else 0.0  # recentered
            if ti > boot_max:
                boot_max = ti
        if boot_max >= best_obs:
            ge += 1
    return {"p_consistent": ge / n_boot, "best_t": best_obs, "n_boot": n_boot,
            "n_strat": n_strat, "t": t}


def cscv_pbo(matrix: list[list[float]], *, n_blocks: int, metric: str = "sharpe") -> dict:
    """CSCV PBO on an N x T return matrix.

    matrix[i] = strategy i's length-T return series (list[float]).
    n_blocks S must be even and >= 2. Returns PBO, lambda, n_combos, distribution
    of best-IS OOS ranks.
    """
    n_strat = len(matrix)
    t = min(len(r) for r in matrix) if matrix else 0
    if n_strat < 2 or t < n_blocks or n_blocks < 2 or n_blocks % 2:
        return {"pbo": None, "lambda": None, "n_combos": 0,
                "reason": "need >=2 strategies and T>=S even"}

    # Truncate every strategy to length t.
    mat = [r[:t] for r in matrix]
    # Distribute the remainder round-robin across the first `remainder` blocks so
    # the trailing (most-recent, OOS-relevant) trades are not silently dropped.
    # Old code used block = t // n_blocks and ignored t - n_blocks*block tail rows.
    remainder = t % n_blocks
    block = t // n_blocks
    # blocks[b] = list of the per-strategy block-b returns
    blocks: list[list[list[float]]] = []
    cursor = 0
    for b in range(n_blocks):
        size = block + (1 if b < remainder else 0)
        start, end = cursor, cursor + size
        blocks.append([s[start:end] for s in mat])
        cursor = end
    assert cursor == t, f"block split lost rows: cursor={cursor} t={t}"
    half = n_blocks // 2

    score = _sharpe if metric == "sharpe" else (lambda rs: sum(rs) / len(rs) if rs else 0.0)

    overfit = 0
    combos = 0
    lambdas: list[float] = []
    oos_ranks: list[int] = []
    for is_idx in combinations(range(n_blocks), half):
        oos_idx = [b for b in range(n_blocks) if b not in set(is_idx)]
        # IS/OOS per-strategy scores
        is_scores, oos_scores = [], []
        for i in range(n_strat):
            is_r = [x for b in is_idx for x in blocks[b][i]]
            oos_r = [x for b in oos_idx for x in blocks[b][i]]
            is_scores.append(score(is_r))
            oos_scores.append(score(oos_r))
        best_is = max(range(n_strat), key=lambda i: is_scores[i])
        # OOS rank of best-IS (1 = best)
        oos_sorted = sorted(range(n_strat), key=lambda i: oos_scores[i], reverse=True)
        rank = oos_sorted.index(best_is) + 1
        oos_ranks.append(rank)
        # overfit iff best-IS lands in the bottom half OOS
        if rank > n_strat / 2:
            overfit += 1
        combos += 1
        # per-combo logit of relative rank p = (rank-1)/(N-1) in (0,1)
        p = (rank - 1) / max(n_strat - 1, 1)
        p = min(max(p, 1e-6), 1 - 1e-6)
        lambdas.append(math.log(p / (1 - p)))

    pbo = overfit / combos if combos else 0.0
    lam = sum(lambdas) / len(lambdas) if lambdas else 0.0
    return {"pbo": pbo, "lambda": lam, "n_combos": combos,
            "n_strat": n_strat, "t": t, "n_blocks": n_blocks,
            "overfit_events": overfit,
            "oos_rank_mean": sum(oos_ranks) / len(oos_ranks) if oos_ranks else 0.0,
            "oos_rank_min": min(oos_ranks) if oos_ranks else 0,
            "oos_rank_max": max(oos_ranks) if oos_ranks else 0}


def main() -> int:
    ap = argparse.ArgumentParser(description="CSCV/PBO overfitting audit on a DUMP_RSERIES dump")
    ap.add_argument("--dump", type=Path, default=ROOT / "deleaked_rseries.json")
    ap.add_argument("--min-trades", type=int, default=40,
                    help="drop cells with fewer than this many trades")
    ap.add_argument("--blocks", type=int, default=10, help="S blocks (even)")
    ap.add_argument("--metric", choices=["sharpe", "mean"], default="sharpe")
    args = ap.parse_args()

    if not args.dump.exists():
        print(f"DUMP file not found: {args.dump}")
        print("Re-run quantum_loop.py with DUMP_RSERIES=<path> set, then pass --dump <path>.")
        return 2
    data = json.loads(args.dump.read_text())
    # Prefer net R series; fall back to gross r_series (older dumps).
    rseries = data.get("r_series_net") or data.get("r_series") or {}
    if not rseries:
        print("No r_series found in dump (expected r_series_net or r_series).")
        return 2
    # keep cells meeting min-trades, truncate to common length
    kept = {k: [float(x) for x in v] for k, v in rseries.items() if len(v) >= args.min_trades}
    if len(kept) < 2:
        print(f"Only {len(kept)} cells with >= {args.min_trades} trades; need >= 2 for CSCV.")
        return 2
    matrix = list(kept.values())
    cells = list(kept.keys())
    print(f"CSCV/PBO audit on {len(cells)} cells (min {args.min_trades} trades), "
          f"metric={args.metric}, blocks={args.blocks}")
    print("Cells:", ", ".join(cells))
    print("-" * 70)
    res = cscv_pbo(matrix, n_blocks=args.blocks, metric=args.metric)
    if res.get("pbo") is None:
        print("CSCV not runnable:", res.get("reason"))
        return 2
    print(f"PBO            = {res['pbo']:.4f}   (fraction of combos where the best-IS "
          f"cell lands in the bottom half OOS)")
    print(f"lambda (logit)  = {res['lambda']:+.4f}  (paper's symmetric metric; >0 => overfit)")
    print(f"combos         = {res['n_combos']}   overfit_events={res['overfit_events']}")
    print(f"best-IS OOS rank: mean={res['oos_rank_mean']:.2f}  "
          f"min={res['oos_rank_min']}  max={res['oos_rank_max']}  (1=best OOS)")
    print("-" * 70)
    if res["pbo"] > 0.5:
        print(f"VERDICT: PBO={res['pbo']:.2f} > 0.5 -> the selection procedure is OVERFIT: "
              f"the best in-sample cell is more often than not in the bottom half "
              f"out-of-sample. No deployable edge.")
    elif res["pbo"] > 0.0:
        print(f"VERDICT: PBO={res['pbo']:.2f} (0..0.5). Some overfit but best-IS carries "
              f"OOS signal {1 - res['pbo']:.0%} of the time; still must survive DSR for deployability.")
    else:
        print("VERDICT: PBO=0 -- best-IS is always top-half OOS (no overfit in selection).")
    print("\nNOTE: trade-ordinal alignment is a documented approximation of CSCV; for a")
    print("strict time-aligned CSCV re-run with per-bar PnL (purgedcv adoption TODO).")
    print("=" * 70)
    # ---- Hansen SPA: does ANY cell beat HOLD after multiple-testing? ----
    spa = spa_pvalue(matrix, n_boot=2000)
    print(f"Hansen SPA (consistent p-value, benchmark=HOLD, 2000 bootstrap draws):")
    if spa.get("p_consistent") is None:
        print("  SPA not runnable:", spa.get("reason"))
    else:
        p = spa["p_consistent"]
        print(f"  p_consistent = {p:.4f}   (best observed t = {spa['best_t']:+.3f})")
        if p < 0.05:
            print(f"  VERDICT: p={p:.3f} < 0.05 -> some cell is genuinely superior to HOLD "
                  f"after multiple-testing. Surprising; cross-check the cell vs DSR.")
        else:
            print(f"  VERDICT: p={p:.3f} >= 0.05 -> NO cell beats HOLD after Hansen SPA "
                  f"multiple-testing. Confirms the 0-organic-winner DSR verdict from a "
                  f"second, more-powerful test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())