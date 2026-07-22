"""D1 Time-Series Momentum (TSMOM) -- Moskowitz-Ooi-Pedersen 2012 (JFE) frozen spec.

WHY THIS SPEC: the N-lever test showed the frozen Donchian-EMA edge is gold+oil-
specific (adding 8 symbols DROPPED DSR 0.827 -> 0.142). Donchian-20 is not a
universal trend rule. TSMOM is *designed* as a cross-asset trend strategy
(vol-targeted 252-day momentum, monthly rebalance) and is the most-cited trend
spec in the literature (MOP 2012 JFE; Hurst-Ooi-Pedersen 2017). It is the natural
test of whether a *cross-asset* trend rule -- rather than a gold-tuned breakout --
carries a retail-cost-survivable edge across the Exness CFD basket.

PRE-REGISTERED, frozen parameters (MOP 2012 defaults, NOT fit on this data) ->
counts as 1 DSR trial (K=1):
  * Signal at each month-end: sign of cumulative return over the past 252
    trading days (~12 months). Long if >0, short if <0, flat if 0.
  * Position size: vol-targeted to 40% annualized. s_t = target_vol / sigma_t,
    sigma_t = EWMA(std of daily returns, span=60) annualized (sqrt 252). Capped
    at max_leverage=5 for realism (retail can't run 20x).
  * Rebalance: monthly (hold 1 month, ~21 trading days). One round-trip cost/month.
  * Exit: sign flip at the next month-end (no intrabar stop -- monthly close-to-close).
  * Basket: equal-weight of per-symbol vol-targeted positions (each at 40% target
    vol, equal dollar weight). Portfolio monthly return = mean over symbols of
    (s_sym * signal_sym * fwd_monthly_return_sym) - mean(s_sym * cost_round_trip).
  * Cost: round-trip 30 bps on the notional s_sym (one entry+exit per month).

EVALUATION GRID (important): DSR is computed on MONTHLY PORTFOLIO returns
(n = number of months). This is a DIFFERENT observation grid from the Donchian
spec's per-trade R-multiples, so the two DSRs are NOT directly comparable -- each
is an independent K=1 deployability test on its own grid. A rigorous head-to-head
would be Hansen SPA on a common monthly grid (future work).

Walk-forward: TSMOM is fully frozen (no IS fitting), so the full monthly series
is out-of-sample by construction. We ALSO report 3-fold monthly stats for
robustness. Net of retail 30 bps. NO LIVE TRADING. Even a passing DSR is a
research finding, not $80->$50k (Peters non-ergodicity + Barber-Odean).

Usage:
  python scripts/d1_tsmom_backtest.py --symbols XAUUSDm USOILm --cost-bps 30
  python scripts/d1_tsmom_backtest.py --symbols XAUUSDm USOILm XAGUSDm XPTUSDm XPDUSDm US500m US30m UK100m FR40m AUDUSDm --cost-bps 30
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from core.history_manager import HISTORY_DIR
from scripts.quantum_loop import deflated_sharpe
from scripts.strategy_evaluator import bootstrap_expectancy_ci, make_splits

TARGET_VOL = 0.40       # 40% annualized vol target (MOP 2012)
LOOKBACK = 252          # 252 trading days (~12 months) momentum lookback
VOL_SPAN = 60           # EWMA span for daily-vol estimate (MOP center-of-mass 60)
MAX_LEVERAGE = 5.0      # cap s_t (retail can't run 20x)
TRADING_DAYS = 252      # annualization


def _monthly_tsmom_returns(df: pd.DataFrame, cost_bps: float) -> pd.DataFrame:
    """Per-symbol monthly TSMOM net returns + position + signal.

    Month-end is the last available D1 bar of each calendar month. Signal uses
    the 252-trading-day return ending at that month-end; position is vol-targeted;
    the forward return is the actual close-to-close return to the NEXT month-end.
    Cost is one round-trip (entry+exit) at 30 bps on the notional s_t.
    """
    df = df.sort_values("time").reset_index(drop=True).copy()
    df["close"] = df["close"].astype(float)
    df["ret"] = df["close"].pct_change()
    # EWMA std of daily returns (span=60). Use ewm std.
    df["vol_daily"] = df["ret"].ewm(span=VOL_SPAN).std()
    df["vol_annual"] = df["vol_daily"] * math.sqrt(TRADING_DAYS)
    # 252-trading-day cumulative return
    df["mom252"] = df["close"].pct_change(LOOKBACK)
    # month-end bars: last bar of each calendar month
    df["ym"] = df["time"].dt.to_period("M")
    month_ends = df.groupby("ym").tail(1).reset_index(drop=True)
    month_ends["signal"] = month_ends["mom252"].apply(lambda x: 1.0 if x > 0 else (-1.0 if x < 0 else 0.0))
    month_ends["sigma"] = month_ends["vol_annual"]
    month_ends["size"] = (TARGET_VOL / month_ends["sigma"]).clip(upper=MAX_LEVERAGE)
    # forward monthly return: close-to-close to next month-end
    fwd = month_ends["close"].shift(-1) / month_ends["close"] - 1.0
    month_ends["fwd_ret"] = fwd
    cost = (cost_bps / 10000.0)
    # net monthly return per unit of capital allocated to this symbol
    month_ends["pnl"] = month_ends["size"] * month_ends["signal"] * month_ends["fwd_ret"] \
        - month_ends["size"] * cost
    return month_ends[["time", "signal", "size", "fwd_ret", "pnl", "close"]].dropna(subset=["fwd_ret"])


def main() -> int:
    ap = argparse.ArgumentParser(description="D1 TSMOM (MOP 2012) OOS backtest")
    ap.add_argument("--symbols", nargs="+", default=["XAUUSDm", "USOILm"])
    ap.add_argument("--cost-bps", type=float, default=30.0)
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--train-ratio", type=float, default=0.6)
    ap.add_argument("--min-trades", type=int, default=40, help="min monthly observations for eligibility")
    ap.add_argument("--n-trials", type=int, default=1)
    args = ap.parse_args()

    per_symbol = []
    for sym in args.symbols:
        pq = HISTORY_DIR / f"{sym}_D1.parquet"
        if not pq.exists():
            print(f"NO D1 parquet for {sym} -- skipping")
            continue
        df = pd.read_parquet(pq)
        df["time"] = pd.to_datetime(df["time"])
        if df["time"].dt.tz is not None:
            df["time"] = df["time"].dt.tz_localize(None)
        me = _monthly_tsmom_returns(df, cost_bps=args.cost_bps)
        if me.empty:
            print(f"{sym}: no monthly trades (need >{LOOKBACK} bars)")
            continue
        per_symbol.append({"symbol": sym, "me": me, "start": me["time"].iloc[0], "end": me["time"].iloc[-1], "n": len(me)})
        print(f"D1 {sym}: {len(df)} bars -> {len(me)} monthly TSMOM trades, {me['time'].iloc[0]}..{me['time'].iloc[-1]}")

    if not per_symbol:
        print("No data loaded.")
        return 2

    # Align on common month-end timestamps; equal-weight basket P&L.
    common_idx = per_symbol[0]["me"]["time"]
    pnl_frame = pd.DataFrame({s["symbol"]: s["me"].set_index("time")["pnl"] for s in per_symbol})
    # equal-weight mean across symbols available each month (skipnan)
    basket_pnl = pnl_frame.mean(axis=1, skipna=True).dropna()
    n = len(basket_pnl)
    rs = basket_pnl.values.tolist()

    # Walk-forward folds on months for robustness
    common_start = min(s["start"] for s in per_symbol)
    common_end = max(s["end"] for s in per_symbol)
    splits = make_splits(common_start, common_end, args.train_ratio, args.folds)
    fold_stats = []
    for sp in splits:
        te_s, te_e = sp["test_start"], sp["test_end"]
        fold_rs = [r for t, r in basket_pnl.items() if te_s <= pd.Timestamp(t) < te_e]
        if not fold_rs:
            fold_stats.append({"fold": sp["fold"], "trades": 0, "mean": 0.0, "ci95": [0.0, 0.0]})
            continue
        lo, hi = bootstrap_expectancy_ci(fold_rs)
        fold_stats.append({"fold": sp["fold"], "trades": len(fold_rs), "mean": round(sum(fold_rs)/len(fold_rs), 4), "ci95": [round(lo,4), round(hi,4)]})
        print(f"  Fold {sp['fold']} test[{te_s}..{te_e}]: months={len(fold_rs)} meanR={round(sum(fold_rs)/len(fold_rs),4)} CI95lo={round(lo,4)}")

    wins = [x for x in rs if x > 0]
    gross_win = sum(x for x in rs if x > 0)
    gross_loss = -sum(x for x in rs if x <= 0)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    eq = peak_eq = max_dd = 0.0
    for x in rs:
        eq += x; peak_eq = max(peak_eq, eq); max_dd = max(max_dd, peak_eq - eq)
    mean_r = sum(rs) / n if n else 0.0
    lo, hi = bootstrap_expectancy_ci(rs)
    dsr = deflated_sharpe(rs, n_trials=args.n_trials, sr_var_across_trials=0.0)
    folds_positive = sum(1 for f in fold_stats if f["mean"] > 0)
    majority = folds_positive >= (args.folds + 1) // 2
    eligible = n >= args.min_trades
    # annualized Sharpe for reporting (DSR itself uses per-observation form)
    var = sum((x - mean_r) ** 2 for x in rs) / n if n else 0.0
    std = math.sqrt(var)
    sharpe_ann = (mean_r / std) * math.sqrt(12) if std > 0 else 0.0
    organic_winner = (eligible and dsr >= 0.95 and lo > 0 and majority and mean_r > 0)

    basket = "+".join(s["symbol"] for s in per_symbol)
    print("\n" + "=" * 92)
    print(f"D1 TSMOM (MOP 2012)  (basket={basket}, cost={args.cost_bps} bps RT, folds={args.folds}, K={args.n_trials})")
    print("=" * 92)
    print(f"months={n} win_rate={round(100.0*len(wins)/n,1) if n else 0}% mean_monthly_R={round(mean_r,4)} "
          f"PF={round(pf,3) if math.isfinite(pf) else 'inf'} maxDD={round(max_dd,2)}R "
          f"ann_Sharpe={round(sharpe_ann,3)} CI95=[{round(lo,4)},{round(hi,4)}]")
    print(f"DSR(precise,monthly,K={args.n_trials})={dsr:.4f}  folds_positive={folds_positive}/{args.folds}")
    print("-" * 92)
    print("NOTE: DSR is on MONTHLY portfolio returns -- different grid from the Donchian")
    print("spec's per-trade R-multiples, so the two DSRs are NOT directly comparable.")
    if n < args.min_trades:
        print(f"NOT ELIGIBLE: only {n} months (< {args.min_trades}).")
    elif organic_winner:
        print("ORGANIC WINNER: TSMOM survives DSR>=0.95 + CI95>0 + majority folds.")
    else:
        reasons = []
        if dsr < 0.95: reasons.append(f"DSR {dsr:.4f}<0.95")
        if lo <= 0: reasons.append(f"CI95 lo {round(lo,4)}<=0")
        if not majority: reasons.append(f"only {folds_positive}/{args.folds} folds positive")
        if mean_r <= 0: reasons.append(f"mean monthly R {round(mean_r,4)}<=0")
        print(f"NO ORGANIC WINNER ({'; '.join(reasons)}).")
        if mean_r > 0 and dsr < 0.95:
            print(f"  -> monthly edge is POSITIVE ({round(mean_r,4)}) but below DSR bar.")
    print("-" * 92)
    print("Caveat: a passing DSR is a research finding, not $80->$50k validation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())