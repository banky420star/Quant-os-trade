#!/usr/bin/env python
"""Cost-aware D1 and H4 trend baseline research runner."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.costs.trend_portfolio import (
    CLUSTER_CAPS,
    COST_BPS,
    SINGLE_SYMBOL_CAP,
    SLIPPAGE_BPS,
    SWAP_BPS_DAILY,
    VOL_LOOKBACK,
    bars_per_day,
    capped_vol_weights,
    cost_per_trade_bps,
    daily_swap_bps,
    equivalent_horizons,
    net_returns,
    periods_per_year,
    portfolio_metrics,
    symbol_cluster,
)
from research.data.catalog import RESEARCH_UNIVERSE
from research.strategies.trend_baseline import (
    blended_momentum_signal,
    moving_average_signal,
    time_series_momentum_signal,
)

DATA_DIR = Path("research/data")
FALLBACK_DIR = Path("data/history")
REPORT_PATH = Path("data/trend_mvp_cost_report.json")


def _read_ohlc(symbol: str, timeframe: str) -> pd.DataFrame:
    """Load direct OHLC history or resample M15 as a fallback."""
    direct = DATA_DIR / f"{symbol}_{timeframe}.parquet"
    if not direct.exists():
        direct = FALLBACK_DIR / f"{symbol}_{timeframe}.parquet"
    if direct.exists():
        frame = pd.read_parquet(direct)
        if "time" in frame.columns:
            frame["time"] = pd.to_datetime(frame["time"], utc=True)
            frame = frame.set_index("time")
        if not isinstance(frame.index, pd.DatetimeIndex):
            raise ValueError(f"{direct} has no datetime index")
        return frame.sort_index()

    source = FALLBACK_DIR / f"{symbol}_M15.parquet"
    if not source.exists():
        raise FileNotFoundError(f"No data for {symbol} {timeframe}")
    frame = pd.read_parquet(source)
    if "time" in frame.columns:
        frame["time"] = pd.to_datetime(frame["time"], utc=True)
        frame = frame.set_index("time")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError(f"{source} has no datetime index")
    rule = {"D1": "1D", "H4": "4h"}[timeframe]
    aggregation: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    for column in ("volume", "tick_volume", "real_volume"):
        if column in frame.columns:
            aggregation[column] = "sum"
    if "spread" in frame.columns:
        aggregation["spread"] = "mean"
    return frame.resample(rule).agg(aggregation).dropna(subset=["open", "close"])


def _signal_metrics_cost(
    close: pd.Series,
    signal: pd.Series,
    symbol: str,
    *,
    cost_mult: float = 1.0,
    tf: str = "D1",
) -> dict[str, Any]:
    """Calculate timeframe-aware performance after costs."""
    strategy_returns = net_returns(
        close, signal, symbol, cost_mult=cost_mult, timeframe=tf
    ).dropna()
    metrics = portfolio_metrics(strategy_returns, timeframe=tf)
    if "error" in metrics:
        return {
            "n_bars": len(strategy_returns),
            "total_return": 0.0,
            "annual_return": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
        }
    flips = int(signal.reindex(close.index).fillna(0.0).diff().fillna(0.0).ne(0.0).sum())
    metrics.update(
        {
            "n_bars": metrics.pop("n_periods"),
            "signal_changes": flips,
            "cost_mult": cost_mult,
            "cost_bps_rt": round(cost_per_trade_bps(symbol) * cost_mult, 2),
            "swap_bps_daily": round(daily_swap_bps(symbol) * cost_mult, 3),
            "bars_per_day": bars_per_day(tf),
            "avg_signal": round(float(signal.mean()), 4),
            "long_pct": round(float((signal > 0).mean()), 4),
            "short_pct": round(float((signal < 0).mean()), 4),
            "flat_pct": round(float((signal == 0).mean()), 4),
        }
    )
    return metrics


def _model_signal(close: pd.Series, model: str, timeframe: str) -> tuple[pd.Series, dict[str, Any]]:
    """Generate a model signal and report its effective lookbacks."""
    if model == "ma_200":
        effective_period = min(200, max(len(close) // 2, 2))
        return moving_average_signal(close, period=effective_period), {
            "effective_period": effective_period
        }
    if model == "mom_3m":
        lookback = equivalent_horizons(timeframe, (63,))[0]
        return time_series_momentum_signal(close, lookback=lookback), {
            "lookback": lookback
        }
    if model == "blended":
        horizons = equivalent_horizons(timeframe)
        return blended_momentum_signal(close, horizons=horizons, threshold=0.0), {
            "horizons": horizons
        }
    raise ValueError(f"unknown model: {model}")


def run_baselines(symbol: str, timeframe: str) -> dict[str, Any]:
    """Evaluate MA, momentum, and blended trend baselines for one symbol."""
    try:
        frame = _read_ohlc(symbol, timeframe)
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}
    close = frame["close"].dropna()
    minimum = max(252, equivalent_horizons(timeframe)[-1])
    if len(close) < minimum:
        return {"error": f"only {len(close)} {timeframe} bars for {symbol} (need {minimum})"}

    result: dict[str, Any] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": len(close),
        "start": str(close.index.min()),
        "end": str(close.index.max()),
        "cost_model": {
            "spread_bps": COST_BPS.get(symbol, 2.0),
            "slippage_bps": SLIPPAGE_BPS,
            "swap_bps_daily": SWAP_BPS_DAILY.get(symbol, 0.05),
            "bars_per_day": bars_per_day(timeframe),
        },
    }
    latest: dict[str, float | int] = {}
    for model in ("ma_200", "mom_3m", "blended"):
        signal, parameters = _model_signal(close, model, timeframe)
        result[model] = {
            "parameters": parameters,
            "gross": _signal_metrics_cost(close, signal, symbol, cost_mult=0.0, tf=timeframe),
            "net_1x": _signal_metrics_cost(close, signal, symbol, cost_mult=1.0, tf=timeframe),
            "net_2x": _signal_metrics_cost(close, signal, symbol, cost_mult=2.0, tf=timeframe),
            "net_3x": _signal_metrics_cost(close, signal, symbol, cost_mult=3.0, tf=timeframe),
        }
        latest[model] = round(float(signal.iloc[-1]), 4) if len(signal) else 0
    result["latest"] = latest
    return result


def survival_table(all_results: dict[str, dict[str, Any]], tf: str) -> dict[str, Any]:
    """Count profitable symbols by model and cost multiplier."""
    levels = {
        "0x (gross)": "gross",
        "1x cost": "net_1x",
        "2x cost": "net_2x",
        "3x cost": "net_3x",
    }
    table: dict[str, Any] = {"timeframe": tf}
    for model in ("ma_200", "mom_3m", "blended"):
        table[model] = {
            label: sum(
                1
                for data in all_results.values()
                if data.get("timeframe") == tf
                and "error" not in data
                and data.get(model, {}).get(level, {}).get("total_return", 0.0) > 0
            )
            for label, level in levels.items()
        }
    return table


def survival_detail(
    all_results: dict[str, dict[str, Any]], tf: str, model: str
) -> list[dict[str, Any]]:
    """Return per-symbol stress metrics for a model and timeframe."""
    rows: list[dict[str, Any]] = []
    for data in all_results.values():
        if data.get("timeframe") != tf or "error" in data:
            continue
        metrics = data.get(model, {})
        rows.append(
            {
                "symbol": data["symbol"],
                "flips": metrics.get("gross", {}).get("signal_changes", 0),
                "cost_bps_rt": round(cost_per_trade_bps(data["symbol"]), 2),
                "gross": metrics.get("gross", {}).get("total_return", 0.0),
                "net_1x": metrics.get("net_1x", {}).get("total_return", 0.0),
                "net_2x": metrics.get("net_2x", {}).get("total_return", 0.0),
                "net_3x": metrics.get("net_3x", {}).get("total_return", 0.0),
                "survives_2x": metrics.get("net_2x", {}).get("total_return", 0.0) > 0,
            }
        )
    return sorted(rows, key=lambda row: row["symbol"])


def build_portfolio(
    all_results: dict[str, dict[str, Any]],
    model: str,
    *,
    cost_mult: float = 1.0,
    tf: str = "D1",
) -> dict[str, Any]:
    """Build a deterministic portfolio using only results for ``tf``."""
    returns_by_symbol: dict[str, pd.Series] = {}
    volatilities: dict[str, float] = {}
    for data in all_results.values():
        if data.get("timeframe") != tf or "error" in data:
            continue
        symbol = data.get("symbol")
        if not symbol:
            continue
        try:
            close = _read_ohlc(symbol, tf)["close"].dropna()
        except (FileNotFoundError, ValueError):
            continue
        try:
            signal, _ = _model_signal(close, model, tf)
        except ValueError:
            continue
        symbol_returns = net_returns(
            close, signal, symbol, cost_mult=cost_mult, timeframe=tf
        )
        returns_by_symbol[symbol] = symbol_returns
        lookback = int(VOL_LOOKBACK * bars_per_day(tf))
        sample = symbol_returns.tail(lookback)
        volatilities[symbol] = float(sample.std() * np.sqrt(periods_per_year(tf)))

    if not returns_by_symbol:
        return {"error": f"no valid symbols for {tf} portfolio"}
    common_index = next(iter(returns_by_symbol.values())).index
    for series in returns_by_symbol.values():
        common_index = common_index.intersection(series.index)
    if len(common_index) < 60:
        return {"error": f"only {len(common_index)} common {tf} bars"}

    weights = capped_vol_weights(volatilities)
    combined = pd.Series(0.0, index=common_index)
    for symbol, weight in weights.items():
        combined += weight * returns_by_symbol[symbol].reindex(common_index).fillna(0.0)
    metrics = portfolio_metrics(combined, timeframe=tf)
    if "error" in metrics:
        return metrics
    cluster_exposure: dict[str, float] = {}
    for symbol, weight in weights.items():
        cluster = symbol_cluster(symbol)
        cluster_exposure[cluster] = cluster_exposure.get(cluster, 0.0) + weight
    return {
        **metrics,
        "timeframe": tf,
        "model": model,
        "cost_mult": cost_mult,
        "n_symbols": len(weights),
        "cluster_exposure": {key: round(value, 4) for key, value in cluster_exposure.items()},
        "symbol_weights": {key: round(value, 4) for key, value in weights.items()},
        "cluster_caps_applied": CLUSTER_CAPS,
        "single_symbol_cap": SINGLE_SYMBOL_CAP,
        "equity_curve": [round(float(value), 6) for value in (1.0 + combined).cumprod()],
    }


def portfolio_report(portfolio_series: pd.Series, tf: str = "D1") -> dict[str, Any]:
    """Compatibility wrapper for timeframe-aware portfolio metrics."""
    return portfolio_metrics(portfolio_series, timeframe=tf)


def main() -> int:
    """Run all symbol and timeframe baselines and write a JSON report."""
    timeframes = ("D1", "H4")
    all_results: dict[str, dict[str, Any]] = {}
    for timeframe in timeframes:
        for symbol in RESEARCH_UNIVERSE:
            all_results[f"{symbol}_{timeframe}"] = run_baselines(symbol, timeframe)

    details = {
        model: {
            timeframe: survival_detail(all_results, timeframe, model)
            for timeframe in timeframes
        }
        for model in ("ma_200", "mom_3m", "blended")
    }
    portfolios = {
        model: build_portfolio(all_results, model, cost_mult=1.0, tf="D1")
        for model in ("ma_200", "mom_3m", "blended")
    }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_tfs": list(timeframes),
        "results": all_results,
        "survival_by_tf": {
            timeframe: survival_table(all_results, timeframe)
            for timeframe in timeframes
        },
        "ma200_detail_by_tf": details["ma_200"],
        "blended_detail_by_tf": details["blended"],
        "portfolio": portfolios,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"Report written to {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
