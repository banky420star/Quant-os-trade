"""Cross-sectional equity-index momentum on the Exness CFD basket -- frozen, K=1.

WHY THIS SPEC: the four falsified edge families are (1) directional time-series
trend/momentum (M5/D1 Donchian, TSMOM, N-lever), (2) non-directional gold-silver
cointegration, (3) regime-SWITCHING on a single strategy, (4) cross-asset regime-
ALLOCATION. The one GENUINELY-DIFFERENT family not yet tested on this venue is
CROSS-SECTIONAL momentum: a market-neutral long-short that RANKS assets by past return
and longs the winners / shorts the losers. This is a different object from all four:
  - not directional time-series (no single-asset trend; ranks ACROSS assets)
  - not cointegration (no equilibrium relationship; pure relative momentum)
  - not regime-conditional (no regime classifier)
  - not regime-allocation (long-only rotation; this is dollar-neutral long-short)

The 2024-2026 literature is favorable on EQUITY-INDEX cross-sectional momentum
(Gupta 2025 Sharpe 1.34-1.71, brianbanna 0.70, RegimeSense 0.769 -- all on equity
indices) because equity indices have POSITIVE STRUCTURAL DRIFT (long-biased trend
works) and the cited studies use 5-10bps costs. The honest concerns on this venue:
  * Retail 30bps RT is 3-6x the cited cost. Cross-sectional long-short has TWO legs,
    so cost is roughly 2x a long-only rotation -- the short leg is the cost risk.
  * N=4 indices is SMALL (US500m, US30m, UK100m, FR40m) -- the short leg mean-reverts
    with small N (rahulsp.com cross-sectional study), and rank flips are costly.
  * Dollar-neutral long-short on CFDs requires shorting indices (feasible on Exness).

PRE-REGISTERED, frozen parameters (NOT fit on this data -> counts as 1 DSR trial, K=1):
  * Universe (4 equity indices, deepest aligned D1 history): US500m, US30m, UK100m, FR40m.
    HK50m/JP225m dropped (shorter history, start 2019-07). Metals/FX dropped (different
    regime dynamics; the literature edge is specifically equity-index cross-sectional).
  * Formation signal (causal, price-only): 126-day (6-month) past return, cross-sectional
    rank. Signal computed from data through month-end T-1 -> weights held during month T
    (causal, no look-ahead). One frozen parameter (126d formation). No skip-month (a
    12-1 skip would be a 2nd trial; kept simple and pre-registered).
  * Allocation (frozen): long the top-2 (+0.5 each), short the bottom-2 (-0.5 each),
    dollar-neutral (sum weights = 0). Monthly rebalance. Ties broken by symbol order.
  * Cost: round-trip 30 bps on the CHANGED notional at each monthly rebalance, weight-
    tracking turnover model (one-way rate = 15bps * one-way turnover = 0.5*sum|Δw|).
    A full rank flip (top-2 <-> bottom-2) costs ~30bps one-way; sticky ranks cost little.
  * Evaluation: DSR on monthly portfolio SIMPLE returns (dollar-neutral: gross =
    sum(w_i * r_i), net = gross - rebalance_cost). Walk-forward 3 folds; 126d formation
    is causal so the full series is OOS by construction. Net of retail 30bps. K=1.
    NO LIVE TRADING. A passing DSR is a research finding, not $80->$50k (Peters
    non-ergodicity + Barber-Odean bind at retail account size).

Usage:
  python scripts/xs_momentum_backtest.py --cost-bps 30 --folds 3 --formation 126
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

from core.history_manager import HISTORY_DIR
from scripts.quantum_loop import deflated_sharpe
from scripts.strategy_evaluator import bootstrap_expectancy_ci, make_splits

UNIVERSE = ["US500m", "US30m", "UK100m", "FR40m"]
FORMATION_DEFAULT = 126          # 6-month past return for cross-sectional rank
COST_BPS_DEFAULT = 30.0
TRAIN_RATIO = 0.6
MIN_MONTHS = 36


def build_monthly_returns(cost_bps: float, formation: int) -> tuple[pd.Series, pd.DataFrame]:
    """Construct the monthly cross-sectional long-short portfolio return series.

    Returns (monthly_portfolio_simple_ret, weight_matrix_monthly).
    Causal: rank from data through month-end T-1 -> weights held during month T.
    Dollar-neutral long top-2 (+0.5) / short bottom-2 (-0.5).
    """
    frames = []
    for s in UNIVERSE:
        pq = HISTORY_DIR / f"{s}_D1.parquet"
        if not pq.exists():
            raise FileNotFoundError(f"missing {pq}")
        d = pd.read_parquet(pq)[["time", "close"]].copy()
        d["time"] = pd.to_datetime(d["time"])
        if d["time"].dt.tz is not None:
            d["time"] = d["time"].dt.tz_localize(None)
        d = d.dropna().sort_values("time").drop_duplicates("time", keep="last")
        d = d.rename(columns={"close": s})
        d = d.set_index("time")
        frames.append(d)
    # inner join on the common calendar -> equal coverage, no forward-fill leakage
    px = pd.concat(frames, axis=1, join="inner").dropna()

    # past return signal (formation-period cumulative return), shifted to be causal.
    # mom_signal[t] = px[t] / px[t-formation] - 1, known at day t (uses px through t).
    mom = px.pct_change(formation)            # 126-day past return, NaN during warmup
    # month-end resample: last available daily value per month
    px_m = px.resample("ME").last()
    mom_m = mom.resample("ME").last()
    # signal known at end of month T-1 determines weights held during month T (causal)
    mom_signal = mom_m.shift(1)
    ret_m = px_m.pct_change()                 # simple monthly returns per asset

    assets = list(px_m.columns)
    n_a = len(assets)
    w_prev = None  # drifted weights at end of prior month (None = start flat)
    idxs, rets, w_rows = [], [], []
    for idx in ret_m.index:
        sig_row = mom_signal.loc[idx]                 # Series indexed by asset
        r = ret_m.loc[idx].to_numpy(dtype=float)
        if not np.isfinite(sig_row.to_numpy()).all() or np.any(~np.isfinite(r)):
            w_prev = None
            continue
        sig_vals = np.array([sig_row[a] for a in assets])
        # cross-sectional rank: top-2 long (+0.5), bottom-2 short (-0.5), dollar-neutral
        order = np.argsort(sig_vals)  # ascending: [worst, ..., best]
        w_target = np.zeros(n_a)
        w_target[order[-1]] = 0.5   # best
        w_target[order[-2]] = 0.5   # 2nd best
        w_target[order[0]] = -0.5   # worst
        w_target[order[1]] = -0.5   # 2nd worst
        w_prior = (np.zeros(n_a) if w_prev is None else w_prev)
        # one-way turnover = 0.5*(sum|Δw_assets| + |Δcash|). The TARGET is dollar-neutral
        # (sum w_target = 0) but the DRIFTED w_prior has sum = prior PnL != 0; the cash-leg
        # term sizes the net buy/sell imbalance needed to restore self-financing, so the
        # 0.5*sum|Δw_all| = one-way-notional identity holds exactly (regime_alloc convention).
        # Subtracting the mean to re-center would understate turnover when PnL != 0.
        delta_assets = np.abs(w_target - w_prior)
        delta_cash = abs(float(w_target.sum()) - float(w_prior.sum()))
        turnover = 0.5 * (float(delta_assets.sum()) + delta_cash)
        rebalance_cost = (cost_bps / 2.0 / 10000.0) * turnover   # one-way rate * notional
        gross = float(np.dot(w_target, r))                        # cash leg return = 0
        net = gross - rebalance_cost
        idxs.append(idx); rets.append(net); w_rows.append(w_target.tolist())
        # drift asset weights through month T's returns; the cash leg absorbs the PnL
        # (sum w_target = 0 -> cash weight = 1 - sum(grown) holds the book). Keep the
        # drifted asset weights as w_prior for next month's turnover (NO recentering).
        w_prev = w_target * (1.0 + r)
    port_ret = pd.Series(rets, index=pd.DatetimeIndex(idxs), dtype=float)
    weights = pd.DataFrame(w_rows, index=port_ret.index, columns=assets)
    return port_ret, weights


def main() -> int:
    ap = argparse.ArgumentParser(description="Cross-sectional equity-index momentum OOS backtest (Exness CFDs)")
    ap.add_argument("--cost-bps", type=float, default=COST_BPS_DEFAULT)
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--train-ratio", type=float, default=TRAIN_RATIO)
    ap.add_argument("--formation", type=int, default=FORMATION_DEFAULT)
    ap.add_argument("--min-months", type=int, default=MIN_MONTHS)
    ap.add_argument("--n-trials", type=int, default=1)
    ap.add_argument("--sr-var", type=float, default=0.0)
    args = ap.parse_args()

    port_ret, weights = build_monthly_returns(args.cost_bps, args.formation)
    n = len(port_ret)
    if n < args.min_months:
        print(f"Only {n} months (< {args.min_months}); not enough for a meaningful DSR.")
        return 0
    rs = port_ret.to_numpy()
    mean_r = float(rs.mean())
    std = float(rs.std(ddof=1))
    sr = mean_r / std if std > 0 else 0.0
    lo, hi = bootstrap_expectancy_ci(rs.tolist())
    dsr = deflated_sharpe(rs.tolist(), n_trials=args.n_trials, sr_var_across_trials=args.sr_var)

    # walk-forward folds on the monthly series
    start = port_ret.index[0]
    end = port_ret.index[-1]
    splits = make_splits(start, end, args.train_ratio, args.folds)
    fold_stats = []
    for sp in splits:
        te_s, te_e = sp["test_start"], sp["test_end"]
        fold_rs = port_ret[(port_ret.index >= te_s) & (port_ret.index < te_e)]
        if len(fold_rs) == 0:
            fold_stats.append({"fold": sp["fold"], "months": 0, "mean": 0.0, "ci95": [0.0, 0.0]})
            continue
        fv = fold_rs.to_numpy()
        flo, fhi = bootstrap_expectancy_ci(fv.tolist())
        fold_stats.append({"fold": sp["fold"], "months": len(fold_rs),
                            "mean": round(float(fv.mean()), 5),
                            "ci95": [round(flo, 5), round(fhi, 5)]})
        print(f"  Fold {sp['fold']} test[{te_s.date()}..{te_e.date()}]: "
              f"months={len(fold_rs)} mean={float(fv.mean()):.5f} CI95lo={flo:.5f}")

    folds_positive = sum(1 for f in fold_stats if f["mean"] > 0)
    majority = folds_positive >= (args.folds + 1) // 2
    eligible = n >= args.min_months
    ann_ret = mean_r * 12
    ann_vol = std * math.sqrt(12)
    ann_sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    eq = np.cumsum(rs)
    peak = np.maximum.accumulate(eq)
    max_dd = float((peak - eq).max())
    # average monthly turnover (one-way notional traded)
    wdiff = weights.diff().abs().sum(axis=1).iloc[1:] if len(weights) > 1 else pd.Series([0.0])
    avg_turnover = float(wdiff.mean()) * 0.5 if len(wdiff) else 0.0
    organic_winner = (eligible and dsr >= 0.95 and lo > 0 and majority and mean_r > 0)

    print("\n" + "=" * 92)
    print(f"CROSS-SECTIONAL MOMENTUM (universe={UNIVERSE}, formation={args.formation}d, "
          f"long top-2 / short bottom-2, dollar-neutral, cost={args.cost_bps}bps RT, "
          f"monthly rebalance, K={args.n_trials})")
    print("=" * 92)
    print(f"months={n} ({start.date()}..{end.date()})  avg_one_way_turnover={avg_turnover:.3f}")
    print(f"mean_monthly={mean_r:.5f} std={std:.5f} monthly_Sharpe={sr:.4f}")
    print(f"ann_return={ann_ret:.4f} ann_vol={ann_vol:.4f} ann_Sharpe={ann_sharpe:.4f} maxDD={max_dd:.3f}cum")
    print(f"CI95=[{lo:.5f},{hi:.5f}]  DSR(precise,net,K={args.n_trials})={dsr:.4f}  "
          f"folds_positive={folds_positive}/{args.folds}")
    print("-" * 92)
    print("NOTE: cross-sectional equity-index long-short momentum -- a DIFFERENT edge family")
    print("from directional time-series trend, cointegration, regime-switching, and regime-")
    print("allocation. Market-neutral (dollar-neutral long top-2 / short bottom-2). Monthly")
    print("simple returns are the evaluation unit. Honest limitations: N=4 indices is small")
    print("(short leg mean-reverts with small N); 30bps RT is 3-6x the cited equity-index")
    print("study costs; two-leg long-short roughly doubles cost vs long-only rotation.")
    if not eligible:
        print(f"NOT ELIGIBLE: only {n} months (< {args.min_months}).")
    elif organic_winner:
        print("ORGANIC WINNER: cross-sectional momentum survives DSR>=0.95 + CI95>0 + majority folds.")
    else:
        reasons = []
        if dsr < 0.95: reasons.append(f"DSR {dsr:.4f}<0.95")
        if lo <= 0: reasons.append(f"CI95 lo {lo:.5f}<=0")
        if not majority: reasons.append(f"only {folds_positive}/{args.folds} folds positive")
        if mean_r <= 0: reasons.append(f"mean monthly {mean_r:.5f}<=0")
        print(f"NO ORGANIC WINNER ({'; '.join(reasons)}).")
        if mean_r > 0 and dsr < 0.95:
            print(f"  -> cross-sectional momentum is POSITIVE ({mean_r:.5f}/mo, ann_Sharpe "
                  f"{ann_sharpe:.3f}) but below DSR bar.")
        elif mean_r <= 0:
            print(f"  -> cross-sectional momentum is NEGATIVE ({mean_r:.5f}/mo): the short leg")
            print(f"     mean-reverts / 2x cost eats the relative-momentum edge at retail 30bps.")
    print("-" * 92)
    print("Caveat: a passing DSR is a research finding, not $80->$50k validation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())