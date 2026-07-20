"""Multi-symbol replay benchmark with monthly PnL projection."""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from core.history_manager import HistoryManager
from core.performance_projection import (
    DEFAULT_MIN_CLOSED_TRADES,
    DEFAULT_MIN_REPLAY_WINDOW_DAYS,
    PROJECTION_CAPITAL_USD,
    TARGET_MONTHLY_PNL_USD,
    benchmark_meets_target,
    project_monthly_pnl,
    replay_window_days,
)
from core.account_mode import performance_gates_active
from core.replay_engine import run_portfolio_replay
from core.utils import load_config, utc_now_iso

BENCHMARK_EXECUTION_KEYS = frozenset({
    "mode",
    "starting_cash",
    "live_trading_enabled",
    "mt5_trading_enabled",
})


def sync_performance_gates(config: dict[str, Any]) -> dict[str, Any]:
    """Apply profitability-first gates from performance section when plan is active."""
    perf = config.get("performance")
    if not perf or not performance_gates_active(config):
        return config

    capital = float(perf.get("projection_capital_usd", PROJECTION_CAPITAL_USD))
    config.setdefault("execution", {})["starting_cash"] = capital
    signals = config.setdefault("signals", {})
    trading = config.setdefault("trading", {})
    filters = config.setdefault("filters", {})
    intelligence = config.setdefault("intelligence", {})
    session = config.setdefault("session_scoring", {})
    quant = config.setdefault("quant", {})
    risk = config.setdefault("risk", {})

    signals["default_risk_percent"] = float(perf.get("default_risk_percent", signals.get("default_risk_percent", 0.35)))
    signals["min_confidence"] = int(perf.get("min_confidence", signals.get("min_confidence", 62)))
    signals["min_risk_reward"] = float(perf.get("min_risk_reward", signals.get("min_risk_reward", 1.15)))

    trading["aggressive_mode"] = False
    trading["max_session_trades_per_symbol"] = int(
        perf.get("max_session_trades_per_symbol", trading.get("max_session_trades_per_symbol", 12))
    )
    trading.setdefault("dynamic_entries", {})["enabled"] = False
    strategy_entries = trading.setdefault("strategy_entries", {})
    strategy_entries["enabled"] = bool(perf.get("strategy_entries_enabled", strategy_entries.get("enabled", True)))
    strategy_entries["use_limit_orders"] = False
    strategy_entries["market_if_within_atr"] = 99.0

    filters["min_volume_ratio"] = float(perf.get("min_volume_ratio", filters.get("min_volume_ratio", 0.35)))
    intelligence["regime_veto_enabled"] = bool(perf.get("regime_veto_enabled", True))
    intelligence["trend_structure_veto_enabled"] = bool(perf.get("trend_structure_veto_enabled", True))

    session["enabled"] = True
    session["min_trade_score"] = int(perf.get("min_trade_score", session.get("min_trade_score", 72)))
    session["min_trade_score_aggressive"] = int(
        perf.get("min_trade_score_aggressive", session.get("min_trade_score_aggressive", 72))
    )

    quant["require_top_ranked_setup"] = bool(perf.get("require_top_ranked_setup", True))
    quant["min_rank_win_rate"] = float(perf.get("min_rank_win_rate", quant.get("min_rank_win_rate", 40)))
    quant["ranking_flex_min_win_rate"] = float(
        perf.get("ranking_flex_min_win_rate", quant.get("ranking_flex_min_win_rate", 48))
    )

    risk["max_symbol_exposure_usd"] = float(perf.get("max_symbol_exposure_usd", capital * 0.45))
    risk["max_total_exposure_usd"] = float(perf.get("max_total_exposure_usd", capital * 0.85))
    risk["unlimited_trades"] = False
    return config


def benchmark_config_diff(base: dict[str, Any], benchmarked: dict[str, Any]) -> dict[str, Any]:
    """Return sections/keys that differ between base and benchmark configs."""
    diff: dict[str, Any] = {}
    all_keys = set(base) | set(benchmarked)
    for key in all_keys:
        if key == "execution":
            exec_diff = {}
            base_exec = base.get("execution", {})
            bench_exec = benchmarked.get("execution", {})
            for ek in set(base_exec) | set(bench_exec):
                if base_exec.get(ek) != bench_exec.get(ek):
                    exec_diff[ek] = {"base": base_exec.get(ek), "benchmark": bench_exec.get(ek)}
            if exec_diff:
                diff["execution"] = exec_diff
        elif base.get(key) != benchmarked.get(key):
            diff[key] = {"base": base.get(key), "benchmark": benchmarked.get(key)}
    return diff


def m5_bar_counts(config: dict[str, Any], symbols: list[str]) -> dict[str, int]:
    """Row counts in on-disk M5 parquet per symbol."""
    history = HistoryManager(config)
    counts: dict[str, int] = {}
    for symbol in symbols:
        df = history.load(symbol, "M5")
        counts[symbol] = len(df) if df is not None and not df.empty else 0
    return counts


def parquet_calendar_span(config: dict[str, Any], symbols: list[str]) -> dict[str, Any]:
    """Calendar span (days) from first/last M5 bar timestamps."""
    history = HistoryManager(config)
    spans: dict[str, Any] = {}
    for symbol in symbols:
        df = history.load(symbol, "M5")
        if df is None or len(df) < 2:
            spans[symbol] = {"bars": 0, "calendar_days": 0.0, "first": None, "last": None}
            continue
        df = df.sort_values("time")
        first = pd.to_datetime(df["time"].iloc[0])
        last = pd.to_datetime(df["time"].iloc[-1])
        days = (last - first).total_seconds() / 86400.0
        spans[symbol] = {
            "bars": len(df),
            "calendar_days": round(days, 4),
            "first": str(df["time"].iloc[0]),
            "last": str(df["time"].iloc[-1]),
        }
    return spans


def estimate_bars_replayed(m5_rows: int, max_bars: int, step: int, min_bars: int) -> int:
    """Bars walked by ReplayEngine for given parquet depth."""
    if m5_rows <= min_bars or step <= 0:
        return 0
    start_idx = max(min_bars, m5_rows - max_bars)
    return len(range(start_idx, m5_rows, step))


def resolve_benchmark_replay_params(
    config: dict[str, Any],
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Pick max_bars/step from on-disk history (min M5 rows across symbols)."""
    symbols = symbols or list(config["mt5"]["symbols"])
    perf = config.get("performance", {})
    step = int(perf.get("replay_step", config.get("replay", {}).get("bars_per_step", 1)))
    cap = int(perf.get("replay_max_bars", config.get("replay", {}).get("max_bars", 50000)))
    min_bars = int(config.get("features", {}).get("min_bars_required", 20))
    counts = m5_bar_counts(config, symbols)
    min_rows = min(counts.values()) if counts else 0
    max_bars = min(min_rows, cap) if min_rows > min_bars else max(min_rows, 1)
    estimated = estimate_bars_replayed(min_rows, max_bars, step, min_bars) if min_rows else 0
    return {
        "symbols": symbols,
        "max_bars": max_bars,
        "step": step,
        "m5_bar_counts": counts,
        "min_m5_rows": min_rows,
        "estimated_bars_replayed": estimated,
        "estimated_replay_window_days": round(replay_window_days(estimated, step), 4),
    }


def apply_benchmark_config(config: dict[str, Any]) -> dict[str, Any]:
    """Replay-safety overrides only — gates come from load_config + config.yaml."""
    cfg = copy.deepcopy(config)
    perf = cfg.get("performance", {})
    capital = float(perf.get("projection_capital_usd", PROJECTION_CAPITAL_USD))
    cfg["execution"]["mode"] = "paper"
    cfg["execution"]["starting_cash"] = capital
    cfg["execution"]["live_trading_enabled"] = False
    cfg["execution"]["mt5_trading_enabled"] = False
    return cfg


def run_multi_symbol_benchmark(
    config: dict[str, Any] | None = None,
    *,
    symbols: list[str] | None = None,
    max_bars: int | None = None,
    step: int | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Shared-equity portfolio replay across symbols, then project monthly PnL."""
    base = load_config() if config is None else config
    cfg = apply_benchmark_config(base)
    perf = cfg.get("performance", {})
    log = logger or logging.getLogger("performance_benchmark")

    symbols = symbols or list(cfg["mt5"]["symbols"])
    resolved = resolve_benchmark_replay_params(cfg, symbols)
    max_bars = max_bars or resolved["max_bars"]
    step = step or resolved["step"]
    capital = float(cfg["execution"]["starting_cash"])
    target = float(perf.get("target_monthly_pnl_usd", TARGET_MONTHLY_PNL_USD))
    min_window_days = float(perf.get("min_replay_window_days", DEFAULT_MIN_REPLAY_WINDOW_DAYS))
    min_closed = int(perf.get("min_closed_trades", DEFAULT_MIN_CLOSED_TRADES))
    history_span = parquet_calendar_span(cfg, symbols)

    log.info(
        "Portfolio benchmark replay (max_bars=%d step=%d symbols=%s est_bars=%d)",
        max_bars,
        step,
        symbols,
        resolved["estimated_bars_replayed"],
    )
    portfolio = run_portfolio_replay(cfg, symbols, max_bars=max_bars, step=step, logger=log)
    symbol_results = portfolio["symbol_results"]
    for row in symbol_results:
        log.info(
            "  %s: pnl=%.2f trades=%d wr=%.1f%%",
            row.get("symbol"),
            row.get("pnl_total", 0),
            row.get("trades_closed", 0),
            row.get("win_rate_pct", 0),
        )

    projection = project_monthly_pnl(
        float(portfolio["pnl_total"]),
        int(portfolio["bars_replayed"]),
        step,
        replay_starting_cash=capital,
        target_capital=capital,
        target_monthly_pnl=target,
    )
    per_symbol = []
    for row in symbol_results:
        sym_proj = project_monthly_pnl(
            float(row.get("pnl_total", 0)),
            int(portfolio["bars_replayed"]),
            step,
            replay_starting_cash=capital,
            target_capital=capital,
            target_monthly_pnl=target,
        )
        per_symbol.append({**row, "projected_monthly_pnl": sym_proj["projected_monthly_pnl"]})

    projection = {
        **projection,
        "symbols": per_symbol,
        "combined_pnl_total": portfolio["pnl_total"],
        "combined_bars_replayed": portfolio["bars_replayed"],
        "portfolio_mode": "shared_equity",
    }
    eligibility = benchmark_meets_target(
        projection,
        trades_closed=int(portfolio["trades_closed"]),
        min_replay_window_days=min_window_days,
        min_closed_trades=min_closed,
        target_monthly_pnl=target,
    )

    return {
        "timestamp": utc_now_iso(),
        "symbols_tested": symbols,
        "replay_max_bars": max_bars,
        "replay_step": step,
        "replay_params": resolved,
        "history_span": history_span,
        "capital_base_usd": capital,
        "target_monthly_pnl_usd": target,
        "portfolio": portfolio,
        "symbol_results": symbol_results,
        "projection": projection,
        "eligibility": eligibility,
        "coverage_ok": eligibility["coverage_ok"],
        "pnl_ok": eligibility["pnl_ok"],
        "meets_target": eligibility["meets_target"],
        "projected_monthly_pnl": projection.get("projected_monthly_pnl", 0),
    }


def format_benchmark_report(result: dict[str, Any]) -> str:
    """Human-readable benchmark output for logs."""
    lines = [
        "=== MT5 Quant Performance Benchmark ===",
        f"capital_base_usd: {result.get('capital_base_usd')}",
        f"target_monthly_pnl_usd: {result.get('target_monthly_pnl_usd')}",
        f"projected_monthly_pnl: {result.get('projected_monthly_pnl')}",
        f"meets_target: {result.get('meets_target')}",
        f"coverage_ok: {result.get('coverage_ok')}",
        f"pnl_ok: {result.get('pnl_ok')}",
        "",
        "Per-symbol replay:",
    ]
    for row in result.get("projection", {}).get("symbols", []):
        lines.append(
            f"  {row.get('symbol')}: pnl={row.get('pnl_total')} "
            f"trades={row.get('trades_closed')} wr={row.get('win_rate_pct')}% "
            f"proj_monthly={row.get('projected_monthly_pnl')}"
        )
    proj = result.get("projection", {})
    lines.extend([
        "",
        f"combined_pnl_total: {proj.get('combined_pnl_total')}",
        f"combined_bars_replayed: {proj.get('combined_bars_replayed')}",
        f"replay_window_days: {proj.get('replay_window_days')}",
        f"daily_pnl: {proj.get('daily_pnl')}",
        f"gap_to_target: {proj.get('gap_to_target')}",
    ])
    elig = result.get("eligibility", {})
    if elig:
        lines.extend([
            "",
            f"min_replay_window_days: {elig.get('min_replay_window_days')}",
            f"min_closed_trades: {elig.get('min_closed_trades')}",
            f"trades_closed: {elig.get('trades_closed')}",
            f"window_ok: {elig.get('window_ok')}",
            f"trades_ok: {elig.get('trades_ok')}",
        ])
    return "\n".join(lines)


def write_benchmark_log(result: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **result,
        "report": format_benchmark_report(result),
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    path.with_suffix(".txt").write_text(payload["report"], encoding="utf-8")
    return path