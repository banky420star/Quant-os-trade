"""Monthly regime-allocation across the Exness CFD basket -- frozen, K=1.

WHY THIS SPEC: the prior falsified levers were all DIRECTIONAL (M5/D1 Donchian/D1
TSMOM/N-lever) or NON-DIRECTIONAL stat-arb (cointegration). The one regime angle
the cited 2024-2026 literature splits out as SEPARATE -- and that this project had
NOT yet tested -- is top-down regime ALLOCATION: low-turnover MONTHLY rotation
across ASSET CLASSES conditional on a regime. This is a genuinely different edge
family from regime-SWITCHING on a single directional strategy (which [[ui-verified-
regime-event-deadends-2026-06-28]] falsified: high-turnover, K-inflating, cost-broken).

The strongest cited support:
  * QUANTT Macro Regime paper (quantt.ca, walk-forward 1963-2025, 765 months,
    block-bootstrap p=0.013): monthly multi-asset rotation, walk-forward OOS Sharpe
    0.760, break-even ~30bps one-way. ANOVA: alpha is CROSS-ASSET (equities vs bonds
    vs gold), NOT within-class sector rotation (p>0.49).
  * Arithmax comparison paper: after real costs a 60/40 (annual, ~0.3% cost) BEATS an
    AI monthly-regime strategy (2-3% cost); simplicity beats complexity; the AI
    strategy likely nets -0.25% to -0.40% CAGR. -> cost is the decisive factor, and
    retail 30bps sits right at QUANTT's break-even.
  * FR-LUX (arXiv 2510.02986): proportional costs create an inaction band; regime
    conditioning only adds value when costs are internalized. Low-turnover survives;
    high-turnover breaks beyond 10-25bps.
  * Dhuria/macro_regime: WEEKLY regime timing is negative (crisis regime captures
    both crash AND recovery weeks -> sells low / buys high); MONTHLY > weekly.

Honest prior before running: ~10-15% P(deployable DSR>=0.95 net of retail 30bps),
because (a) QUANTT's Sharpe 0.760 is already well below the 0.95 DSR deploy bar even
before CFD cost inflation, (b) retail 30bps sits AT QUANTT's ~30bps break-even, (c)
Exness serves NO bond CFD -- bonds are QUANTT's true defensive asset, so the
regime-OFF leg can only go to cash (a weaker defensive than bonds), and (d) adding
this as a new trial inflates the project's cumulative K and deflates the bar. Worth
running for completeness: a pass would be a real cross-asset edge; a fail is the
honest final negative across all four edge families (directional / non-directional
stat-arb / regime-switching / regime-allocation).

PRE-REGISTERED, frozen parameters (NOT fit on this data -> counts as 1 DSR trial, K=1):
  * Universe (6 assets, 3 classes, deepest aligned history): equities {US500m, US30m,
    UK100m, FR40m}, gold {XAUUSDm}, energy {USOILm}. Metals XAG/XPT/XPD dropped
    (correlated with gold -> would double-count the metals bet; QUANTT says
    within-class rotation has no alpha). HK50/JP225 dropped (shorter history).
    AUDUSD dropped (FX, different regime dynamics).
  * Regime signal (retail-feasible, causal -- no macro feed on MT5): 200-day EMA of
    the equal-weight cross-asset basket index. regime = RISK_ON if basket > EMA200
    else RISK_OFF. This is the classic Faber/Asness 200-day trend regime filter
    (the standard causal price-based analog of QUANTT's macro growth x inflation
    classifier, which needs data MT5 does not serve). One frozen parameter (200d).
  * Allocation (frozen):
      RISK_ON  -> equal-weight 6 assets (full risk).
      RISK_OFF -> 100% cash (the ONLY true defensive asset on this venue; Exness has
        no bond CFD, and gold is only conditionally defensive -- honest limitation).
    Monthly rebalance. No vol-targeting / leverage overlay (would be a 2nd trial; the
    QUANTT vol-target push is a separate lever and is NOT part of this K=1 spec).
  * Cost: round-trip 30 bps on the CHANGED notional at each monthly rebalance (only
    the legs that actually flip get charged -- sticky regimes => low turnover =>
    low cost, which is the whole point of the low-frequency design).
  * Evaluation: DSR on monthly portfolio SIMPLE returns (the classic Sharpe basis;
    same as TSMOM's pct_change grid so directly comparable -- NOT per-trade R).
    Walk-forward 3 folds; 200-day EMA is causal so the full series is OOS by
    construction. Net of retail 30 bps.
    NO LIVE TRADING. A passing DSR is a research finding, not $80->$50k (Peters
    non-ergodicity + Barber-Odean bind at retail account size).

Usage:
  python scripts/regime_alloc_backtest.py --cost-bps 30 --folds 3 --ema 200
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

UNIVERSE = ["US500m", "US30m", "UK100m", "FR40m", "XAUUSDm", "USOILm"]
EMA_REGIME = 200          # 200-day EMA of the equal-weight basket -> regime
COST_BPS_DEFAULT = 30.0
TRAIN_RATIO = 0.6
MIN_MONTHS = 36            # need enough months for a meaningful monthly-return DSR


def _ema(series: pd.Series, span: int) -> pd.Series:
    # adjust=False is the standard recursive EMA (matches the Faber/Asness filter).
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def build_monthly_returns(cost_bps: float, ema_span: int) -> tuple[pd.Series, pd.DataFrame, pd.Series]:
    """Construct the monthly regime-allocated portfolio return series.

    Returns (monthly_portfolio_simple_ret, regime_label_at_monthend, basket_index).
    Causal construction: signal computed from data through month-end T is acted on
    for the hold period [T, T+1], so the forward month return T->T+1 is OOS.
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
    # equal-weight basket index (each asset rebased to 1 at the common start)
    norm = px.div(px.iloc[0])
    basket = norm.mean(axis=1)
    ema = _ema(basket, ema_span)
    # During EMA warmup there is no regime signal. `basket > NaN == False` would
    # silently emit 0 (RISK_OFF/cash) and record degenerate 0-return months for
    # spans whose EMA is not yet valid (e.g. span=300 until ~2020-04). Mask those
    # days to NaN so the monthly loop SKIPS them (no fake cash months) and each
    # span's series starts at its first post-warmup month -- the honest construction.
    regime_raw = (basket > ema).astype("Int64").astype(float).where(ema.notna())

    # month-end resample: last available daily close per month
    px_m = px.resample("ME").last()
    regime_m = regime_raw.resample("ME").last()

    # Simple monthly returns per asset (return of month T = close_T/close_{T-1} - 1).
    # Simple (not log) so weight drift + turnover arithmetic is exact.
    ret_m = px_m.pct_change()
    # Signal known at end of month T-1 (regime_m.shift(1)) determines the weights
    # HELD during month T -> earns month T's return. Causal (no look-ahead).
    regime_signal = regime_m.shift(1)

    # Walk month by month, tracking drifted weights and charging the TRUE one-way
    # turnover cost at the project-consistent rate (cost_bps = full ROUND-TRIP, so the
    # one-way rate is cost_bps/2). This unifies regime-flip cost (a flip moves ~100% of
    # the basket one-way -> 15bps) with within-RISK_ON equal-weight drift cost (weights
    # drift with relative returns, rebalanced back to 1/6 each month -> a few bps/yr).
    # one-way turnover = 0.5 * sum|Δw_all| where the cash leg is the residual that makes
    # weights sum to 1: Δw_cash = -(sum(w_target) - sum(w_prior)).
    assets = list(px_m.columns)
    n_a = len(assets)
    w_prev = None  # drifted asset weights at end of prior month (None = start from cash)
    idxs, rets, sigs = [], [], []
    for idx in ret_m.index:
        sig = regime_signal.get(idx, np.nan)
        r = ret_m.loc[idx].to_numpy(dtype=float)
        if not np.isfinite(sig) or np.any(~np.isfinite(r)):
            w_prev = None
            continue
        w_target = (np.full(n_a, 1.0 / n_a) if sig == 1 else np.zeros(n_a))
        w_prior = (np.zeros(n_a) if w_prev is None else w_prev)
        delta_assets = np.abs(w_target - w_prior)
        delta_cash = abs(float(w_target.sum()) - float(w_prior.sum()))
        turnover = 0.5 * (float(delta_assets.sum()) + delta_cash)  # one-way notional traded
        rebalance_cost = (cost_bps / 2.0 / 10000.0) * turnover      # one-way rate * notional
        gross = float(np.dot(w_target, r))                         # cash leg return = 0
        net = gross - rebalance_cost
        idxs.append(idx); rets.append(net); sigs.append(int(sig))
        # drift weights through month T's returns to end-of-month (cash return = 0)
        grown = w_target * (1.0 + r)
        V = float(grown.sum()) + (1.0 - float(w_target.sum()))     # + cash (weight = 1-sum)
        w_prev = (grown / V if V != 0 else w_target)
    port_ret = pd.Series(rets, index=pd.DatetimeIndex(idxs), dtype=float)
    regime_signal = pd.Series(sigs, index=port_ret.index, dtype=int)
    regime_signal = regime_signal.dropna().reindex(port_ret.index)
    return port_ret, regime_signal, basket


def main() -> int:
    ap = argparse.ArgumentParser(description="Monthly regime-allocation OOS backtest (Exness CFD basket)")
    ap.add_argument("--cost-bps", type=float, default=COST_BPS_DEFAULT)
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--train-ratio", type=float, default=TRAIN_RATIO)
    ap.add_argument("--ema", type=int, default=EMA_REGIME)
    ap.add_argument("--min-months", type=int, default=MIN_MONTHS)
    ap.add_argument("--n-trials", type=int, default=1)
    ap.add_argument("--sr-var", type=float, default=0.0,
                    help="cross-trial Sharpe variance for DSR deflation (0 = single "
                         "trial; for a K-trial robustness sweep pass the variance of the "
                         "per-trial monthly Sharpes so n_trials has a real effect)")
    args = ap.parse_args()

    port_ret, regime_signal, basket = build_monthly_returns(args.cost_bps, args.ema)
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
    # annualized: mean_monthly*12, std*sqrt(12)
    ann_ret = mean_r * 12
    ann_vol = std * math.sqrt(12)
    ann_sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    # equity curve / maxDD on cumulative simple returns (small-return cumsum approx)
    eq = np.cumsum(rs)
    peak = np.maximum.accumulate(eq)
    max_dd = float((peak - eq).max())
    on_months = int((regime_signal == 1).sum())
    organic_winner = (eligible and dsr >= 0.95 and lo > 0 and majority and mean_r > 0)

    print("\n" + "=" * 92)
    print(f"REGIME ALLOCATION (universe={UNIVERSE}, EMA{args.ema} basket regime, "
          f"cost={args.cost_bps} bps RT on changed notional, monthly rebalance, K={args.n_trials})")
    print("=" * 92)
    print(f"months={n} ({start.date()}..{end.date()})  risk_on_months={on_months}/{n} "
          f"({round(100*on_months/n,1)}%)")
    print(f"mean_monthly={mean_r:.5f} std={std:.5f} monthly_Sharpe={sr:.4f}")
    print(f"ann_return={ann_ret:.4f} ann_vol={ann_vol:.4f} ann_Sharpe={ann_sharpe:.4f} maxDD={max_dd:.3f}cum")
    print(f"CI95=[{lo:.5f},{hi:.5f}]  DSR(precise,net,K={args.n_trials})={dsr:.4f}  "
          f"folds_positive={folds_positive}/{args.folds}")
    print("-" * 92)
    print("NOTE: top-down cross-asset regime ALLOCATION -- a DIFFERENT edge family from the")
    print("directional trend specs and from regime-SWITCHING on a single strategy. Monthly")
    print("simple monthly returns are the evaluation unit (QUANTT/Sharpe grid), NOT per-trade R.")
    print("Honest limitations: no bond CFD on Exness -> regime-OFF leg is cash (weaker than")
    print("bonds, QUANTT's true defensive asset); 200d-EMA price regime is the causal retail")
    print("analog of QUANTT's macro growth x inflation classifier (macro data not on MT5).")
    if not eligible:
        print(f"NOT ELIGIBLE: only {n} months (< {args.min_months}).")
    elif organic_winner:
        print("ORGANIC WINNER: regime-allocation survives DSR>=0.95 + CI95>0 + majority folds.")
    else:
        reasons = []
        if dsr < 0.95: reasons.append(f"DSR {dsr:.4f}<0.95")
        if lo <= 0: reasons.append(f"CI95 lo {lo:.5f}<=0")
        if not majority: reasons.append(f"only {folds_positive}/{args.folds} folds positive")
        if mean_r <= 0: reasons.append(f"mean monthly {mean_r:.5f}<=0")
        print(f"NO ORGANIC WINNER ({'; '.join(reasons)}).")
        if mean_r > 0 and dsr < 0.95:
            print(f"  -> cross-asset rotation is POSITIVE ({mean_r:.5f}/mo, ann_Sharpe "
                  f"{ann_sharpe:.3f}) but below DSR bar.")
    print("-" * 92)
    print("Caveat: a passing DSR is a research finding, not $80->$50k validation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())