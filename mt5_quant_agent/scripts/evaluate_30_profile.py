"""Evaluate $30 micro profiles — C1 culturing evolution or C2 adaptive weight evolution."""

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

from core.micro_quant_evolution import (  # noqa: E402
    micro_evolution_enabled,
    replay_expectancy_usd,
    replay_fitness_score,
    run_micro_evolution,
)
from core.profile_launcher import set_active_profile  # noqa: E402
from core.replay_engine import ReplayEngine, run_portfolio_replay  # noqa: E402
from core.utils import load_config, read_json_state, setup_logger, write_json_state  # noqa: E402

MICRO_SYMBOLS = ["XAUUSDm", "USOILm", "UK100m"]
LIVE_SPREAD_POINTS = {"XAUUSDm": 240.0, "USOILm": 20.0, "UK100m": 80.0}


def _is_c2_profile(profile: str, config: dict[str, Any]) -> bool:
    if profile in ("30-c2", "30_c2"):
        return True
    return micro_evolution_enabled(config)


def _evaluate_c2(
    profile: str,
    config: dict[str, Any],
    logger: Any,
    *,
    max_bars: int,
    step: int,
    run_evolution: bool,
) -> dict[str, Any]:
    symbols = list(micro_evolution_settings_symbols(config))
    symbol_replays: list[dict[str, Any]] = []
    total_fitness = 0.0
    total_pnl = 0.0
    total_trades = 0

    for symbol in symbols:
        try:
            out = ReplayEngine(config, logger).run(symbol=symbol, max_bars=max_bars, step=step)
        except Exception as exc:  # noqa: BLE001
            symbol_replays.append({"symbol": symbol, "error": str(exc), "fitness_score": 0.0})
            continue
        fitness = replay_fitness_score(out)
        total_fitness += fitness
        total_pnl += float(out.get("pnl_total", 0))
        total_trades += int(out.get("trades_closed", 0))
        symbol_replays.append({
            "symbol": symbol,
            "trades_closed": out.get("trades_closed"),
            "win_rate_pct": out.get("win_rate_pct"),
            "pnl_total": out.get("pnl_total"),
            "expectancy_usd": replay_expectancy_usd(out),
            "fitness_score": round(fitness, 3),
            "final_equity": out.get("final_equity"),
        })

    evolution_result = run_micro_evolution(config, logger) if run_evolution else None
    micro = (config.get("practice") or {}).get("micro") or {}

    return {
        "profile": profile,
        "approach": "adaptive_weight_evolution_c2",
        "account_size_usd": micro.get("account_size_usd", 30),
        "starting_cash": config.get("execution", {}).get("starting_cash", 30),
        "symbols": symbols,
        "replay_bars": max_bars,
        "replay_step": step,
        "aggregate": {
            "total_fitness_score": round(total_fitness, 3),
            "avg_fitness_score": round(total_fitness / max(1, len(symbols)), 3),
            "total_pnl": round(total_pnl, 2),
            "total_trades": total_trades,
            "avg_expectancy_usd": round(total_pnl / total_trades, 4) if total_trades else 0.0,
        },
        "per_symbol_replay": symbol_replays,
        "micro_evolution": evolution_result,
        "positive_evolution": bool(evolution_result and evolution_result.get("positive_evolution")),
        "deploy_safe": not bool((config.get("research") or {}).get("auto_deploy")),
        "verdict": (
            "positive_evolution_ready"
            if evolution_result and evolution_result.get("positive_evolution")
            else "hold_baseline"
        ),
    }


def micro_evolution_settings_symbols(config: dict[str, Any]) -> list[str]:
    from core.micro_quant_evolution import micro_evolution_settings

    return micro_evolution_settings(config)["symbols"]


def _evaluate_c1(
    profile: str,
    config: dict[str, Any],
    logger: Any,
    *,
    max_bars: int,
    step: int,
    refresh_evolution: bool,
) -> dict[str, Any]:
    from core.positive_evolution import positive_evolution_active, refresh_positive_evolution
    from quant.research.trade_log_cells import cell_expectancy_report

    symbols = list((config.get("practice") or {}).get("micro", {}).get("symbols") or MICRO_SYMBOLS)
    evolution_state = None
    if refresh_evolution and positive_evolution_active(config):
        ledger = read_json_state("forward_test_ledger.json", default={}) or {}
        evolution_state = refresh_positive_evolution(
            config, ledger_cells=ledger.get("cells"), logger=logger,
        )

    cell_report = cell_expectancy_report(symbols=symbols)
    replay_out = run_portfolio_replay(
        config, symbols, max_bars=max_bars, step=step,
        spread_data={s: LIVE_SPREAD_POINTS.get(s, 50.0) for s in symbols},
        return_all_trades=True,
    )
    pnl = float(replay_out.get("pnl_total", 0))
    wr = float(replay_out.get("win_rate_pct", 0))
    trades = int(replay_out.get("trades_closed", 0))
    replay_score = pnl + (wr - 50) * 0.3 + min(trades, 30) * 0.05
    micro = (config.get("practice") or {}).get("micro") or {}

    return {
        "profile": profile,
        "approach": "culturing_evolution_c1",
        "account_size_usd": micro.get("account_size_usd", 30),
        "symbols": symbols,
        "trade_log_cells": cell_report,
        "replay_score": {
            "trades_closed": trades,
            "pnl_total": pnl,
            "win_rate_pct": wr,
            "score": round(replay_score, 3),
        },
        "positive_evolution_state": evolution_state,
        "positive_evolution": bool(cell_report.get("positive_evolution")),
        "verdict": "positive_evolution_ready" if cell_report.get("positive_evolution") else "collecting_data",
    }


def evaluate_profile(
    profile: str = "30",
    *,
    max_bars: int | None = None,
    step: int | None = None,
    run_evolution: bool = True,
    refresh_evolution: bool = True,
) -> dict[str, Any]:
    os.environ["MT5_QUANT_PROFILE"] = profile
    set_active_profile(profile)
    config = load_config()
    logger = setup_logger("evaluate_30_profile", "evaluate_30_profile.log")

    replay_bars = max_bars or int((config.get("research") or {}).get("replay_bars", 300))
    replay_step = step or int((config.get("research") or {}).get("replay_step", 15))

    if _is_c2_profile(profile, config):
        report = _evaluate_c2(
            profile, config, logger,
            max_bars=replay_bars, step=replay_step, run_evolution=run_evolution,
        )
    else:
        report = _evaluate_c1(
            profile, config, logger,
            max_bars=replay_bars, step=replay_step, refresh_evolution=refresh_evolution,
        )

    write_json_state("evaluate_30_profile.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate $30 micro profile")
    parser.add_argument("--profile", default="30", help="Profile (default: 30, winner)")
    parser.add_argument("--max-bars", type=int, default=None)
    parser.add_argument("--step", type=int, default=None)
    parser.add_argument("--no-evolution", action="store_true", help="Skip C2 evolution cycle")
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    args = parser.parse_args()

    report = evaluate_profile(
        args.profile,
        max_bars=args.max_bars,
        step=args.step,
        run_evolution=not args.no_evolution,
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(json.dumps({
            "profile": report["profile"],
            "approach": report["approach"],
            "positive_evolution": report.get("positive_evolution"),
            "verdict": report.get("verdict"),
            "aggregate": report.get("aggregate"),
            "micro_evolution": {
                "proposal": (report.get("micro_evolution") or {}).get("proposal"),
                "symbols_evolved": (report.get("micro_evolution") or {}).get("symbols_evolved"),
            } if report.get("micro_evolution") else None,
        }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())