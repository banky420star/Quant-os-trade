"""Day-of-week calendar-seasonal backtest (QC-derived, Qadan 2019).

PRE-REGISTERED hypothesis (K=1, no parameter search): long the asset for the
return of a SPECIFIC pre-named weekday (Friday for USOIL per Qadan 2019 WTI
Friday effect; XAU tested as a second independent K=1). Enter at prior-day
close, exit at target-day close. Round-trip cost charged per trade at real
Exness-measured ~6 bps (XAU 1.2 / USOIL 5.6 RT, rounded up).

Walk-forward 3 folds -> per-cell DSR (deflated for K=1 -> = Sharpe), two-sided
CI95 lower bound, fold-robustness. Honest deploy gate (all must pass):
DSR >= 0.95 AND CI95 lo > 0 AND >=2/3 folds positive.

The weekday can also be scanned (--scan) to show the in-sample max weekday
for reference, BUT the verdict is only reported for the pre-registered
--weekday. Selecting the in-sample max would require K=5 deflation and is
NOT done here -- that is the selection-bias guard.

Run:
    python scripts/dow_seasonal_backtest.py --symbol USOILm --weekday 4 --cost-bps 6
    python scripts/dow_seasonal_backtest.py --symbol USOILm --scan
"""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "history"
WD_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def load(sym: str) -> pd.DataFrame:
    df = pd.read_parquet(HIST / f"{sym}_D1.parquet")
    df["t"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("t").sort_index()
    df["ret"] = np.log(df["close"] / df["close"].shift(1))
    df["dow"] = df.index.dayofweek
    return df.dropna()


def sharpe(r: np.ndarray) -> float:
    if len(r) < 2:
        return 0.0
    m, s = float(np.mean(r)), float(np.std(r, ddof=1))
    if s <= 0:
        return 0.0
    return m / s * math.sqrt(252)


def ci95_lo(r: np.ndarray) -> float:
    n = len(r)
    if n < 2:
        return 0.0
    m, s = float(np.mean(r)), float(np.std(r, ddof=1))
    if s <= 0:
        return m
    return m - 1.96 * s / math.sqrt(n)


def dsr(sharpe_raw: float, n_trials: int, var_obs: float = 0.0) -> float:
    """Deflated Sharpe (Bailey & Lopez de Prado). K=1 -> sr_var 0 -> no deflation."""
    if n_trials <= 1 or var_obs <= 0:
        return sharpe_raw
    e_sharpe = math.sqrt(var_obs) * ((1 - 0.0) * (2.0 * math.log(n_trials)))
    e_sharpe_adj = e_sharpe * (1 - 0.0)  # skew/kurt omitted (unknown for daily)
    return (sharpe_raw - e_sharpe_adj) if sharpe_raw > e_sharpe_adj else 0.0


def backtest(sym: str, weekday: int, cost_bps: float, folds: int):
    df = load(sym)
    cost_r = cost_bps / 10000.0  # daily-return-space cost fraction per RT trade
    # strategy: capture the return of `weekday` -> hold from prior close to weekday close
    trades = []
    for i in range(1, len(df)):
        if int(df["dow"].iloc[i]) == weekday:
            r = float(df["ret"].iloc[i]) - cost_r  # pay RT cost on entry+exit
            trades.append({"date": df.index[i].strftime("%Y-%m-%d"), "ret": r})
    if not trades:
        return {"symbol": sym, "weekday": weekday, "weekday_name": WD_NAMES[weekday],
                "n_trades": 0, "verdict": "NO_TRADES"}
    rets = np.array([t["ret"] for t in trades])
    # walk-forward folds by date order
    dates = pd.to_datetime([t["date"] for t in trades])
    fold_labels = pd.qcut(np.arange(len(rets)), folds, labels=False)
    fold_pos = []
    for f in range(folds):
        fr = rets[fold_labels == f]
        fold_pos.append(bool(fr.mean() > 0)) if len(fr) else fold_pos.append(False)
    sh = sharpe(rets)
    return {
        "symbol": sym, "weekday": weekday, "weekday_name": WD_NAMES[weekday],
        "n_trades": len(rets), "cost_bps": cost_bps,
        "mean_bps": float(np.mean(rets) * 1e4),
        "t_stat": float(np.mean(rets) / (np.std(rets, ddof=1) / math.sqrt(len(rets)))) if len(rets) > 1 else 0.0,
        "sharpe_ann": sh, "dsr": dsr(sh, 1, 0.0),
        "ci95_lo_bps": float(ci95_lo(rets) * 1e4),
        "fold_positive": f"{sum(fold_pos)}/{folds}",
        "win_rate_pct": float((rets > 0).mean() * 100),
        "verdict": _verdict(sh, ci95_lo(rets), sum(fold_pos), folds),
    }


def _verdict(sh, ci_lo, n_pos, folds):
    ok = sh >= 0.95 and ci_lo > 0 and n_pos >= 2
    return "DEPLOY_CANDIDATE" if ok else "FAILS_GATE"


def scan(sym: str, cost_bps: float):
    df = load(sym)
    cost_r = cost_bps / 10000.0
    print(f"=== {sym} in-sample weekday scan (reference only, NOT pre-registered) ===")
    for wd in range(5):  # Mon-Fri only
        r = df.loc[df["dow"] == wd, "ret"].values - cost_r
        if len(r) < 2:
            continue
        t = float(np.mean(r) / (np.std(r, ddof=1) / math.sqrt(len(r))))
        print(f"  {WD_NAMES[wd]} n={len(r):4d} mean={np.mean(r)*1e4:7.2f}bps t={t:+.2f} sharpe={sharpe(r):.2f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="USOILm")
    ap.add_argument("--weekday", type=int, default=4, help="0=Mon..4=Fri (pre-registered)")
    ap.add_argument("--cost-bps", type=float, default=6.0)
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--scan", action="store_true")
    args = ap.parse_args()
    if args.scan:
        scan(args.symbol, args.cost_bps)
        return
    res = backtest(args.symbol, args.weekday, args.cost_bps, args.folds)
    print(json.dumps(res, indent=2))
    out = ROOT / "state" / "dow_seasonal_result.json"
    out.write_text(json.dumps(res, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()