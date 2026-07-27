"""Historical replay with news blackout + growth awareness for $30 profile.

Runs portfolio replay on staged parquet history, applies:
  - Aspect A: scheduled US news blackout at each bar's UTC time
  - Aspect B: macro event detection (NFP / FOMC / CPI)
  - Growth tracker: 20% daily target, 10% max daily loss, 30-day compound goal

Writes state/historical_news_replay.json for monitoring.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.positive_evolution import positive_evolution_active, refresh_positive_evolution
from core.profile_launcher import set_active_profile
from core.replay_engine import ReplayEngine, run_portfolio_replay
from core.utils import load_config, read_json_state, setup_logger, write_json_state
from quant.research.trade_log_cells import cell_expectancy_report

MICRO_SYMBOLS = ["XAUUSDm", "USOILm", "UK100m"]
LIVE_SPREAD_POINTS = {"XAUUSDm": 240.0, "USOILm": 20.0, "UK100m": 80.0}
STATE_FILE = "historical_news_replay.json"


def run_historical_news_replay(
    profile: str = "30",
    *,
    max_bars: int | None = None,
    step: int | None = None,
    refresh_evolution: bool = True,
) -> dict[str, Any]:
    os.environ["MT5_QUANT_PROFILE"] = profile
    set_active_profile(profile)
    config = load_config()
    logger = setup_logger("historical_news_replay", "historical_news_replay.log")

    symbols = list((config.get("practice") or {}).get("micro", {}).get("symbols") or MICRO_SYMBOLS)
    replay_bars = max_bars or int((config.get("research") or {}).get("replay_bars", 300))
    replay_step = step or int((config.get("research") or {}).get("replay_step", 12))

    cell_report = cell_expectancy_report(symbols=symbols)
    evolution_state = None
    if refresh_evolution and positive_evolution_active(config):
        ledger = read_json_state("forward_test_ledger.json", default={}) or {}
        evolution_state = refresh_positive_evolution(
            config, ledger_cells=ledger.get("cells"), logger=logger,
        )

    portfolio = run_portfolio_replay(
        config,
        symbols,
        max_bars=replay_bars,
        step=replay_step,
        logger=logger,
        spread_data={s: LIVE_SPREAD_POINTS.get(s, 50.0) for s in symbols},
        return_all_trades=True,
    )

    per_symbol: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            sym_out = ReplayEngine(config, logger).run(
                symbol=symbol, max_bars=replay_bars, step=replay_step,
            )
            per_symbol.append({
                "symbol": symbol,
                "trades_closed": sym_out.get("trades_closed"),
                "pnl_total": sym_out.get("pnl_total"),
                "win_rate_pct": sym_out.get("win_rate_pct"),
                "news_blocked_signals": sym_out.get("news_blocked_signals", 0),
                "growth_blocked_signals": sym_out.get("growth_blocked_signals", 0),
            })
        except Exception as exc:  # noqa: BLE001
            per_symbol.append({"symbol": symbol, "error": str(exc)})

    growth = portfolio.get("growth_replay") or {}
    positive = bool(
        cell_report.get("positive_evolution")
        and growth.get("positive_evolution")
        and float(portfolio.get("pnl_total", 0)) >= 0
    )
    verdict = (
        "positive_evolution_ready"
        if cell_report.get("positive_evolution") and growth.get("on_track")
        else "collecting_data"
        if cell_report.get("positive_evolution")
        else "needs_improvement"
    )

    report = {
        "profile": profile,
        "approach": "historical_news_growth_replay",
        "symbols": symbols,
        "replay_bars": replay_bars,
        "replay_step": replay_step,
        "portfolio_replay": {
            "trades_closed": portfolio.get("trades_closed"),
            "pnl_total": portfolio.get("pnl_total"),
            "win_rate_pct": portfolio.get("win_rate_pct"),
            "final_equity": portfolio.get("final_equity"),
            "starting_cash": portfolio.get("starting_cash"),
            "news_blocked_signals": portfolio.get("news_blocked_signals", 0),
            "growth_blocked_signals": portfolio.get("growth_blocked_signals", 0),
        },
        "per_symbol_replay": per_symbol,
        "growth_replay": growth,
        "trade_log_cells": {
            "positive_evolution": cell_report.get("positive_evolution"),
            "positive_edge_targets": cell_report.get("positive_edge_targets"),
            "edge_target_total": cell_report.get("edge_target_total"),
        },
        "positive_evolution_state": {
            "total_allowed": (evolution_state or {}).get("total_allowed"),
            "total_blocked": (evolution_state or {}).get("total_blocked"),
        } if evolution_state else None,
        "positive_evolution": bool(cell_report.get("positive_evolution")),
        "becoming_positive": positive,
        "verdict": verdict,
    }
    write_json_state(STATE_FILE, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Historical replay with news + growth")
    parser.add_argument("--profile", default="30")
    parser.add_argument("--max-bars", type=int, default=None)
    parser.add_argument("--step", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = run_historical_news_replay(
        args.profile, max_bars=args.max_bars, step=args.step,
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(json.dumps({
            "profile": report["profile"],
            "verdict": report["verdict"],
            "becoming_positive": report["becoming_positive"],
            "portfolio": report["portfolio_replay"],
            "growth": report["growth_replay"],
            "cells": report["trade_log_cells"],
        }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())