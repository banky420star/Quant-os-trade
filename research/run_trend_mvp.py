#!/usr/bin/env python
"""D1/H4 Trend MVP — cost-aware baselines with double-cost stress survival.

Reads real D1/H4 Parquet files from MT5 (backfilled by history_loop).
Falls back to M15 resampling if D1/H4 files are not yet available.
Runs MA200, 3M momentum, and blended 1/3/6/12-month models.
Deducts estimated spread, swap, and slippage per signal flip,
then stress-tests at 1x, 2x, and 3x costs.
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


def _read_ohlc(sym: str, tf: str) -> pd.DataFrame:
    """Read OHLC data for a symbol/timeframe.

    Prefers real D1/H4 Parquet files from MT5.  Falls back to
    M15 resampling if the direct file doesn't exist yet.
    """
    # Try direct D1/H4 file first (backfilled by history_loop)
    direct = HISTORY_DIR / f"{sym}_{tf}.parquet"
    if direct.exists():
        df = pd.read_parquet(direct)
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], utc=True)
            df = df.set_index("time")
        return df.sort_index()

    # Fall back to M15 resampling
    m15 = HISTORY_DIR / f"{sym}_M15.parquet"
    if not m15.exists():
        raise FileNotFoundError(f"No data for {sym} {tf} (neither {direct.name} nor {m15.name})")

    df = pd.read_parquet(m15)
    # Resample M15 to the target timeframe
    if "time" in df.columns:
        df = df.copy()
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.set_index("time")
    elif not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("DataFrame must have a DatetimeIndex or 'time' column")

    rule = {"D1": "D", "H4": "4h"}[tf]
    agg_map = {"open": "first", "high": "max", "low": "min", "close": "last"}
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
    try:
        daily = _read_ohlc(sym, tf)
    except FileNotFoundError as e:
        return {"error": str(e)}
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


# ── cluster definitions ─────────────────────────────────────────────────────

CLUSTERS: dict[str, list[str]] = {
    "equity_indices": ["US500m", "US30m", "NAS100m", "UK100m", "FR40m", "JP225m"],
    "fx_usd_majors": ["EURUSDm", "GBPUSDm", "AUDUSDm", "USDCHFm", "USDJPYm"],
    "metals": ["XAUUSDm"],
    "energy": ["USOILm"],
    "crypto": ["BTCUSDm"],
}

CLUSTER_CAPS: dict[str, float] = {
    "equity_indices": 0.35,
    "fx_usd_majors": 0.35,
    "metals": 0.15,
    "energy": 0.15,
    "crypto": 0.10,
}

SINGLE_SYMBOL_CAP: float = 0.15
VOL_TARGET: float = 0.15
VOL_LOOKBACK: int = 63


def _symbol_cluster(symbol: str) -> str:
    for name, members in CLUSTERS.items():
        if symbol in members:
            return name
    return "other"


# ── portfolio construction ──────────────────────────────────────────────────


def _per_symbol_returns(
    close: pd.Series,
    signal: pd.Series,
    symbol: str,
    *,
    cost_mult: float = 1.0,
) -> pd.Series:
    """Compute net daily returns for one symbol given a signal series."""
    ret = close.pct_change()
    gross = (ret * signal.shift(1)).fillna(0)

    tc_bps = cost_per_trade_bps(symbol) * cost_mult
    tc_decimal = tc_bps / 10_000
    swap_bps = daily_swap_bps(symbol) * cost_mult / 10_000

    cost_series = pd.Series(0.0, index=gross.index)
    sig_vals = signal.values if isinstance(signal, pd.Series) else signal
    for i in range(1, len(signal)):
        if sig_vals[i] != sig_vals[i - 1]:
            idx = signal.index[i]
            if idx in cost_series.index:
                cost_series.loc[idx] = tc_decimal

    swap_daily = signal.abs().shift(1).fillna(0) * swap_bps
    swap_daily = swap_daily.reindex(cost_series.index).fillna(0)

    return gross - cost_series - swap_daily


def build_portfolio(
    all_results: dict[str, dict[str, Any]],
    model: str,
    *,
    cost_mult: float = 1.0,
) -> dict[str, Any]:
    """Build a vol-scaled, cluster-capped portfolio from per-symbol signals.

    Steps:
      1. Recompute per-symbol signals and net daily returns.
      2. Compute rolling annualised vol for each symbol.
      3. Vol-scale each symbol to contribute equal risk (vol_target / vol_i).
      4. Apply cluster caps (35% equity, 35% FX, 15% others).
      5. Cap single-symbol weight at 15%.
      6. Combine into portfolio equity curve.
    """
    tf = all_results.get(list(all_results.keys())[0], {}).get("timeframe", "D1")

    # Step 1: recompute signals and net returns for all symbols
    sym_returns: dict[str, pd.Series] = {}
    sym_vols: dict[str, float] = {}
    sym_weights_final: dict[str, float] = {}

    for key, data in all_results.items():
        sym = data.get("symbol", "")
        if not sym or "error" in data:
            continue

        try:
            daily = _read_ohlc(sym, tf)
        except FileNotFoundError:
            continue
        close = daily["close"]
        if len(close) < 252:
            continue

        # Recompute signal
        if model == "ma_200":
            sig = moving_average_signal(close, period=min(200, len(close) // 2))
        elif model == "mom_3m":
            lookback = {"D1": 63, "H4": 90}.get(tf, 63)
            sig = time_series_momentum_signal(close, lookback=lookback)
        elif model == "blended":
            horizons = {"D1": (21, 63, 126, 252), "H4": (30, 90, 180, 360)}.get(tf, (21, 63, 126, 252))
            sig = blended_momentum_signal(close, horizons=horizons, threshold=0.0)
        else:
            continue

        net_ret = _per_symbol_returns(close, sig, sym, cost_mult=cost_mult)
        sym_returns[sym] = net_ret

        # Rolling annualised vol (last VOL_LOOKBACK days)
        if len(net_ret) >= VOL_LOOKBACK:
            sym_vols[sym] = float(net_ret.tail(VOL_LOOKBACK).std() * np.sqrt(252))
        else:
            sym_vols[sym] = float(net_ret.std() * np.sqrt(252))

    if not sym_returns:
        return {"error": "no valid symbols for portfolio"}

    # Align to common date index
    common_dates = sorted(set.intersection(*[set(r.index) for r in sym_returns.values()]))
    if len(common_dates) < 60:
        return {"error": f"only {len(common_dates)} common dates"}

    aligned: dict[str, pd.Series] = {
        sym: r.reindex(common_dates).fillna(0)
        for sym, r in sym_returns.items()
    }

    # Step 2-3: vol-scale weights
    raw_weights: dict[str, float] = {}
    for sym in aligned:
        vol = sym_vols.get(sym, 0)
        if vol > 0:
            raw_weights[sym] = VOL_TARGET / vol
        else:
            raw_weights[sym] = 0.0

    # Normalize so total raw weight = 1.0
    total_raw = sum(raw_weights.values())
    if total_raw > 0:
        raw_weights = {s: w / total_raw for s, w in raw_weights.items()}

    # Step 4-5: apply single-symbol cap, then cluster caps
    weights = dict(raw_weights)

    # Single-symbol cap
    for sym in weights:
        if weights[sym] > SINGLE_SYMBOL_CAP:
            weights[sym] = SINGLE_SYMBOL_CAP

    # Cluster caps — process symbols in descending weight order
    cluster_totals: dict[str, float] = {c: 0.0 for c in CLUSTERS}
    cluster_totals["other"] = 0.0

    for sym in sorted(weights, key=lambda s: -weights[s]):
        cluster = _symbol_cluster(sym)
        cap = CLUSTER_CAPS.get(cluster, SINGLE_SYMBOL_CAP)
        if cluster_totals[cluster] + weights[sym] > cap:
            weights[sym] = max(0.0, cap - cluster_totals[cluster])
        cluster_totals[cluster] += weights[sym]

    # Do NOT re-normalize after caps — clipping trims exposure,
    # it should not inflate other weights back above their caps.
    # The portfolio may be less than 100% invested; that's correct.
    sym_weights_final = dict(weights)

    # Step 6: combined daily return
    combined = pd.Series(0.0, index=common_dates)
    for sym, w in weights.items():
        if w > 0 and sym in aligned:
            combined += w * aligned[sym]

    # Portfolio metrics
    metrics = portfolio_report(combined)

    # Cluster exposure breakdown
    cluster_exposure: dict[str, float] = {}
    for sym, w in weights.items():
        c = _symbol_cluster(sym)
        cluster_exposure[c] = cluster_exposure.get(c, 0.0) + w

    # Per-symbol final weights (top-level)
    symbol_weights = {s: round(w, 4) for s, w in sorted(weights.items(), key=lambda x: -x[1])}

    return {
        **metrics,
        "model": model,
        "cost_mult": cost_mult,
        "n_symbols": len(aligned),
        "vol_target": VOL_TARGET,
        "cluster_exposure": {c: round(w, 4) for c, w in sorted(cluster_exposure.items(), key=lambda x: -x[1])},
        "symbol_weights": symbol_weights,
        "cluster_caps_applied": CLUSTER_CAPS,
        "single_symbol_cap": SINGLE_SYMBOL_CAP,
        "equity_curve": [round(float(x), 6) for x in (1 + combined).cumprod()],
    }


def portfolio_report(
    portfolio_series: pd.Series,
) -> dict[str, Any]:
    """Report Sharpe, MaxDD, annual return from a daily return series."""
    if len(portfolio_series) < 20:
        return {"error": f"only {len(portfolio_series)} daily returns"}

    cum = (1 + portfolio_series).cumprod()
    total = float(cum.iloc[-1] - 1)
    dd = (cum / cum.cummax() - 1).min()
    ann = float((1 + total) ** (252 / len(portfolio_series)) - 1)
    sharpe = float(portfolio_series.mean() / portfolio_series.std() * np.sqrt(252)) if portfolio_series.std() > 0 else 0
    win_days = float((portfolio_series > 0).mean())

    return {
        "n_days": len(portfolio_series),
        "total_return": round(total, 6),
        "annual_return": round(ann, 6),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(float(dd), 4),
        "win_day_pct": round(win_days, 4),
        "avg_daily_return": round(float(portfolio_series.mean()), 8),
        "vol_daily": round(float(portfolio_series.std()), 6),
        "vol_annual": round(float(portfolio_series.std() * np.sqrt(252)), 4),
    }


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

    # ── portfolio section ─────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print("  COMBINED PORTFOLIO (vol-scaled + cluster-capped, 1x costs)")
    print(f"{'='*80}")
    header_pf = f"{'Model':12s} {'#Sym':>5s} {'AnnRet':>8s} {'Sharpe':>8s} {'MaxDD':>8s} {'WinDay%':>7s} {'VolAnn':>8s}"
    print(header_pf)
    print("-" * 65)

    portfolios: dict[str, dict[str, Any]] = {}
    for model in ["ma_200", "mom_3m", "blended"]:
        pf = build_portfolio(all_results, model, cost_mult=1.0)
        portfolios[model] = pf
        if "error" in pf:
            print(f"  {model:12s}  ERROR: {pf['error'][:50]}")
            continue
        print(
            f"  {model:12s} {pf['n_symbols']:>5d} "
            f"{pf['annual_return']:>8.4f} {pf['sharpe']:>8.4f} {pf['max_drawdown']:>8.4f} "
            f"{pf['win_day_pct']:>7.4f} {pf['vol_annual']:>8.4f}"
        )

    # ── cluster exposure breakdown ────────────────────────────────────────
    for model, pf in portfolios.items():
        if "error" in pf:
            continue
        print(f"\n  {model} cluster exposure:")
        for cluster, w in pf.get("cluster_exposure", {}).items():
            cap = CLUSTER_CAPS.get(cluster, SINGLE_SYMBOL_CAP)
            bar_in = int(w * 40)
            bar_out = int(cap * 40)
            print(f"    {cluster:20s} {w:>6.2%} / cap {cap:>5.0%}  [{'#' * bar_in}{'.' * max(0, bar_out - bar_in)}]")

    # ── per-symbol weights (top 8) ────────────────────────────────────────
    for model, pf in portfolios.items():
        if "error" in pf:
            continue
        syms = list(pf.get("symbol_weights", {}).items())[:8]
        print(f"\n  {model} top-8 symbol weights:")
        for sym, w in syms:
            bar = int(w * 50)
            print(f"    {sym:12s} {w:>6.2%}  [{'#' * bar}]")

    # ── write report ─────────────────────────────────────────────────────
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_tf": "D1_real",  # real D1 from MT5, M15 fallback if missing
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
        "portfolio": portfolios,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nReport written to {REPORT_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
