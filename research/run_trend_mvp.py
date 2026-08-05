#!/usr/bin/env python
"""D1/H4 Trend MVP — cost-aware baselines with double-cost stress survival.

Resamples M15 data to D1 and H4, runs MA200, 3M momentum, and blended
1/3/6/12-month models.  Deducts estimated spread, swap, and slippage
per signal flip, then stress-tests at 1×, 2×, and 3× costs.
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
REPORT_PATH = Path("data/trend_mvp_cost_report.json")

# ── cost model ───────────────────────────────────────────────────────────────
# Estimated one-way cost in basis points per symbol class.
# These are conservative planning estimates; real costs come from
# broker_symbol_specs.json + mt5.order_calc_profit().

COST_BPS: dict[str, float] = {
    # Equity indices — wider spreads on CFDs
    "US500m": 1.5, "US30m": 2.0, "NAS100m": 2.5,
    "UK100m": 2.5, "FR40m": 2.5, "JP225m": 3.0,
    # FX majors — tight spreads
    "EURUSDm": 0.3, "GBPUSDm": 0.5, "USDJPYm": 0.4,
    "USDCHFm": 0.5, "AUDUSDm": 0.6,
    # Metals & energy — moderate
    "XAUUSDm": 2.0, "USOILm": 3.0,
    # Crypto — wide
    "BTCUSDm": 5.0,
}

# Daily swap cost in bps (1 bp = 0.01%).  Negative = you receive.
# Long-only estimate; shorts are symm for this rough pass.
SWAP_BPS_DAILY: dict[str, float] = {
    "XAUUSDm": 0.15, "USOILm": 0.40,
    "EURUSDm": 0.05, "GBPUSDm": 0.04, "USDJPYm": 0.06,
    "USDCHFm": 0.03, "AUDUSDm": 0.04,
    "US500m": 0.08, "US30m": 0.10, "NAS100m": 0.12,
    "UK100m": 0.08, "FR40m": 0.08, "JP225m": 0.10,
    "BTCUSDm": 1.0,
}

# Slippage in bps per entry/exit
SLIPPAGE_BPS: float = 0.5


def cost_per_trade_bps(symbol: str) -> float:
    """Round-trip cost in bps: spread + 2×slippage (entry+exit)."""
    spread = COST_BPS.get(symbol, 2.0)
    return spread + 2 * SLIPPAGE_BPS


def daily_swap_bps(symbol: str) -> float:
    return SWAP_BPS_DAILY.get(symbol, 0.05)


# ── resampling ───────────────────────────────────────────────────────────────


def _resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if "time" in df.columns:
        df = df.copy()
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.set_index("time")
    elif not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("DataFrame must have a DatetimeIndex or 'time' column")

    agg_map: dict[str, str] = {
        "open": "first", "high": "max", "low": "min", "close": "last",
    }
    for col in ["volume", "tick_volume", "real_volume"]:
        if col in df.columns:
            agg_map[col] = "sum"
    if "spread" in df.columns:
        agg_map["spread"] = "mean"
    return df.resample(rule).agg(agg_map).dropna(subset=["open", "close"])


# ── cost-aware metrics ───────────────────────────────────────────────────────


def _signal_metrics_cost(
    close: pd.Series,
    signal: pd.Series,
    symbol: str,
    *,
    cost_mult: float = 1.0,
) -> dict[str, Any]:
    """Compute net P&L metrics after transaction + holding costs."""
    ret = close.pct_change()
    gross = (ret * signal.shift(1)).dropna()
    if len(gross) < 20:
        return {"n_bars": len(gross), "total_return": 0, "sharpe": 0, "max_dd": 0}

    # Count signal flips = number of round-trip trades
    flips = int((signal.diff().abs() > 0).sum())

    # Transaction cost: per-flip round-trip cost in decimal
    tc_bps = cost_per_trade_bps(symbol) * cost_mult
    tc_decimal = tc_bps / 10_000

    # Holding cost: daily swap × signal direction × cost multiplier
    swap_bps = daily_swap_bps(symbol) * cost_mult / 10_000

    # Build cost series
    cost_series = pd.Series(0.0, index=gross.index)

    # Transaction cost at each flip
    for i in range(1, len(signal)):
        if signal.iloc[i] != signal.iloc[i - 1]:
            idx = signal.index[i]
            if idx in cost_series.index:
                cost_series.loc[idx] = tc_decimal

    # Daily swap when in position
    swap_daily = signal.abs().shift(1).fillna(0) * swap_bps
    swap_daily = swap_daily.reindex(cost_series.index).fillna(0)

    net = gross - cost_series - swap_daily
    cum = (1 + net).cumprod()
    total = float(cum.iloc[-1] - 1)
    dd = (cum / cum.cummax() - 1).min()
    sharpe = float(net.mean() / net.std() * np.sqrt(252)) if net.std() > 0 else 0

    return {
        "n_bars": len(net),
        "total_return": round(total, 6),
        "annual_return": round(float((1 + total) ** (252 / max(len(net), 1)) - 1), 6),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(float(dd), 4),
        "signal_changes": flips,
        "cost_mult": cost_mult,
        "cost_bps_rt": round(tc_bps, 2),
        "swap_bps_daily": round(swap_bps * 10_000, 3),
        "total_cost_deducted": round(float(cost_series.sum() + swap_daily.sum()), 6),
        "avg_signal": round(float(signal.mean()), 4),
        "long_pct": round(float((signal > 0).mean()), 4),
        "short_pct": round(float((signal < 0).mean()), 4),
        "flat_pct": round(float((signal == 0).mean()), 4),
    }


# ── single-symbol runner ─────────────────────────────────────────────────────


def run_baselines(sym: str, tf: str) -> dict[str, Any]:
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
        "cost_model": {
            "spread_bps": COST_BPS.get(sym, 2.0),
            "slippage_bps": SLIPPAGE_BPS,
            "swap_bps_daily": SWAP_BPS_DAILY.get(sym, 0.05),
            "round_trip_bps": round(cost_per_trade_bps(sym), 2),
        },
    }

    # Baseline A: 200-period MA
    sig_a = moving_average_signal(close, period=min(200, len(close) // 2))
    result["ma_200"] = {
        "gross": _signal_metrics_cost(close, sig_a, sym, cost_mult=0.0),
        "net_1x": _signal_metrics_cost(close, sig_a, sym, cost_mult=1.0),
        "net_2x": _signal_metrics_cost(close, sig_a, sym, cost_mult=2.0),
        "net_3x": _signal_metrics_cost(close, sig_a, sym, cost_mult=3.0),
    }

    # Baseline B: 3-month momentum
    lookback = {"D1": 63, "H4": 90}[tf]
    sig_b = time_series_momentum_signal(close, lookback=lookback)
    result["mom_3m"] = {
        "gross": _signal_metrics_cost(close, sig_b, sym, cost_mult=0.0),
        "net_1x": _signal_metrics_cost(close, sig_b, sym, cost_mult=1.0),
        "net_2x": _signal_metrics_cost(close, sig_b, sym, cost_mult=2.0),
        "net_3x": _signal_metrics_cost(close, sig_b, sym, cost_mult=3.0),
    }

    # Baseline C: Blended
    horizons_d1 = (21, 63, 126, 252)
    horizons_h4 = (30, 90, 180, 360)
    horizons = {"D1": horizons_d1, "H4": horizons_h4}[tf]
    sig_c = blended_momentum_signal(close, horizons=horizons, threshold=0.0)
    result["blended"] = {
        "gross": _signal_metrics_cost(close, sig_c, sym, cost_mult=0.0),
        "net_1x": _signal_metrics_cost(close, sig_c, sym, cost_mult=1.0),
        "net_2x": _signal_metrics_cost(close, sig_c, sym, cost_mult=2.0),
        "net_3x": _signal_metrics_cost(close, sig_c, sym, cost_mult=3.0),
    }

    result["latest"] = {
        "ma_200": int(sig_a.iloc[-1]) if len(sig_a) > 0 else 0,
        "mom_3m": int(sig_b.iloc[-1]) if len(sig_b) > 0 else 0,
        "blended": round(float(sig_c.iloc[-1]), 2) if len(sig_c) > 0 else 0,
    }

    return result


# ── survival tables ──────────────────────────────────────────────────────────


def survival_table(
    all_results: dict[str, dict[str, Any]],
    tf: str,
) -> dict[str, Any]:
    """Count how many symbols survive (positive net return) at each cost level."""
    models = ["ma_200", "mom_3m", "blended"]
    cost_levels = ["gross", "net_1x", "net_2x", "net_3x"]
    cost_labels = ["0x (gross)", "1x cost", "2x cost", "3x cost"]

    table: dict[str, Any] = {"timeframe": tf}
    for model in models:
        counts: dict[str, int] = {}
        for level, label in zip(cost_levels, cost_labels):
            survivors = 0
            for key, data in all_results.items():
                if not key.endswith(f"_{tf}"):
                    continue
                if "error" in data:
                    continue
                net = data.get(model, {}).get(level, {}).get("total_return", 0)
                if net > 0:
                    survivors += 1
            counts[label] = survivors
        table[model] = counts

    return table


def survival_detail(
    all_results: dict[str, dict[str, Any]],
    tf: str,
    model: str,
) -> list[dict[str, Any]]:
    """Per-symbol net return at each cost level."""
    cost_levels = ["gross", "net_1x", "net_2x", "net_3x"]
    rows: list[dict[str, Any]] = []
    for key, data in sorted(all_results.items()):
        if not key.endswith(f"_{tf}"):
            continue
        if "error" in data:
            continue
        m = data.get(model, {})
        row = {
            "symbol": data["symbol"],
            "flips": m.get("gross", {}).get("signal_changes", 0),
            "cost_bps_rt": round(cost_per_trade_bps(data["symbol"]), 2),
        }
        for level in cost_levels:
            row[level] = m.get(level, {}).get("total_return", 0)
        row["survives_2x"] = m.get("net_2x", {}).get("total_return", 0) > 0
        rows.append(row)
    return rows


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    timeframes = ["D1"]
    all_results: dict[str, dict[str, Any]] = {}

    for tf in timeframes:
        print(f"\n{'='*80}")
        print(f"  {tf} TREND BASELINES — COST-AWARE")
        print(f"{'='*80}")
        header = (
            f"{'Symbol':12s} {'Bars':>5s} {'Flips':>5s} "
            f"{'Gross':>8s} {'Net 1x':>8s} {'Net 2x':>8s} {'Net 3x':>8s} "
            f"{'Surv':>5s} {'Latest'}"
        )
        print(header)
        print("-" * 80)

        for sym in RESEARCH_UNIVERSE:
            result = run_baselines(sym, tf)
            key = f"{sym}_{tf}"
            all_results[key] = result

            if "error" in result:
                print(f"  {sym:10s}  SKIP: {result['error'][:60]}")
                continue

            # Show MA200 (the best model from previous run)
            m = result["ma_200"]
            gross = m["gross"]["total_return"]
            n1 = m["net_1x"]["total_return"]
            n2 = m["net_2x"]["total_return"]
            n3 = m["net_3x"]["total_return"]
            flips = m["gross"]["signal_changes"]
            surv = "YES" if n2 > 0 else "no"
            latest = result.get("latest", {})
            latest_str = f"ma={latest.get('ma_200',0)}"

            print(
                f"  {sym:10s} {result['bars']:>5d} {flips:>5d} "
                f"{gross:>8.4f} {n1:>8.4f} {n2:>8.4f} {n3:>8.4f} "
                f"{surv:>5s}  {latest_str}"
            )

    # ── survival summary ──────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print("  DOUBLE-COST SURVIVAL (D1)")
    print(f"{'='*80}")

    surv = survival_table(all_results, "D1")
    cost_labels = ["0x (gross)", "1x cost", "2x cost", "3x cost"]
    header2 = f"{'Model':12s} " + " ".join(f"{l:>12s}" for l in cost_labels)
    print(header2)
    print("-" * 65)

    for model in ["ma_200", "mom_3m", "blended"]:
        counts = surv[model]
        row = f"{model:12s} " + " ".join(f"{counts[l]:>12d}" for l in cost_labels)
        print(row)

    # ── per-symbol detail (MA200, best model) ─────────────────────────────
    print(f"\n{'='*80}")
    print("  MA200 PER-SYMBOL COST STRESS (D1)")
    print(f"{'='*80}")
    detail = survival_detail(all_results, "D1", "ma_200")
    detail_header = (
        f"{'Symbol':12s} {'Flips':>5s} {'Cost bp':>7s} "
        f"{'Gross':>8s} {'1x':>8s} {'2x':>8s} {'3x':>8s} {'Surv2x':>7s}"
    )
    print(detail_header)
    print("-" * 80)
    for row in detail:
        surv2x = "YES" if row["survives_2x"] else "no"
        print(
            f"  {row['symbol']:10s} {row['flips']:>5d} {row['cost_bps_rt']:>7.2f} "
            f"{row['gross']:>8.4f} {row['net_1x']:>8.4f} {row['net_2x']:>8.4f} "
            f"{row['net_3x']:>8.4f} {surv2x:>7s}"
        )

    # ── blended detail ────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print("  BLENDED PER-SYMBOL COST STRESS (D1)")
    print(f"{'='*80}")
    detail_b = survival_detail(all_results, "D1", "blended")
    print(detail_header)
    print("-" * 80)
    for row in detail_b:
        surv2x = "YES" if row["survives_2x"] else "no"
        print(
            f"  {row['symbol']:10s} {row['flips']:>5d} {row['cost_bps_rt']:>7.2f} "
            f"{row['gross']:>8.4f} {row['net_1x']:>8.4f} {row['net_2x']:>8.4f} "
            f"{row['net_3x']:>8.4f} {surv2x:>7s}"
        )

    # ── write report ─────────────────────────────────────────────────────
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_tf": "M15",
        "target_tfs": ["D1"],
        "cost_model": {
            "slippage_bps": SLIPPAGE_BPS,
            "spread_bps_by_symbol": COST_BPS,
            "swap_bps_daily_by_symbol": SWAP_BPS_DAILY,
        },
        "results": all_results,
        "survival_d1": surv,
        "ma200_detail_d1": detail,
        "blended_detail_d1": detail_b,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nReport written to {REPORT_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
