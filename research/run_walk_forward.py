#!/usr/bin/env python
"""Purged portfolio walk-forward validation for daily trend strategies."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.costs.trend_portfolio import (
    VOL_LOOKBACK,
    capped_vol_weights,
    net_returns,
    portfolio_metrics,
)
from research.data.catalog import RESEARCH_UNIVERSE
from research.strategies.trend_baseline import (
    blended_momentum_signal,
    moving_average_signal,
    time_series_momentum_signal,
)

DATA_DIR = Path("research/data")
FALLBACK_DIR = Path("data/history")
REPORT_PATH = Path("data/walk_forward_report.json")


def _read_close(symbol: str) -> pd.Series:
    """Load sorted UTC D1 close history for one symbol."""
    path = DATA_DIR / f"{symbol}_D1.parquet"
    if not path.exists():
        path = FALLBACK_DIR / f"{symbol}_D1.parquet"
    if not path.exists():
        raise FileNotFoundError(str(path))
    frame = pd.read_parquet(path)
    if "time" in frame.columns:
        frame["time"] = pd.to_datetime(frame["time"], utc=True)
        frame = frame.set_index("time")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError(f"{path} has no datetime index")
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    else:
        frame.index = frame.index.tz_convert("UTC")
    return frame["close"].sort_index().dropna()


def _generate_folds(
    earliest_start: pd.Timestamp,
    latest_end: pd.Timestamp,
    *,
    train_months: int = 12,
    test_months: int = 4,
    purge_days: int = 5,
    min_train_days: int = 200,
) -> list[dict[str, Any]]:
    """Create expanding folds separated by a purge interval."""
    folds: list[dict[str, Any]] = []
    test_start = earliest_start + pd.DateOffset(months=train_months)
    fold_number = 0
    while test_start + pd.DateOffset(months=test_months) <= latest_end:
        train_end = test_start - pd.Timedelta(days=purge_days)
        if (train_end - earliest_start).days >= min_train_days:
            folds.append(
                {
                    "fold": fold_number,
                    "train_start": earliest_start,
                    "train_end": train_end,
                    "test_start": test_start,
                    "test_end": min(
                        test_start + pd.DateOffset(months=test_months), latest_end
                    ),
                }
            )
            fold_number += 1
        test_start += pd.DateOffset(months=test_months)
    return folds


def _signal_for_fold(
    close_window: pd.Series,
    train_length: int,
    model: str,
) -> tuple[pd.Series, dict[str, Any]]:
    """Generate signals while selecting all parameters from training data only."""
    if model == "ma_200":
        period = min(200, max(train_length // 2, 2))
        return moving_average_signal(close_window, period=period), {
            "effective_period": period
        }
    if model == "mom_3m":
        return time_series_momentum_signal(close_window, lookback=63), {
            "lookback": 63
        }
    if model == "blended":
        horizons = (21, 63, 126, 252)
        return blended_momentum_signal(
            close_window, horizons=horizons, threshold=0.0
        ), {"horizons": horizons}
    raise ValueError(f"unknown model: {model}")


def _evaluate_fold(
    fold: dict[str, Any],
    all_closes: dict[str, pd.Series],
    model: str,
) -> dict[str, Any]:
    """Evaluate one fold with train-only vol estimates and test-only returns."""
    train_start = fold["train_start"]
    train_end = fold["train_end"]
    test_start = fold["test_start"]
    test_end = fold["test_end"]
    returns_by_symbol: dict[str, pd.Series] = {}
    volatilities: dict[str, float] = {}
    parameters: dict[str, Any] = {}

    for symbol, full_close in all_closes.items():
        if full_close.index.min() > train_start or full_close.index.max() < test_end:
            continue
        close_window = full_close.loc[train_start:test_end]
        training = full_close.loc[train_start:train_end]
        if len(training) < 200 or len(close_window.loc[test_start:test_end]) < 20:
            continue
        signal, model_parameters = _signal_for_fold(
            close_window, len(training), model
        )
        all_net_returns = net_returns(
            close_window, signal, symbol, cost_mult=1.0, timeframe="D1"
        )
        test_returns = all_net_returns.loc[test_start:test_end]
        if len(test_returns) < 20:
            continue
        returns_by_symbol[symbol] = test_returns
        training_returns = all_net_returns.loc[train_start:train_end]
        sample = training_returns.tail(VOL_LOOKBACK)
        volatilities[symbol] = float(sample.std() * np.sqrt(252))
        parameters[symbol] = model_parameters

    if len(returns_by_symbol) < 3:
        return {"fold": fold["fold"], "error": f"only {len(returns_by_symbol)} symbols"}

    common_index = next(iter(returns_by_symbol.values())).index
    for returns in returns_by_symbol.values():
        common_index = common_index.intersection(returns.index)
    if len(common_index) < 20:
        return {"fold": fold["fold"], "error": f"only {len(common_index)} common dates"}

    weights = capped_vol_weights(volatilities)
    combined = pd.Series(0.0, index=common_index)
    for symbol, weight in weights.items():
        combined += weight * returns_by_symbol[symbol].reindex(common_index).fillna(0.0)
    metrics = portfolio_metrics(combined, timeframe="D1")
    if "error" in metrics:
        return {"fold": fold["fold"], **metrics}
    return {
        "fold": fold["fold"],
        "train_start": str(train_start.date()),
        "train_end": str(train_end.date()),
        "test_start": str(test_start.date()),
        "test_end": str(test_end.date()),
        "n_symbols": len(weights),
        "n_days": metrics.pop("n_periods"),
        "weights": {key: round(value, 4) for key, value in weights.items()},
        "parameters": parameters,
        **metrics,
    }


def run_walk_forward(
    model: str,
    all_closes: dict[str, pd.Series],
    *,
    train_months: int = 12,
    test_months: int = 4,
    purge_days: int = 5,
) -> dict[str, Any]:
    """Run a model over folds built from the symbols' common history."""
    if not all_closes:
        return {"model": model, "error": "no close series"}
    earliest = max(series.index.min() for series in all_closes.values())
    latest = min(series.index.max() for series in all_closes.values())
    if earliest >= latest:
        return {"model": model, "error": "no common history"}
    folds = _generate_folds(
        earliest,
        latest,
        train_months=train_months,
        test_months=test_months,
        purge_days=purge_days,
    )
    results = [_evaluate_fold(fold, all_closes, model) for fold in folds]
    valid = [result for result in results if "error" not in result]
    errors = [result for result in results if "error" in result]
    if not valid:
        return {
            "model": model,
            "error": "no valid folds",
            "n_folds": len(folds),
            "fold_errors": errors,
        }

    sharpes = [result["sharpe"] for result in valid]
    returns = [result["total_return"] for result in valid]
    drawdowns = [result["max_drawdown"] for result in valid]
    return {
        "model": model,
        "common_start": str(earliest.date()),
        "common_end": str(latest.date()),
        "n_folds_total": len(folds),
        "n_folds_valid": len(valid),
        "n_folds_error": len(errors),
        "fold_errors": errors or None,
        "sharpe_stability": {
            "mean": round(float(np.mean(sharpes)), 4),
            "std": round(float(np.std(sharpes)), 4),
            "min": round(float(np.min(sharpes)), 4),
            "max": round(float(np.max(sharpes)), 4),
            "positive_folds": sum(value > 0 for value in sharpes),
            "positive_fraction": round(
                sum(value > 0 for value in sharpes) / len(sharpes), 3
            ),
        },
        "return_stability": {
            "mean": round(float(np.mean(returns)), 6),
            "std": round(float(np.std(returns)), 6),
            "min": round(float(np.min(returns)), 6),
            "max": round(float(np.max(returns)), 6),
            "positive_folds": sum(value > 0 for value in returns),
        },
        "drawdown_stability": {
            "mean": round(float(np.mean(drawdowns)), 4),
            "worst": round(float(np.min(drawdowns)), 4),
        },
        "folds": results,
    }


def main() -> int:
    """Load the research universe, run three models, and write the report."""
    all_closes: dict[str, pd.Series] = {}
    load_errors: dict[str, str] = {}
    for symbol in RESEARCH_UNIVERSE:
        try:
            all_closes[symbol] = _read_close(symbol)
        except (FileNotFoundError, ValueError) as exc:
            load_errors[symbol] = str(exc)
    reports = {
        model: run_walk_forward(model, all_closes)
        for model in ("ma_200", "mom_3m", "blended")
    }
    document = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "load_errors": load_errors or None,
        "models": reports,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(document, indent=2, default=str), encoding="utf-8")
    print(f"Report written to {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
