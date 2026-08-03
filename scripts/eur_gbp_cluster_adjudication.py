"""EUR/GBP London+NY continuation cluster — DSR/SPA + OOS walk-forward adjudication.

Pre-registered in state/eur_gbp_cluster_preregistration.json BEFORE this script
was run (2026-08-03). Reuses existing DSR/SPA/fold math:
  - scripts.quantum_loop.deflated_sharpe  (Bailey-LdP False Strategy Theorem)
  - scripts.validation_audit.spa_pvalue   (Hansen SPA, Politis-Romano bootstrap)
  - scripts.strategy_evaluator.make_splits, bootstrap_expectancy_ci

Read-only on history parquets; no live trading path touched.
"""
from __future__ import annotations
import sys, json
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
from core.utils import load_config
from core.specialized_replay_labeler import replay_symbol
from scripts.quantum_loop import deflated_sharpe
from scripts.validation_audit import spa_pvalue
from scripts.strategy_evaluator import make_splits, bootstrap_expectancy_ci

# ---- pre-registered cluster (must match state/eur_gbp_cluster_preregistration.json) ----
CLUSTER_SYMBOLS = ["EURUSDm", "GBPUSDm"]
CLUSTER_SETUPS = {
    "london_adx_trend", "ny_adx_trend",
    "london_atr_pct_breakout", "ny_atr_pct_breakout",
    "london_cmf_continuation", "ny_cmf_continuation",
    "london_adx_cmf", "ny_adx_cmf",
    "london_adx_atr_pct", "ny_adx_atr_pct",
    "london_atr_pct_cmf", "ny_atr_pct_cmf",
    "london_triple_confirm", "ny_triple_confirm",
}
COST_R = 0.15           # pre-registered round-trip retail cost in R
K_PRIMARY = 742        # honest full-search deflation (distinct_cells searched)
K_SECONDARY = 28       # cluster size (transparency only, NOT honest bar)
K_TERTIARY = 7         # distinct entry models (most-generous upper bound)
SR_VAR_FIXED = 0.35    # prior-work typical, for sensitivity
FOLDS = 3
TRAIN_RATIO = 0.6
MIN_N_CELL = 20        # min trades for a cell to enter SPA / DSR-per-cell


def _sharpe(rs):
    if len(rs) < 2:
        return 0.0
    m = sum(rs) / len(rs)
    var = sum((r - m) ** 2 for r in rs) / len(rs)  # population
    if var <= 1e-12:
        return 0.0
    return m / (var ** 0.5)


def _pop_var(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / len(xs)


def _dsr_suite(rs_net, sr_var):
    """DSR at the three pre-registered K levels + a fixed-sr_var sensitivity."""
    out = {}
    for label, K in (("K742_primary", K_PRIMARY), ("K28_secondary", K_SECONDARY), ("K7_tertiary", K_TERTIARY)):
        out[label] = round(deflated_sharpe(rs_net, n_trials=K, sr_var_across_trials=sr_var), 4)
    return out


def main():
    cfg = load_config()
    print("=" * 78)
    print("EUR/GBP L+NY continuation cluster — DSR/SPA + OOS adjudication")
    print("Pre-registered 2026-08-03 (state/eur_gbp_cluster_preregistration.json)")
    print("=" * 78)

    # 1. Re-run replay_symbol for the two cluster symbols, collect per-fire rows.
    all_rows = []
    common_starts, common_ends = [], []
    # max_bars large enough to label the FULL available M5 history (~50k bars
    # per symbol) so the walk-forward folds (spanning the whole parquet
    # calendar) actually contain fires. max_bars=10000 only labeled the last
    # ~34 days, which all landed after every fold's test window -> empty folds.
    for sym in CLUSTER_SYMBOLS:
        rows = replay_symbol(sym, cfg, max_bars=60000)
        all_rows.extend(rows)
        # common calendar from the parquet (for make_splits)
        df = pd.read_parquet(ROOT / "data" / "history" / f"{sym}_M5.parquet")
        ts = pd.to_datetime(df.iloc[:, 0])
        common_starts.append(ts.min()); common_ends.append(ts.max())
        print(f"  {sym}: {len(rows)} fires collected")
    cs = max(common_starts); ce = min(common_ends)
    print(f"  common M5 window: {cs} .. {ce}")

    # 2. Filter to cluster; net-of-cost R.
    cluster_rows = [r for r in all_rows if r["setup_type"] in CLUSTER_SETUPS]
    for r in cluster_rows:
        r["net_r"] = r["r_multiple"] - COST_R
    print(f"  cluster fires: {len(cluster_rows)}")

    # 3. Per-cell (symbol, setup_type) groupings.
    cells = defaultdict(list)
    for r in cluster_rows:
        cells[(r["symbol"], r["setup_type"])].append(r["net_r"])
    cell_stats = []
    for (sym, st), rs in sorted(cells.items()):
        gross = [r + COST_R for r in rs]
        cell_stats.append({
            "cell": f"{sym}|{st}", "symbol": sym, "setup": st,
            "n": len(rs),
            "exp_net": round(sum(rs) / len(rs), 4),
            "exp_gross": round(sum(gross) / len(rs), 4),
            "sharpe_net": round(_sharpe(rs), 4),
        })
    cell_stats.sort(key=lambda c: c["exp_net"], reverse=True)

    # 4. sr_var_across_trials (cluster-local proxy, pre-registered method).
    eligible = [c for c in cell_stats if c["n"] >= MIN_N_CELL]
    sr_var_cluster = round(_pop_var([c["sharpe_net"] for c in eligible]), 4) if eligible else 0.0
    print(f"  cluster cells: {len(cell_stats)} ({len(eligible)} eligible n>={MIN_N_CELL})")
    print(f"  sr_var_cluster (proxy) = {sr_var_cluster}  | sr_var_fixed = {SR_VAR_FIXED}")

    # 5. Pooled cluster DSR (all cluster rows net).
    pooled_net = [r["net_r"] for r in cluster_rows]
    pooled_gross = [r["r_multiple"] for r in cluster_rows]
    print("\n--- POOLED CLUSTER DSR (all cluster fires, net of cost) ---")
    print(f"  pooled n = {len(pooled_net)}, exp_net = {round(sum(pooled_net)/len(pooled_net),4)}, "
          f"exp_gross = {round(sum(pooled_gross)/len(pooled_gross),4)}, sharpe_net = {round(_sharpe(pooled_net),4)}")
    for sv_label, sv in (("sr_var_cluster", sr_var_cluster), ("sr_var_fixed_0.35", SR_VAR_FIXED)):
        suite = _dsr_suite(pooled_net, sv if sv > 0 else 0.001)
        print(f"  DSR [{sv_label}]: K742={suite['K742_primary']}  K28={suite['K28_secondary']}  K7={suite['K7_tertiary']}")
    lo, hi = bootstrap_expectancy_ci(pooled_net, n_boot=2000)
    print(f"  pooled net CI95 = [{round(lo,4)}, {round(hi,4)}]  (lo>0 = naive pass)")

    # 6. Per-cell DSR at primary K (honest), top cells.
    print("\n--- PER-CELL DSR (K=742 primary, sr_var_cluster) top 8 by exp_net ---")
    print(f"  {'cell':28} {'n':>5} {'expN':>7} {'wr%':>5} {'sr':>5} {'DSR742':>7} {'DSR28':>7} {'DSR7':>6}")
    for c in eligible[:8]:
        rs = cells[(c["symbol"], c["setup"])]
        wr = round(100 * sum(1 for r in rs if r > 0) / len(rs), 1)
        s742 = round(deflated_sharpe(rs, n_trials=K_PRIMARY, sr_var_across_trials=sr_var_cluster), 4)
        s28 = round(deflated_sharpe(rs, n_trials=K_SECONDARY, sr_var_across_trials=sr_var_cluster), 4)
        s7 = round(deflated_sharpe(rs, n_trials=K_TERTIARY, sr_var_across_trials=sr_var_cluster), 4)
        print(f"  {c['cell']:28} {c['n']:>5} {c['exp_net']:>+7.3f} {wr:>5.1f} {c['sharpe_net']:>5.2f} {s742:>7.3f} {s28:>7.3f} {s7:>6.3f}")

    # 7. Walk-forward OOS: 3 expanding-window folds on the common calendar.
    print(f"\n--- WALK-FORWARD OOS ({FOLDS} folds, train_ratio={TRAIN_RATIO}) ---")
    splits = make_splits(cs, ce, TRAIN_RATIO, FOLDS)
    fold_results = []
    for sp in splits:
        ts0 = pd.Timestamp(sp["test_start"]); ts1 = pd.Timestamp(sp["test_end"])
        fold_rows = [r for r in cluster_rows
                     if ts0 <= pd.Timestamp(r["fired_at_bar"]) < ts1]
        if not fold_rows:
            fold_results.append({"fold": sp["fold"], "n": 0}); continue
        fr_net = [r["net_r"] for r in fold_rows]
        d742 = round(deflated_sharpe(fr_net, n_trials=K_PRIMARY, sr_var_across_trials=sr_var_cluster), 4)
        lo, hi = bootstrap_expectancy_ci(fr_net, n_boot=2000)
        wr = round(100 * sum(1 for r in fr_net if r > 0) / len(fr_net), 1)
        expn = round(sum(fr_net) / len(fr_net), 4)
        fold_results.append({
            "fold": sp["fold"], "n": len(fr_net), "exp_net": expn, "wr": wr,
            "ci95_lo": round(lo, 4), "ci95_hi": round(hi, 4), "DSR742": d742,
        })
        print(f"  fold {sp['fold']}: test {ts0}..{ts1}  n={len(fr_net):>4}  exp_net={expn:+.3f}  wr={wr:.1f}%  "
              f"CI95=[{round(lo,4)},{round(hi,4)}]  DSR742={d742}")
    folds_pos = sum(1 for f in fold_results if f.get("exp_net", 0) > 0)
    folds_dsr = sum(1 for f in fold_results if f.get("DSR742", 0) >= 0.95)
    folds_ci = sum(1 for f in fold_results if f.get("ci95_lo", -1) > 0)
    print(f"  fold consistency: {folds_pos}/{FOLDS} positive exp, {folds_ci}/{FOLDS} CI lo>0, {folds_dsr}/{FOLDS} DSR742>=0.95")

    # 8. Hansen SPA on the matrix of eligible cells' net R series.
    print("\n--- HANSEN SPA (benchmark = HOLD/0R, consistent p) ---")
    matrix = [cells[(c["symbol"], c["setup"])] for c in eligible]
    if len(matrix) >= 2:
        spa = spa_pvalue(matrix, n_boot=2000)
        print(f"  cells in matrix: {len(matrix)}  | SPA consistent p = {round(spa.get('p_consistent', spa.get('p', 'n/a')), 4)}")
        print(f"  (p < 0.05 = some cluster cell genuinely beats HOLD after SPA recentering)")
    else:
        spa = {}
        print(f"  insufficient eligible cells ({len(matrix)}) for SPA")

    # 9. Verdict per pre-registered decision rule.
    print("\n" + "=" * 78)
    print("VERDICT (pre-registered decision rule)")
    print("=" * 78)
    pooled_dsr742 = deflated_sharpe(pooled_net, n_trials=K_PRIMARY, sr_var_across_trials=sr_var_cluster)
    spa_p = spa.get("p_consistent", spa.get("p", 1.0)) if spa else 1.0
    pooled_ci_lo = bootstrap_expectancy_ci(pooled_net, n_boot=2000)[0]
    checks = {
        "DSR742>=0.95 (pooled)": pooled_dsr742 >= 0.95,
        "SPA p<0.05": spa_p < 0.05,
        ">=2/3 folds DSR742>=0.95": folds_dsr >= 2,
        "pooled CI95 lo>0": pooled_ci_lo > 0,
    }
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    deployable = all(checks.values())
    print(f"\n  DEPLOYABLE EDGE: {'YES' if deployable else 'NO'}")
    print(f"  (primary honest bar K=742; see pre-registration for K-secondary/tertiary transparency)")

    # 10. Persist result.
    out = {
        "pooled": {"n": len(pooled_net), "exp_net": round(sum(pooled_net)/len(pooled_net),4),
                   "exp_gross": round(sum(pooled_gross)/len(pooled_gross),4),
                   "sharpe_net": round(_sharpe(pooled_net),4),
                   "DSR742": round(pooled_dsr742,4),
                   "DSR28": round(deflated_sharpe(pooled_net,n_trials=K_SECONDARY,sr_var_across_trials=sr_var_cluster),4),
                   "DSR7": round(deflated_sharpe(pooled_net,n_trials=K_TERTIARY,sr_var_across_trials=sr_var_cluster),4),
                   "ci95_lo": round(pooled_ci_lo,4)},
        "sr_var_cluster": sr_var_cluster, "cost_r": COST_R,
        "folds": fold_results, "spa_p": round(spa_p,4) if isinstance(spa_p,(int,float)) else None,
        "checks": checks, "deployable_edge": deployable,
        "cell_count": len(cell_stats), "eligible_cells": len(eligible),
        "top_cells": cell_stats[:10],
    }
    outp = ROOT / "state" / "eur_gbp_cluster_adjudication.json"
    with open(outp, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\n  result persisted: {outp}")


if __name__ == "__main__":
    main()