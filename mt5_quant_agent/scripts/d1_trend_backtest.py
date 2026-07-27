"""D1 Donchian-EMA trend strategy -- the one literature-supported redirect.

Self-contained out-of-sample backtest on XAUUSDm D1 (~8y, 2018-2026). This is the
edge-finding direction VERDICT.md §7 names and the D1 research plan quantified:
at D1 the 1.5*ATR(D1) stop is ~4x wider than M5's, so retail 30 bps cost drops
from cost_r~=1.66R (M5, edge inverts) to cost_r~=0.17R (D1, a +0.50R trend gross
edge survives net at ~+0.33R). The binding constraint is sample size -- 8y of D1
yields ~60-120 Donchian-20 trades, enough for a DSR verdict where the 260-day M5
window (only ~15-25 D1 trades) was not.

PRE-REGISTERED, frozen parameters (literature defaults, NOT fit on this data) ->
counts as 1 DSR trial (no in-window hyperparameter tuning):
  * Entry long : close > Donchian_high(20) AND EMA(50) > EMA(200)
  * Entry short: close < Donchian_low(20)  AND EMA(50) < EMA(200)
  * Stop       : 1.5 * ATR(14, D1) at entry (same kernel as the cost model)
  * Exit       : Chandelier trailing stop = peak_high - 3*ATR(14) (ratcheted),
                 OR EMA(50) flip -- no fixed TP (let winners run; the trend edge
                 is in the fat right tail).
  * Size       : 0.5% risk/trade (informational; R-multiple math is size-invariant)
  * Entry fill : NEXT bar open after the signal bar closes (no intrabar entry
                 look-ahead). Exits intrabar (stop-first) on subsequent bars.

Honesty conventions (match scripts/independent_backtest_check.py -- zero shared
code with the M5 PaperBroker, the project's cross-check discipline):
  * Causal indicators (EMA/Donchian/ATR use only prior bars).
  * Walk-forward: 3 folds, expanding train, marching non-overlapping test
    windows. Trades counted only when the entry falls in the test window.
  * Net of retail cost via --cost-bps (cost_r = cost_price / 1.5*ATR(D1)).
  * Verdict reuses quantum_loop.deflated_sharpe (precise two-quantile, net R) +
    strategy_evaluator.bootstrap_expectancy_ci, so the bar is identical to the
    intraday grid's. organic_winner needs DSR>=0.95, CI95 lo>0, majority folds
    positive, >=40 trades, netR > Bonferroni floor (K=1 -> floor ~0).

NO LIVE TRADING. Paper/research only. Even a passing D1 DSR does NOT validate the
$80->$50k path -- Peters (2011/2019) non-ergodicity + Barber-Odean census still
bind at retail account size. This is a research finding (cost-structural escape
from the intraday negative), not a wealth vehicle.

Usage:
  python scripts/d1_trend_backtest.py --cost-bps 30 --folds 3
  python scripts/d1_trend_backtest.py --cost-bps 30 --folds 3 --symbol XAUUSDm
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


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def run_strategy(df: pd.DataFrame, *, cost_r: float) -> list[dict]:
    """Walk D1 bars causally; return trades (one position at a time, no pyramiding).

    Each trade dict carries entry/exit/sl/side + R and net R. Entry fills at the
    NEXT bar's open after a signal bar closes; exits are intrabar stop-first.
    """
    df = df.sort_values("time").reset_index(drop=True)
    high = df["high"].astype(float).values
    low = df["low"].astype(float).values
    close = df["close"].astype(float).values
    opn = df["open"].astype(float).values
    times = df["time"].values
    atr = _atr(df, 14).values
    ema50 = _ema(df["close"], 50).values
    ema200 = _ema(df["close"], 200).values
    don_high = df["high"].rolling(20).max().shift(1).values  # prior 20 bars, excl current
    don_low = df["low"].rolling(20).min().shift(1).values

    trades: list[dict] = []
    in_pos = False
    side = 0  # +1 long, -1 short
    entry = stop = peak = 0.0
    entry_idx = -1

    for i in range(1, len(df)):
        # --- manage open position intrabar (stop-first / conservative) ---
        # The Chandelier trail is computed from the peak as it stood at the
        # START of this bar (prior bar's peak). The current bar's favourable
        # extreme is only ratcheted into `peak` AFTER the adverse check, so a
        # single bar whose high AND low both exceed the prior trail cannot use
        # its own high to tighten the trail its own low is tested against
        # (that would be optimistic intrabar ordering, contradicting the
        # stop-first principle the PaperBroker uses and this script's own
        # docstring). The peak ratchets only if the bar did NOT trigger an exit.
        if in_pos:
            atr_i = atr[i] if math.isfinite(atr[i]) else atr[i - 1]
            if side == 1:
                trail = peak - 3.0 * atr_i
                # Active stop = the TIGHTER (higher for a long) of the initial
                # stop and the ratcheted Chandelier trail. Once peak has risen
                # >= 1.5*ATR, trail > initial stop and the initial stop is no
                # longer in force; exiting at the lower initial stop would
                # understate R on winners that reverse through both levels.
                eff_stop = max(stop, trail)
                exit_price = None
                if low[i] <= eff_stop:
                    exit_price = eff_stop
                elif ema50[i] < ema200[i] and math.isfinite(ema50[i]) and math.isfinite(ema200[i]):
                    exit_price = close[i]  # EMA flip -> exit at close
                if exit_price is None:
                    peak = max(peak, high[i])  # ratchet for FUTURE bars only
            else:  # short
                trail = peak + 3.0 * atr_i
                eff_stop = min(stop, trail)  # tighter = lower for a short
                exit_price = None
                if high[i] >= eff_stop:
                    exit_price = eff_stop
                elif ema50[i] > ema200[i] and math.isfinite(ema50[i]) and math.isfinite(ema200[i]):
                    exit_price = close[i]
                if exit_price is None:
                    peak = min(peak, low[i])  # ratchet for FUTURE bars only
            if exit_price is not None:
                sl_dist = abs(entry - stop)
                r = (exit_price - entry) * side / sl_dist if sl_dist > 0 else 0.0
                trades.append({
                    "side": "BUY" if side == 1 else "SELL",
                    "entry_idx": entry_idx, "exit_idx": i,
                    "entry_time": times[entry_idx], "exit_time": times[i],
                    "entry": entry, "exit": exit_price, "sl": stop,
                    "r": r, "net_r": r - cost_r,
                })
                in_pos = False
                side = 0
        # --- look for entry signal on this bar's close; fill at next bar open ---
        if not in_pos and i + 1 < len(df):
            if not (math.isfinite(don_high[i]) and math.isfinite(ema50[i]) and math.isfinite(ema200[i]) and math.isfinite(atr[i])):
                continue
            long_sig = close[i] > don_high[i] and ema50[i] > ema200[i]
            short_sig = close[i] < don_low[i] and ema50[i] < ema200[i]
            if long_sig:
                side = 1
            elif short_sig:
                side = -1
            else:
                continue
            entry = opn[i + 1]  # fill at next bar open (no intrabar entry look-ahead)
            atr_i = atr[i]
            stop = entry - side * 1.5 * atr_i
            # peak starts at the entry FILL price (conservative): the entry
            # bar's favourable extreme is only ratcheted into the trail for
            # bars AFTER the entry bar, so the entry bar's own adverse extreme
            # is tested against a trail based on the fill, not the entry-bar high.
            peak = entry
            entry_idx = i + 1
            in_pos = True
    return trades


def _stats(rs: list[float], *, cost_r: float) -> dict:
    n = len(rs)
    if n == 0:
        return {"trades": 0, "win_rate_pct": 0.0, "expectancy_r": 0.0,
                "expectancy_net_r": 0.0, "profit_factor": 0.0, "max_drawdown_r": 0.0,
                "ci95": [0.0, 0.0], "sum_r": 0.0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    net_rs = [r - cost_r for r in rs]
    pf = (sum(x for x in net_rs if x > 0) / abs(sum(x for x in net_rs if x <= 0))) if any(x <= 0 for x in net_rs) else float("inf")
    # max drawdown on cumulative net R
    eq = 0.0
    peak_eq = 0.0
    max_dd = 0.0
    for x in net_rs:
        eq += x
        peak_eq = max(peak_eq, eq)
        max_dd = max(max_dd, peak_eq - eq)
    lo, hi = bootstrap_expectancy_ci(net_rs)
    return {
        "trades": n,
        "win_rate_pct": round(100.0 * len(wins) / n, 1),
        "expectancy_r": round(sum(rs) / n, 4),
        "expectancy_net_r": round(sum(net_rs) / n, 4),
        "profit_factor": round(pf, 3) if math.isfinite(pf) else "inf",
        "max_drawdown_r": round(max_dd, 2),
        "ci95": [round(lo, 4), round(hi, 4)],
        "sum_r": round(sum(rs), 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="D1 Donchian-EMA trend OOS backtest")
    ap.add_argument("--symbols", nargs="+", default=["XAUUSDm"],
                    help="pre-specified D1 basket to pool (same frozen strategy, K=1). "
                         "e.g. --symbols XAUUSDm USOILm BTCUSDm")
    ap.add_argument("--cost-bps", type=float, default=30.0, help="round-trip cost in bps of notional")
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--train-ratio", type=float, default=0.6)
    ap.add_argument("--min-trades", type=int, default=40)
    ap.add_argument("--n-trials", type=int, default=1, help="DSR selection-bias trials (1 = pre-registered single strategy on a pre-specified basket)")
    args = ap.parse_args()

    # Per-symbol load + cost_r; pool R-multiples across the pre-specified basket.
    # K stays 1: one frozen strategy applied to a basket chosen BEFORE looking at
    # results (not selected post-hoc). Per-symbol cost_r because ATR/spot differ.
    per_symbol: list[dict] = []
    for sym in args.symbols:
        pq = HISTORY_DIR / f"{sym}_D1.parquet"
        if not pq.exists():
            print(f"NO D1 parquet for {sym} at {pq} -- skipping (run history download D1).")
            continue
        df = pd.read_parquet(pq)
        df["time"] = pd.to_datetime(df["time"])
        if df["time"].dt.tz is not None:
            df["time"] = df["time"].dt.tz_localize(None)
        df = df.sort_values("time").reset_index(drop=True)
        atr14_med = _atr(df, 14).median()
        spot = float(df["close"].astype(float).median())
        cost_price = (args.cost_bps / 10000.0) * spot
        cost_r = cost_price / (1.5 * atr14_med) if atr14_med and atr14_med > 0 else 0.0
        trades = run_strategy(df, cost_r=cost_r)
        per_symbol.append({"symbol": sym, "df": df, "cost_r": cost_r, "trades": trades,
                           "start": df["time"].iloc[0], "end": df["time"].iloc[-1], "bars": len(df),
                           "spot": spot, "atr14_med": atr14_med})
        print(f"D1 {sym}: {len(df)} bars, {df['time'].iloc[0]} -> {df['time'].iloc[1] if len(df)>1 else df['time'].iloc[0]}...{df['time'].iloc[-1]} "
              f"({(df['time'].iloc[-1]-df['time'].iloc[0]).total_seconds()/86400/365.25:.2f} y) | "
              f"spot={spot:.2f} ATR14={atr14_med:.4f} cost_r@{args.cost_bps}bps={cost_r:.4f}R | {len(trades)} trades")

    if not per_symbol:
        print("No D1 data loaded. Run scripts/check_d1_depth.py / history download first.")
        return 2

    # Use the widest common window across the basket for fold alignment; trades
    # are bucketed per-symbol by their own entry_time into the shared fold windows.
    common_start = min(s["start"] for s in per_symbol)
    common_end = max(s["end"] for s in per_symbol)
    splits = make_splits(common_start, common_end, args.train_ratio, args.folds)

    fold_stats = []
    pooled_rs = []  # gross R
    pooled_net_rs = []
    pooled_cost_rs = []  # per-trade cost_r, for the trade-weighted basket avg
    for sp in splits:
        te_s, te_e = sp["test_start"], sp["test_end"]
        fold_rs = []
        for s in per_symbol:
            for t in s["trades"]:
                if te_s <= pd.Timestamp(t["entry_time"]) < te_e:
                    fold_rs.append(t["r"])
        # per-fold net stats use a basket-averaged cost_r (trade-weighted)
        avg_cost = sum(s["cost_r"] * sum(1 for t in s["trades"] if te_s <= pd.Timestamp(t["entry_time"]) < te_e)
                       for s in per_symbol) / max(1, len(fold_rs))
        st = _stats(fold_rs, cost_r=avg_cost)
        st["fold"] = sp["fold"]
        fold_stats.append(st)
        pooled_rs.extend(fold_rs)
        for s in per_symbol:
            for t in s["trades"]:
                if te_s <= pd.Timestamp(t["entry_time"]) < te_e:
                    pooled_net_rs.append(t["r"] - s["cost_r"])
                    pooled_cost_rs.append(s["cost_r"])
        print(f"  Fold {sp['fold']} test[{te_s}..{te_e}]: trades={st['trades']} "
              f"wr={st['win_rate_pct']}% netR={st['expectancy_net_r']} "
              f"PF={st['profit_factor']} CI95lo={st['ci95'][0]}")

    # Pooled stats on net R (trade-weighted per-symbol cost already applied).
    n = len(pooled_net_rs)
    wins = [x for x in pooled_net_rs if x > 0]
    gross_win = sum(x for x in pooled_net_rs if x > 0)
    gross_loss = -sum(x for x in pooled_net_rs if x <= 0)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    eq = peak_eq = max_dd = 0.0
    for x in pooled_net_rs:
        eq += x; peak_eq = max(peak_eq, eq); max_dd = max(max_dd, peak_eq - eq)
    mean_net = sum(pooled_net_rs) / n if n else 0.0
    mean_gross = sum(pooled_rs) / n if n else 0.0
    lo, hi = bootstrap_expectancy_ci(pooled_net_rs)
    dsr = deflated_sharpe(pooled_net_rs, n_trials=args.n_trials, sr_var_across_trials=0.0)
    folds_positive = sum(1 for s in fold_stats if s["expectancy_net_r"] > 0)
    majority = folds_positive >= (args.folds + 1) // 2
    eligible = n >= args.min_trades
    # Trade-weighted basket avg cost_r over the POOLED TEST-WINDOW trades only
    # (numerator and denominator both test-window -- the prior bug used all-history
    # trade counts in the numerator, inflating the Bonferroni floor ~2x).
    avg_cost_pooled = (sum(pooled_cost_rs) / n) if n else 0.0
    bonferroni_floor = (avg_cost_pooled * 0.5) * (1 + args.n_trials / 40.0)
    organic_winner = (eligible and dsr >= 0.95 and lo > 0 and majority and mean_net > bonferroni_floor)

    basket = "+".join(s["symbol"] for s in per_symbol)
    print("\n" + "=" * 92)
    print(f"D1 DONCHIAN-EMA TREND  (basket={basket}, avg cost_r~{avg_cost_pooled:.4f}R @ {args.cost_bps} bps, "
          f"folds={args.folds}, K={args.n_trials})")
    print("=" * 92)
    print(f"pooled: trades={n} wr={round(100.0*len(wins)/n,1) if n else 0}% "
          f"grossR={round(mean_gross,4)} netR={round(mean_net,4)} "
          f"PF={round(pf,3) if math.isfinite(pf) else 'inf'} maxDD={round(max_dd,2)}R "
          f"CI95=[{round(lo,4)},{round(hi,4)}]")
    print(f"DSR(precise,net,K={args.n_trials})={dsr:.4f}  folds_positive={folds_positive}/{args.folds} "
          f"bonferroni_floor={bonferroni_floor:.4f}")
    print("-" * 92)
    if n < args.min_trades:
        print(f"NOT ELIGIBLE: only {n} trades (< {args.min_trades} floor). "
              f"Verdict uninformative at this sample size.")
    elif organic_winner:
        print("ORGANIC WINNER: D1 trend edge survives DSR>=0.95 + CI95>0 + majority folds + Bonferroni.")
    else:
        reasons = []
        if dsr < 0.95: reasons.append(f"DSR {dsr:.4f}<0.95")
        if lo <= 0: reasons.append(f"CI95 lo {round(lo,4)}<=0")
        if not majority: reasons.append(f"only {folds_positive}/{args.folds} folds positive")
        if mean_net <= bonferroni_floor: reasons.append(f"netR {round(mean_net,4)}<=floor {bonferroni_floor:.4f}")
        print(f"NO ORGANIC WINNER ({'; '.join(reasons)}).")
        if mean_net > 0 and dsr < 0.95:
            print(f"  -> net edge is POSITIVE ({round(mean_net,4)}R) but below the DSR deploy bar: "
                  f"real-but-sub-threshold, NOT cost-negative (unlike intraday). This is the cost-structural escape.")
    print("-" * 92)
    print("Caveat: a passing D1 DSR is a research finding, not $80->$50k validation "
          "(Peters non-ergodicity + Barber-Odean bind at retail account size).")
    return 0


if __name__ == "__main__":
    sys.exit(main())