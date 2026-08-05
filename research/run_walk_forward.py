#!/usr/bin/env python
"""Purged walk-forward validation on the portfolio level.

Runs the MA200, 3M momentum, and blended trend strategies through purged
walk-forward folds with temporal buffers.  Each fold:

  1. Computes per-symbol signals using the full history (train + test,
     so stateful indicators like MA are correct).
  2. Masks signals to test-period dates only.
  3. Builds a vol-scaled, cluster-capped portfolio using train-period
     vol estimates.
  4. Deducts spread/swap/slippage costs.
  5. Reports fold-level Sharpe, MaxDD, and total return.

Uses 12-month training, 4-month test, 5-day purge to handle the
~14-month common history (NAS100m starts 2025-06-15).  Symbols with
insufficient history in a given fold are simply excluded from that
fold's portfolio.

Usage::

    PYTHONPATH=. python research/run_walk_forward.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.data.catalog import RESEARCH_UNIVERSE
from research.strategies.trend_baseline import (
    blended_momentum_signal,
    moving_average_signal,
    time_series_momentum_signal,
)

# -- paths --------------------------------------------------------------------
# Primary: clean exports from research/data/.  Fallback: raw MT5 history.
DATA_DIR = Path("research/data")
FALLBACK_DIR = Path("data/history")
REPORT_PATH = Path("data/walk_forward_report.json")

# -- cost model ---------------------------------------------------------------

COST_BPS: dict[str, float] = {
    "US500m": 1.5, "US30m": 2.0, "NAS100m": 2.5,
    "UK100m": 2.5, "FR40m": 2.5, "JP225m": 3.0,
    "EURUSDm": 0.3, "GBPUSDm": 0.5, "USDJPYm": 0.4,
    "USDCHFm": 0.5, "AUDUSDm": 0.6,
    "XAUUSDm": 2.0, "USOILm": 3.0,
    "BTCUSDm": 5.0,
}
SWAP_BPS_DAILY: dict[str, float] = {
    "XAUUSDm": 0.15, "USOILm": 0.40,
    "EURUSDm": 0.05, "GBPUSDm": 0.04, "USDJPYm": 0.06,
    "USDCHFm": 0.03, "AUDUSDm": 0.04,
    "US500m": 0.08, "US30m": 0.10, "NAS100m": 0.12,
    "UK100m": 0.08, "FR40m": 0.08, "JP225m": 0.10,
    "BTCUSDm": 1.0,
}
SLIPPAGE_BPS: float = 0.5


def cost_per_trade_bps(symbol: str) -> float:
    return COST_BPS.get(symbol, 2.0) + 2 * SLIPPAGE_BPS


def daily_swap_bps(symbol: str) -> float:
    return SWAP_BPS_DAILY.get(symbol, 0.05)


# -- clusters ----------------------------------------------------------------

CLUSTERS: dict[str, list[str]] = {
    "equity_indices": ["US500m", "US30m", "NAS100m", "UK100m", "FR40m", "JP225m"],
    "fx_usd_majors": ["EURUSDm", "GBPUSDm", "AUDUSDm", "USDCHFm", "USDJPYm"],
    "metals": ["XAUUSDm"],
    "energy": ["USOILm"],
    "crypto": ["BTCUSDm"],
}
CLUSTER_CAPS: dict[str, float] = {
    "equity_indices": 0.35, "fx_usd_majors": 0.35,
    "metals": 0.15, "energy": 0.15, "crypto": 0.10,
}
SINGLE_SYMBOL_CAP: float = 0.15
VOL_TARGET: float = 0.15
VOL_LOOKBACK: int = 63


def _symbol_cluster(symbol: str) -> str:
    for name, members in CLUSTERS.items():
        if symbol in members:
            return name
    return "other"


# -- data helpers ------------------------------------------------------------


def _read_close(sym: str) -> pd.Series:
    """Read D1 close prices for a symbol."""
    direct = DATA_DIR / f"{sym}_D1.parquet"
    if not direct.exists():
        direct = FALLBACK_DIR / f"{sym}_D1.parquet"
    if not direct.exists():
        raise FileNotFoundError(str(direct))
    df = pd.read_parquet(direct)
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.set_index("time")
    return df["close"].sort_index()


# -- single-symbol net returns ------------------------------------------------


def _net_returns(close: pd.Series, signal: pd.Series, symbol: str) -> pd.Series:
    ret = close.pct_change()
    gross = (ret * signal.shift(1)).fillna(0)

    tc_bps = cost_per_trade_bps(symbol)
    tc_decimal = tc_bps / 10_000
    swap_bps = daily_swap_bps(symbol) / 10_000

    cost = pd.Series(0.0, index=gross.index)
    vals = signal.values if isinstance(signal, pd.Series) else signal
    for i in range(1, len(signal)):
        if vals[i] != vals[i - 1]:
            idx = signal.index[i]
            if idx in cost.index:
                cost.loc[idx] = tc_decimal

    swap_daily = signal.abs().shift(1).fillna(0) * swap_bps
    swap_daily = swap_daily.reindex(cost.index).fillna(0)

    return gross - cost - swap_daily


# -- fold generator -----------------------------------------------------------


def _generate_folds(
    earliest_start: pd.Timestamp,
    latest_end: pd.Timestamp,
    *,
    train_months: int = 12,
    test_months: int = 4,
    purge_days: int = 5,
    min_train_days: int = 200,
) -> list[dict[str, Any]]:
    """Generate purged walk-forward folds with temporal buffers."""
    folds: list[dict[str, Any]] = []
    train_delta = pd.DateOffset(months=train_months)
    test_delta = pd.DateOffset(months=test_months)
    purge_delta = pd.Timedelta(days=purge_days)

    fold = 0
    test_start = earliest_start + train_delta

    while test_start + test_delta <= latest_end:
        train_end = test_start - purge_delta
        if (train_end - earliest_start).days < min_train_days:
            test_start = test_start + test_delta
            continue

        folds.append({
            "fold": fold,
            "train_start": earliest_start,
            "train_end": train_end,
            "test_start": test_start,
            "test_end": min(test_start + test_delta, latest_end),
        })
        test_start = test_start + test_delta
        fold += 1

    return folds


# -- per-fold portfolio evaluation --------------------------------------------


def _evaluate_fold(
    fold: dict[str, Any],
    all_closes: dict[str, pd.Series],
    model: str,
) -> dict[str, Any]:
    """Evaluate one walk-forward fold for a given model."""
    t_start = fold["train_start"]
    t_end = fold["train_end"]
    test_start = fold["test_start"]
    test_end = fold["test_end"]

    sym_returns: dict[str, pd.Series] = {}
    sym_vols: dict[str, float] = {}

    for sym, full_close in all_closes.items():
        # Per-symbol: check this symbol covers the fold
        sym_start = full_close.index.min()
        sym_end = full_close.index.max()
        if sym_start > t_end or sym_end < test_start:
            continue  # symbol has no data in this fold window

        window = full_close.loc[max(t_start, sym_start):min(test_end, sym_end)]
        if len(window) < 200:
            continue

        if model == "ma_200":
            sig = moving_average_signal(window, period=min(200, len(window) // 2))
        elif model == "mom_3m":
            sig = time_series_momentum_signal(window, lookback=63)
        elif model == "blended":
            sig = blended_momentum_signal(window, horizons=(21, 63, 126, 252), threshold=0.0)
        else:
            continue

        # Mask to test period
        test_mask = (sig.index >= test_start) & (sig.index <= test_end)
        if test_mask.sum() < 20:
            continue

        net = _net_returns(window, sig, sym)  # window is already a close Series
        sym_returns[sym] = net.loc[test_start:test_end]

        # Vol from train portion
        train_net = net.loc[t_start:t_end]
        if len(train_net) >= VOL_LOOKBACK:
            sym_vols[sym] = float(train_net.tail(VOL_LOOKBACK).std() * np.sqrt(252))
        elif len(train_net) > 0:
            sym_vols[sym] = float(train_net.std() * np.sqrt(252))
        else:
            sym_vols[sym] = 0.0

    if len(sym_returns) < 3:
        return {"fold": fold["fold"], "error": f"only {len(sym_returns)} symbols"}

    common = sorted(set.intersection(*[set(r.index) for r in sym_returns.values()]))
    if len(common) < 20:
        return {"fold": fold["fold"], "error": f"only {len(common)} common dates"}

    aligned = {sym: r.reindex(common).fillna(0) for sym, r in sym_returns.items()}

    raw_weights: dict[str, float] = {}
    for sym in aligned:
        vol = sym_vols.get(sym, 0)
        raw_weights[sym] = VOL_TARGET / vol if vol > 0 else 0.0

    total_raw = sum(raw_weights.values())
    if total_raw > 0:
        raw_weights = {s: w / total_raw for s, w in raw_weights.items()}

    weights = dict(raw_weights)
    for sym in weights:
        if weights[sym] > SINGLE_SYMBOL_CAP:
            weights[sym] = SINGLE_SYMBOL_CAP

    cluster_totals: dict[str, float] = {c: 0.0 for c in CLUSTERS}
    cluster_totals["other"] = 0.0
    for sym in sorted(weights, key=lambda s: -weights[s]):
        cluster = _symbol_cluster(sym)
        cap = CLUSTER_CAPS.get(cluster, SINGLE_SYMBOL_CAP)
        if cluster_totals[cluster] + weights[sym] > cap:
            weights[sym] = max(0.0, cap - cluster_totals[cluster])
        cluster_totals[cluster] += weights[sym]

    combined = pd.Series(0.0, index=common)
    for sym, w in weights.items():
        if w > 0 and sym in aligned:
            combined += w * aligned[sym]

    if len(combined) < 10:
        return {"fold": fold["fold"], "error": "too few combined returns"}

    cum = (1 + combined).cumprod()
    total_ret = float(cum.iloc[-1] - 1)
    dd = (cum / cum.cummax() - 1).min()
    ann_ret = float((1 + total_ret) ** (252 / len(combined)) - 1)
    sharpe = float(combined.mean() / combined.std() * np.sqrt(252)) if combined.std() > 0 else 0
    win_days = float((combined > 0).mean())

    return {
        "fold": fold["fold"],
        "train_start": str(t_start.date()),
        "train_end": str(t_end.date()),
        "test_start": str(test_start.date()),
        "test_end": str(test_end.date()),
        "n_symbols": len(aligned),
        "n_days": len(combined),
        "total_return": round(total_ret, 6),
        "annual_return": round(ann_ret, 6),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(float(dd), 4),
        "win_day_pct": round(win_days, 4),
        "vol_annual": round(float(combined.std() * np.sqrt(252)), 4),
    }


# -- top-level walk-forward runner --------------------------------------------


def run_walk_forward(
    model: str,
    all_closes: dict[str, pd.Series],
    *,
    train_months: int = 12,
    test_months: int = 4,
    purge_days: int = 5,
) -> dict[str, Any]:
    """Run walk-forward for one model across all folds."""
    earliest = min(c.index.min() for c in all_closes.values())
    latest = min(c.index.max() for c in all_closes.values())

    folds = _generate_folds(
        earliest, latest,
        train_months=train_months, test_months=test_months, purge_days=purge_days,
    )

    print(f"  {model}: {len(folds)} folds  train={train_months}mo  test={test_months}mo  purge={purge_days}d")
    print(f"    Range: {earliest.date()} -> {latest.date()} ({len(all_closes)} symbols)")

    results: list[dict[str, Any]] = []
    for f in folds:
        r = _evaluate_fold(f, all_closes, model)
        results.append(r)

    valid = [r for r in results if "error" not in r]
    errors = [r for r in results if "error" in r]

    if not valid:
        return {"model": model, "error": "no valid folds", "n_folds": len(folds), "fold_errors": errors}

    sharpes = [r["sharpe"] for r in valid]
    returns = [r["total_return"] for r in valid]
    madds = [r["max_drawdown"] for r in valid]
    pos_sharpe = sum(1 for s in sharpes if s > 0)

    summary = {
        "model": model,
        "n_folds_total": len(folds),
        "n_folds_valid": len(valid),
        "n_folds_error": len(errors),
        "fold_errors": errors if errors else None,
        "sharpe_stability": {
            "mean": round(float(np.mean(sharpes)), 4),
            "std": round(float(np.std(sharpes)), 4),
            "min": round(float(np.min(sharpes)), 4),
            "max": round(float(np.max(sharpes)), 4),
            "positive_folds": pos_sharpe,
            "positive_fraction": round(pos_sharpe / len(valid), 3) if valid else 0,
        },
        "return_stability": {
            "mean": round(float(np.mean(returns)), 6),
            "std": round(float(np.std(returns)), 6),
            "min": round(float(np.min(returns)), 6),
            "max": round(float(np.max(returns)), 6),
            "positive_folds": sum(1 for r in returns if r > 0),
        },
        "drawdown_stability": {
            "mean": round(float(np.mean(madds)), 4),
            "worst": round(float(np.min(madds)), 4),
        },
        "folds": results,
    }

    print(f"    {'Fold':>4s} {'Test Start':>10s} {'End':>10s} {'Sharpe':>8s} {'MaxDD':>8s} {'TotRet':>8s} {'Sym':>4s}")
    print(f"    {'-'*60}")
    for r in results:
        if "error" in r:
            print(f"    {r['fold']:>4d}  ERROR: {r['error'][:40]}")
        else:
            print(
                f"    {r['fold']:>4d}  {r['test_start']:>10s} {r['test_end']:>10s} "
                f"{r['sharpe']:>8.4f} {r['max_drawdown']:>8.4f} {r['total_return']:>8.4f} {r['n_symbols']:>4d}"
            )

    s = summary["sharpe_stability"]
    print(f"    Sharpe: mean={s['mean']:.4f}  std={s['std']:.4f}  min={s['min']:.4f}  max={s['max']:.4f}  "
          f"positive={s['positive_folds']}/{len(valid)} ({s['positive_fraction']:.0%})")
    print()

    return summary


# -- main ---------------------------------------------------------------------


def main() -> int:
    print("=" * 80)
    print("  PURGED WALK-FORWARD PORTFOLIO VALIDATION")
    print("=" * 80)
    print()

    all_closes: dict[str, pd.Series] = {}
    skipped: list[str] = []
    for sym in RESEARCH_UNIVERSE:
        try:
            all_closes[sym] = _read_close(sym)
        except FileNotFoundError:
            skipped.append(sym)
    if skipped:
        print(f"  Skipped (no D1 data): {skipped}")
    print(f"  Loaded {len(all_closes)} symbols")
    print()

    models = ["ma_200", "mom_3m", "blended"]
    all_summaries: dict[str, Any] = {}

    for model in models:
        summary = run_walk_forward(model, all_closes, train_months=12, test_months=4, purge_days=5)
        all_summaries[model] = summary

    # -- comparison table ---------------------------------------------------
    print("=" * 80)
    print("  SHARPE STABILITY COMPARISON")
    print("=" * 80)
    hdr = f"{'Model':12s} {'Folds':>5s} {'ShrpMean':>9s} {'ShrpStd':>9s} {'ShrpMin':>9s} {'ShrpMax':>9s} {'PosFrac':>8s}"
    print(hdr)
    print("-" * 70)
    for model in models:
        s = all_summaries.get(model, {})
        if "error" in s:
            print(f"  {model:12s}  ERROR: {s['error']}")
            continue
        st = s.get("sharpe_stability", {})
        print(
            f"  {model:12s} {s.get('n_folds_valid',0):>5d} "
            f"{st.get('mean',0):>9.4f} {st.get('std',0):>9.4f} "
            f"{st.get('min',0):>9.4f} {st.get('max',0):>9.4f} "
            f"{st.get('positive_fraction',0):>8.0%}"
        )

    print()
    print("  STABILITY GATES:")
    for model in models:
        s = all_summaries.get(model, {})
        if "error" in s:
            print(f"    {model:12s}  SKIP (no valid folds)")
            continue
        st = s.get("sharpe_stability", {})
        mean_s = st.get("mean", 0)
        std_s = st.get("std", 0)
        pos_f = st.get("positive_fraction", 0)

        passes = []
        passes.append(mean_s > 0)
        passes.append(pos_f >= 0.5)
        passes.append(std_s < max(abs(mean_s), 0.01) * 3)
        all_pass = all(passes)

        reasons = []
        if not passes[0]:
            reasons.append("mean Sharpe <= 0")
        if not passes[1]:
            reasons.append(f"only {pos_f:.0%} folds positive")
        if not passes[2]:
            reasons.append(f"Sharpe std {std_s:.3f} > 3x mean")

        status = "PASS" if all_pass else "FAIL"
        print(f"    {model:12s}  {status}  ({'; '.join(reasons)})" if reasons else f"    {model:12s}  {status}")

    # -- write report -------------------------------------------------------
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {
            "train_months": 12,
            "test_months": 4,
            "purge_days": 5,
            "vol_target": VOL_TARGET,
            "vol_lookback": VOL_LOOKBACK,
            "single_symbol_cap": SINGLE_SYMBOL_CAP,
            "cluster_caps": CLUSTER_CAPS,
        },
        "models": all_summaries,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nReport written to {REPORT_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
