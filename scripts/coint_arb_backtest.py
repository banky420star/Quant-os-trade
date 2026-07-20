"""Gold-silver cointegration stat-arb -- Mittal & Mittal (2025) spec, frozen.

WHY THIS SPEC: the D1 trend direction is exhausted ([[nlever-tsmom-result-2026-06-27]],
[[cycle4-bugfixes-2026-06-28]]). The one residual cheap, unblocked, NON-DIRECTIONAL
lever surfaced by the cited 2024-2026 research is a gold-silver cointegration stat-arb
(Mittal & Mittal 2025, OOS Sharpe 0.71 / Sortino 0.92 on a cointegrated GC/SI pair with
a Kalman hedge ratio + regime filter). This is market-neutral -- it does NOT depend on
a directional trend -- so it is a genuinely different edge family from everything else
tested in this project (all directional). Honest prior: 5-10% P(deployable DSR>=0.95
net of retail 30bps), because (a) the published OOS Sharpe 0.71 is already below the 0.95
DSR bar before CFD cost inflation, and (b) silver CFD spreads are wider than gold's, so
the net Sharpe likely falls to ~0.4-0.5. Worth running for completeness: a pass would be
a real non-directional edge; a fail is another honest negative.

PRE-REGISTERED, frozen parameters (NOT fit on this data -> counts as 1 DSR trial, K=1):
  * Hedge ratio: rolling 60-day OLS of XAU_close on XAG_close (causal: fit on the
    trailing 60 bars only). (The Mittal spec uses a Kalman-filter hedge ratio; rolling
    OLS is the standard causal estimator and is the frozen choice here -- swapping in
    a Kalman hedge would be a second trial and inflate K.)
  * Spread: s_t = XAU_close - (beta_t * XAG_close + alpha_t), with alpha,beta from the
    trailing-60 OLS. sigma_s_t = rolling-60 std of the residual (s - mean).
  * Z-score: z_t = (s_t - mean_t) / sigma_s_t  (= the OLS residual standardized).
  * Entry: |z_t| > 2.0  ->  spread is >2 sigma from mean -> mean-revert.
      z > +2: short XAU, long beta*XAG (dollar-neutral: $1 XAU short, $beta XAG long).
      z < -2: long XAU, short beta*XAG.
  * Exit: |z_t| < 0.5  (reverted toward mean). No intrabar stop (monthly... daily
    close-to-close exit -- the conservative choice; an intrabar stop would be a
    second trial). Forced exit at end of data.
  * Cost: round-trip 30 bps on BOTH legs' notional (entry+exit). Per $1 XAU notional:
    cost = (bps/1e4) * (1 + beta)  [XAU leg notional $1 + XAG leg notional $beta].
  * R-multiple: net_dollar_return_per_$1_capital / |z_entry|. DSR is scale-invariant on
    R-multiples (sr=mean/std), so the normalization only needs to be consistent across
    trades. |z_entry| is the natural risk unit (a z=2 entry risks 2 sigma of spread).

EVALUATION GRID: DSR on per-trade R-multiples (same grid as the Donchian spec, so
directly comparable -- unlike TSMOM's monthly grid). Walk-forward 3 folds for
robustness; rolling-OLS is causal so the full series is OOS by construction. Net of
retail 30 bps. NO LIVE TRADING. Even a passing DSR is a research finding, not $80->$50k
(Peters non-ergodicity + Barber-Odean bind at retail account size).

Usage:
  python scripts/coint_arb_backtest.py --xau XAUUSDm --xag XAGUSDm --cost-bps 30 --folds 3
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

HEDGE_WINDOW = 60       # trailing OLS window for beta/alpha + residual std
Z_ENTRY = 2.0           # |z| > 2 -> enter (mean-revert)
Z_EXIT = 0.5            # |z| < 0.5 -> exit (reverted)
COST_BPS_DEFAULT = 30.0
MIN_TRADES = 40


def _rolling_hedge(xau: pd.Series, xag: pd.Series, window: int) -> pd.DataFrame:
    """Trailing-window OLS: xau = alpha + beta*xag + eps, causal (trailing only).

    Returns a frame with beta, alpha, resid, sigma (rolling std of resid over the
    same trailing window). At index t the fit uses bars [t-window+1 .. t] (inclusive)
    -- i.e. ONLY past and current bars, never future. The residual at t is computed
    with the beta/alpha fit at t (in-sample residual of the trailing fit), which is
    the standard causal construction for a rolling-hedge stat-arb.
    """
    beta = np.full(len(xau), np.nan)
    alpha = np.full(len(xau), np.nan)
    resid = np.full(len(xau), np.nan)
    xv = xau.to_numpy(dtype=float)
    yv = xag.to_numpy(dtype=float)
    for t in range(window - 1, len(xau)):
        xs = yv[t - window + 1 : t + 1]
        ys = xv[t - window + 1 : t + 1]
        # OLS: ys = alpha + beta*xs
        xm = xs.mean()
        ym = ys.mean()
        denom = ((xs - xm) ** 2).sum()
        b = ((xs - xm) * (ys - ym)).sum() / denom if denom > 0 else np.nan
        a = ym - b * xm
        beta[t] = b
        alpha[t] = a
        resid[t] = ys[-1] - (a + b * xs[-1])  # residual at t using fit-at-t
    resid_sr = pd.Series(resid)
    sigma = resid_sr.rolling(window).std().to_numpy()
    # z-score: (resid - rolling_mean_resid) / sigma. rolling mean of resid over window.
    mean_r = resid_sr.rolling(window).mean().to_numpy()
    z = (resid - mean_r) / sigma
    return pd.DataFrame({"beta": beta, "alpha": alpha, "resid": resid,
                         "sigma": sigma, "z": z}, index=xau.index)


def run_strategy(df: pd.DataFrame, cost_bps: float) -> list[dict]:
    """Walk the aligned D1 frame; emit one trade per entry->exit cycle.

    PRICE-SPACE construction (dollar-neutral by construction):
      spread  s_t = XAU - (alpha + beta*XAG)   [the OLS residual, in dollars]
      A 1-unit spread position = short 1 XAU contract + long `beta` XAG contracts.
      Since beta is the price-slope (dXAU/dXAG in $/contract), the XAG leg notional
      beta*XAG ~= XAU (because alpha = mean(XAU) - beta*mean(XAG) ~= 0 over the
      window), so the two legs are ~equal-dollar -> market-neutral.

      side=+1 (z < -Z_ENTRY, spread cheap): LONG spread = long XAU, short beta*XAG.
        PnL_$ = +(XAU_exit - XAU_entry) - beta*(XAG_exit - XAG_entry)
      side=-1 (z > +Z_ENTRY, spread rich): SHORT spread = short XAU, long beta*XAG.
        PnL_$ = -(XAU_exit - XAU_entry) + beta*(XAG_exit - XAG_entry)

      Round-trip cost charged ONCE on the entry notional (project convention: cost_bps
      is the full round-trip rate, applied once -- NOT entry+exit which would double it):
      (bps/1e4)*(XAU_entry + |beta|*XAG_entry).
      R = net_$ / (XAU_entry * |z_entry|)  -> net dollar return per $XAU notional,
        per unit of z-edge risk. DSR is scale-invariant on R, so the normalization
        only needs to be consistent across trades.
    """
    xau = df["xau"].astype(float)
    xag = df["xag"].astype(float)
    hd = _rolling_hedge(xau, xag, HEDGE_WINDOW)
    df = df.copy()
    df["beta"] = hd["beta"].to_numpy()
    df["z"] = hd["z"].to_numpy()

    trades: list[dict] = []
    in_pos = False
    side = 0
    z_entry = beta_entry = xau_entry = xag_entry = 0.0
    entry_idx = -1

    for i in range(len(df)):
        z = df["z"].iloc[i]
        beta = df["beta"].iloc[i]
        if not (math.isfinite(z) and math.isfinite(beta)):
            continue
        if not in_pos:
            if z > Z_ENTRY:
                side = -1  # spread rich -> short XAU, long XAG
            elif z < -Z_ENTRY:
                side = +1  # spread cheap -> long XAU, short XAG
            else:
                continue
            in_pos = True
            z_entry = z
            beta_entry = beta
            xau_entry = float(xau.iloc[i])
            xag_entry = float(xag.iloc[i])
            entry_idx = i
        else:
            # exit when |z| reverted below Z_EXIT (conservative close-to-close)
            if abs(z) < Z_EXIT or i == len(df) - 1:
                xau_exit = float(xau.iloc[i])
                xag_exit = float(xag.iloc[i])
                d_xau = xau_exit - xau_entry
                d_xag = xag_exit - xag_entry
                gross = side * (d_xau - beta_entry * d_xag)  # dollars, 1-unit spread
                # Cost: project convention is cost_bps = full ROUND-TRIP (entry+exit),
                # charged once on the entry notional (matches this file's docstring line
                # ~30: "Per $1 XAU notional: cost = (bps/1e4)*(1+beta)"). The prior code
                # charged bps on BOTH entry and exit notional = 2x the convention. Fixed.
                cost = (cost_bps / 10000.0) * (
                    xau_entry + abs(beta_entry) * xag_entry
                )
                net = gross - cost
                r = net / (xau_entry * abs(z_entry))  # net $ per $XAU notional, per z-risk
                trades.append({
                    "side": "LONG_SPREAD" if side == 1 else "SHORT_SPREAD",
                    "entry_idx": entry_idx, "exit_idx": i,
                    "entry_time": df["time"].iloc[entry_idx],
                    "exit_time": df["time"].iloc[i],
                    "z_entry": round(z_entry, 3), "z_exit": round(z, 3),
                    "beta": round(beta_entry, 4),
                    "gross": round(gross, 4), "cost": round(cost, 4),
                    "net": round(net, 4), "r": r, "net_r": r,
                })
                in_pos = False
                side = 0
    return trades


def main() -> int:
    ap = argparse.ArgumentParser(description="Gold-silver cointegration stat-arb OOS backtest")
    ap.add_argument("--xau", default="XAUUSDm")
    ap.add_argument("--xag", default="XAGUSDm")
    ap.add_argument("--cost-bps", type=float, default=COST_BPS_DEFAULT)
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--train-ratio", type=float, default=0.6)
    ap.add_argument("--min-trades", type=int, default=MIN_TRADES)
    ap.add_argument("--n-trials", type=int, default=1)
    args = ap.parse_args()

    pq_xau = HISTORY_DIR / f"{args.xau}_D1.parquet"
    pq_xag = HISTORY_DIR / f"{args.xag}_D1.parquet"
    if not pq_xau.exists() or not pq_xag.exists():
        print(f"NO D1 parquet for {args.xau} or {args.xag}")
        return 2
    dx = pd.read_parquet(pq_xau)
    dg = pd.read_parquet(pq_xag)
    dx["time"] = pd.to_datetime(dx["time"])
    dg["time"] = pd.to_datetime(dg["time"])
    if dx["time"].dt.tz is not None:
        dx["time"] = dx["time"].dt.tz_localize(None)
    if dg["time"].dt.tz is not None:
        dg["time"] = dg["time"].dt.tz_localize(None)
    dx = dx[["time", "close"]].rename(columns={"close": "xau"}).dropna()
    dg = dg[["time", "close"]].rename(columns={"close": "xag"}).dropna()
    df = dx.merge(dg, on="time", how="inner").sort_values("time").reset_index(drop=True)
    print(f"Aligned {args.xau}+{args.xag} D1: {len(df)} bars, {df['time'].iloc[0]}..{df['time'].iloc[-1]}")

    trades = run_strategy(df, cost_bps=args.cost_bps)
    if not trades:
        print("No trades generated (z never exceeded entry threshold).")
        return 0

    # Filter to test-window trades for fold stats; full series is OOS (causal hedge).
    rs = [t["r"] for t in trades]
    n = len(rs)
    common_start = df["time"].iloc[0]
    common_end = df["time"].iloc[-1]
    splits = make_splits(common_start, common_end, args.train_ratio, args.folds)
    fold_stats = []
    for sp in splits:
        te_s, te_e = sp["test_start"], sp["test_end"]
        fold_rs = [t["r"] for t in trades if te_s <= pd.Timestamp(t["entry_time"]) < te_e]
        if not fold_rs:
            fold_stats.append({"fold": sp["fold"], "trades": 0, "mean": 0.0, "ci95": [0.0, 0.0]})
            continue
        lo, hi = bootstrap_expectancy_ci(fold_rs)
        fold_stats.append({"fold": sp["fold"], "trades": len(fold_rs),
                           "mean": round(sum(fold_rs)/len(fold_rs), 4),
                           "ci95": [round(lo,4), round(hi,4)]})
        print(f"  Fold {sp['fold']} test[{te_s}..{te_e}]: trades={len(fold_rs)} "
              f"meanR={round(sum(fold_rs)/len(fold_rs),4)} CI95lo={round(lo,4)}")

    wins = [x for x in rs if x > 0]
    gross_win = sum(x for x in rs if x > 0)
    gross_loss = -sum(x for x in rs if x <= 0)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    eq = peak_eq = max_dd = 0.0
    for x in rs:
        eq += x; peak_eq = max(peak_eq, eq); max_dd = max(max_dd, peak_eq - eq)
    mean_r = sum(rs) / n
    lo, hi = bootstrap_expectancy_ci(rs)
    dsr = deflated_sharpe(rs, n_trials=args.n_trials, sr_var_across_trials=0.0)
    folds_positive = sum(1 for f in fold_stats if f["mean"] > 0)
    majority = folds_positive >= (args.folds + 1) // 2
    eligible = n >= args.min_trades
    var = sum((x - mean_r) ** 2 for x in rs) / n
    std = math.sqrt(var)
    organic_winner = (eligible and dsr >= 0.95 and lo > 0 and majority and mean_r > 0)

    print("\n" + "=" * 92)
    print(f"COINT ARB (XAU={args.xau}, XAG={args.xag}, cost={args.cost_bps} bps RT, "
          f"hedge=roll{HEDGE_WINDOW}OLS, z_entry={Z_ENTRY}, z_exit={Z_EXIT}, K={args.n_trials})")
    print("=" * 92)
    print(f"trades={n} win_rate={round(100.0*len(wins)/n,1)}% meanR={round(mean_r,4)} "
          f"PF={round(pf,3) if math.isfinite(pf) else 'inf'} maxDD={round(max_dd,2)}R "
          f"CI95=[{round(lo,4)},{round(hi,4)}]")
    print(f"DSR(precise,net,K={args.n_trials})={dsr:.4f}  folds_positive={folds_positive}/{args.folds}")
    print("-" * 92)
    print("NOTE: non-directional market-neutral stat-arb -- a DIFFERENT edge family from the")
    print("directional trend specs. R = net_$return_per_$1 / |z_entry| (per unit z-edge risk).")
    if n < args.min_trades:
        print(f"NOT ELIGIBLE: only {n} trades (< {args.min_trades}).")
    elif organic_winner:
        print("ORGANIC WINNER: coint stat-arb survives DSR>=0.95 + CI95>0 + majority folds.")
    else:
        reasons = []
        if dsr < 0.95: reasons.append(f"DSR {dsr:.4f}<0.95")
        if lo <= 0: reasons.append(f"CI95 lo {round(lo,4)}<=0")
        if not majority: reasons.append(f"only {folds_positive}/{args.folds} folds positive")
        if mean_r <= 0: reasons.append(f"mean R {round(mean_r,4)}<=0")
        print(f"NO ORGANIC WINNER ({'; '.join(reasons)}).")
        if mean_r > 0 and dsr < 0.95:
            print(f"  -> spread-reversion is POSITIVE ({round(mean_r,4)}) but below DSR bar.")
    print("-" * 92)
    print("Caveat: a passing DSR is a research finding, not $80->$50k validation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())