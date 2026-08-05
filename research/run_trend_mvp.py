#!/usr/bin/env python
"""D1/H4 Trend MVP — run all three baselines on resampled MT5 history.

Resamples M15 data to D1 and H4, computes signals from the research
strategies module, and produces a walk-forward report per symbol.
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

HISTORY_DIR = Path("data/history")
REPORT_PATH = Path("data/trend_mvp_report.json")

# ── helpers ──────────────────────────────────────────────────────────────────


def _resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample tick data to OHLC bars at *rule* frequency."""
    if "time" in df.columns:
        df = df.copy()
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.set_index("time")
    elif not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("DataFrame must have a DatetimeIndex or 'time' column")

    agg_map: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    for col in ["volume", "tick_volume", "real_volume"]:
        if col in df.columns:
            agg_map[col] = "sum"
    if "spread" in df.columns:
        agg_map["spread"] = "mean"
    return df.resample(rule).agg(agg_map).dropna(subset=["open", "close"])


def _signal_metrics(close: pd.Series, signal: pd.Series) -> dict[str, Any]:
    """Compute basic P&L metrics for a signal series."""
    ret = close.pct_change()
    strat = (ret * signal.shift(1)).dropna()
    if len(strat) < 20:
        return {"n_bars": len(strat), "total_return": 0, "sharpe": 0, "max_dd": 0}

    cum = (1 + strat).cumprod()
    total = float(cum.iloc[-1] - 1)
    dd = (cum / cum.cummax() - 1).min()
    sharpe = float(strat.mean() / strat.std() * np.sqrt(252)) if strat.std() > 0 else 0

    sig_count = int((signal.diff().abs() > 0).sum())

    return {
        "n_bars": len(strat),
        "total_return": round(total, 6),
        "annual_return": round(float((1 + total) ** (252 / max(len(strat), 1)) - 1), 6),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(float(dd), 4),
        "signal_changes": sig_count,
        "avg_signal": round(float(signal.mean()), 4),
        "long_pct": round(float((signal > 0).mean()), 4),
        "short_pct": round(float((signal < 0).mean()), 4),
        "flat_pct": round(float((signal == 0).mean()), 4),
    }


def run_baselines(sym: str, tf: str) -> dict[str, Any]:
    """Run all three trend baselines on one symbol/timeframe and return metrics."""
    path = HISTORY_DIR / f"{sym}_M15.parquet"
    if not path.exists():
        return {"error": f"no M15 data for {sym}"}

    df = pd.read_parquet(path)
    rule = {"D1": "D", "H4": "4h"}[tf]
    daily = _resample_ohlc(df, rule)
    close = daily["close"]

    if len(close) < 252:
        return {"error": f"only {len(close)} {tf} bars for {sym} (need >=252)"}

    result: dict[str, Any] = {
        "symbol": sym,
        "timeframe": tf,
        "bars": len(close),
        "start": str(close.index.min()),
        "end": str(close.index.max()),
    }

    # Baseline A: 200-period MA
    sig_a = moving_average_signal(close, period=min(200, len(close) // 2))
    result["ma_200"] = _signal_metrics(close, sig_a)

    # Baseline B: 3-month momentum (63 bars daily, ~90 bars H4)
    lookback = {"D1": 63, "H4": 90}[tf]
    sig_b = time_series_momentum_signal(close, lookback=lookback)
    result["mom_3m"] = _signal_metrics(close, sig_b)

    # Baseline C: Blended 1/3/6/12-month
    horizons_d1 = (21, 63, 126, 252)
    horizons_h4 = (30, 90, 180, 360)  # approximate H4 equivalents
    horizons = {"D1": horizons_d1, "H4": horizons_h4}[tf]
    sig_c = blended_momentum_signal(close, horizons=horizons, threshold=0.0)
    result["blended"] = _signal_metrics(close, sig_c)

    # Latest signal
    result["latest"] = {
        "ma_200": int(sig_a.iloc[-1]) if len(sig_a) > 0 else 0,
        "mom_3m": int(sig_b.iloc[-1]) if len(sig_b) > 0 else 0,
        "blended": round(float(sig_c.iloc[-1]), 2) if len(sig_c) > 0 else 0,
    }

    return result


# ── cluster summary ──────────────────────────────────────────────────────────

CLUSTERS: dict[str, list[str]] = {
    "equity_indices": ["US500m", "US30m", "NAS100m", "UK100m", "FR40m", "JP225m"],
    "fx_usd_majors": ["EURUSDm", "GBPUSDm", "AUDUSDm", "USDCHFm", "USDJPYm"],
    "metals": ["XAUUSDm"],
    "energy": ["USOILm"],
    "crypto": ["BTCUSDm"],
}


def cluster_summary(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-symbol results into cluster-level metrics."""
    clusters: dict[str, list[float]] = {c: [] for c in CLUSTERS}
    for sym, data in results.items():
        if "error" in data:
            continue
        for model in ["ma_200", "mom_3m", "blended"]:
            ret = data.get(model, {}).get("total_return", 0)
            for cluster, members in CLUSTERS.items():
                if sym in members:
                    clusters[cluster].append(ret)

    out: dict[str, Any] = {}
    for cluster, returns in clusters.items():
        if not returns:
            out[cluster] = {"n": 0}
            continue
        out[cluster] = {
            "n": len(returns),
            "mean_return": round(float(np.mean(returns)), 6),
            "median_return": round(float(np.median(returns)), 6),
            "positive": sum(1 for r in returns if r > 0),
            "negative": sum(1 for r in returns if r <= 0),
        }
    return out


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    timeframes = ["D1", "H4"]
    all_results: dict[str, dict[str, Any]] = {}

    for tf in timeframes:
        print(f"\n{'='*60}")
        print(f"  {tf} TREND BASELINES")
        print(f"{'='*60}")
        print(f"{'Symbol':12s} {'Bars':>6s} {'MA200 Ret':>10s} {'3M Mom Ret':>10s} {'Blend Ret':>10s} {'Latest':>8s}")

        for sym in RESEARCH_UNIVERSE:
            result = run_baselines(sym, tf)
            key = f"{sym}_{tf}"
            all_results[key] = result

            if "error" in result:
                print(f"  {sym:10s}  SKIP: {result['error'][:60]}")
                continue

            ma = result.get("ma_200", {}).get("total_return", 0)
            mom = result.get("mom_3m", {}).get("total_return", 0)
            blend = result.get("blended", {}).get("total_return", 0)
            latest = result.get("latest", {})
            latest_str = f"ma={latest.get('ma_200',0)} mom={latest.get('mom_3m',0)} bl={latest.get('blended',0)}"

            print(
                f"  {sym:10s} {result['bars']:>6d} "
                f"{ma:>10.4f} {mom:>10.4f} {blend:>10.4f} "
                f"  {latest_str}"
            )

    # ── cluster summary ──────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  CLUSTER SUMMARY (blended model, D1)")
    print(f"{'='*60}")
    d1_results = {k.replace("_D1", ""): v for k, v in all_results.items() if k.endswith("_D1")}
    clusters = cluster_summary(d1_results)
    for name, stats in sorted(clusters.items()):
        if stats.get("n", 0) == 0:
            continue
        pos_pct = round(100 * stats["positive"] / stats["n"], 1)
        print(
            f"  {name:20s} n={stats['n']}  mean_ret={stats['mean_return']:>8.4f}  "
            f"median_ret={stats['median_return']:>8.4f}  positive={stats['positive']}/{stats['n']} ({pos_pct}%)"
        )

    # ── write report ─────────────────────────────────────────────────────
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_tf": "M15",
        "target_tfs": ["D1", "H4"],
        "results": all_results,
        "cluster_summary_d1": cluster_summary(d1_results),
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nReport written to {REPORT_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
