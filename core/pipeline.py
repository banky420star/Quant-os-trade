"""Trading pipeline — sequential loops with per-loop fault isolation."""

from __future__ import annotations

import logging
import traceback
from typing import Any

from core.mt5_terminal_manager import MT5TerminalManager

PIPELINE_LOOPS: list[tuple[str, Any]] = []

# Cycle counter — analytical loops run every N cycles to keep the pipeline fast
# while still collecting policy/optimizer/adaptation data at a useful cadence.
_cycle_counter: int = 0
ANALYTICS_EVERY_N: int = 5
ANALYTICAL_LOOPS: set[str] = {
    "policy_detection_loop",
    "policy_optimizer_loop",
    "adaptation_loop",
    "news_sentiment_loop",
    # Read-only aggregation of the specialized-setup shadow fire ledger.
    "specialized_shadow_loop",
    # NOTE: m1_structure_loop deliberately NOT here — it is part of the live
    # market-state layer and must run on EVERY pipeline cycle so the current
    # FORMING M1 candle stays fresh. It is shadow-only and gated on
    # m1_structure.enabled, so running it every cycle never touches orders.
}


def _init_loops() -> list[tuple[str, Any]]:
    from loops import (
        blue_guardian_loop,
        data_loop,
        evaluation_loop,
        policy_detection_loop,
        policy_optimizer_loop,
        execution_loop,
        feature_loop,
        adaptation_loop,
        health_loop,
        market_context_loop,
        memory_loop,
        position_manager_loop,
        risk_loop,
        signal_loop,
        verifier_loop,
        trade_log_loop,
        babysit_loop,
        news_sentiment_loop,
        specialized_shadow_loop,
        m1_structure_loop,
    )
    return [
        ("data_loop", data_loop.run),
        ("feature_loop", feature_loop.run),
        ("market_context_loop", market_context_loop.run),
        ("risk_loop", risk_loop.run),
        ("signal_loop", signal_loop.run),
        ("evaluation_loop", evaluation_loop.run),
        ("policy_detection_loop", policy_detection_loop.run),
        ("verifier_loop", verifier_loop.run),
        ("execution_loop", execution_loop.run),
        ("blue_guardian_loop", blue_guardian_loop.run),
        ("position_manager_loop", position_manager_loop.run),
        ("memory_loop", memory_loop.run),
        # Organic adaptation: culturing vetoes, BE/trail, edge shifts after new closes.
        ("adaptation_loop", adaptation_loop.run),
        # USER feature request 2026-07-01: comprehensive per-trade log
        # (open/close times, win/loss, setup, drawdown, R-multiple, all fields).
        # Throttled -- rebuilds state/trade_log.json only when paper_trades.json
        # changed (a trade closed). Reuses the in-process MT5 connection.
        ("trade_log_loop", trade_log_loop.run),
        # Observe profitability regressions every cycle (no fabricated equity).
        ("babysit_loop", babysit_loop.run),
        ("policy_optimizer_loop", policy_optimizer_loop.run),
        # 2026-07-31 — LLM news-sentiment SHADOW risk filter. Network-bound +
        # self-throttled (news.refresh_interval_seconds, default 900s). No-op
        # when news.enabled is false (default). Never blocks the pipeline.
        ("news_sentiment_loop", news_sentiment_loop.run),
        # 2026-08-04 — aggregate the specialized-setup shadow fire ledger into a
        # per-symbol/per-setup/per-session report. Read-only, analytical, gated
        # on signals.specialized_setups.shadow.
        ("specialized_shadow_loop", specialized_shadow_loop.run),
        # 2026-08-06 — forward-only M1 smart-money structure shadow engine.
        # Writes state/m1_structure_decisions.json for the dashboard. Read-only,
        # gated on m1_structure.enabled (disabled by default in Phase 0).
        ("m1_structure_loop", m1_structure_loop.run),
        ("health_loop", lambda: health_loop.run(connect=True)),
    ]


def run_pipeline(config: dict[str, Any], logger: logging.Logger) -> dict[str, Any]:
    """Run all trading loops; failures in one loop do not stop the rest.

    Analytical loops (policy_detection, policy_optimizer, adaptation) are
    skipped on most cycles and only run every ANALYTICS_EVERY_N (5) cycles,
    keeping the fast pipeline loops responsive.
    """
    global _cycle_counter
    _cycle_counter += 1
    run_analytics = (_cycle_counter % ANALYTICS_EVERY_N == 0)
    loops = PIPELINE_LOOPS or _init_loops()
    results: dict[str, str] = {}

    for name, fn in loops:
        if name in ANALYTICAL_LOOPS and not run_analytics:
            logger.info(
                "--- %s --- SKIPPED (cycle %d, every %d)",
                name, _cycle_counter, ANALYTICS_EVERY_N,
            )
            results[name] = "SKIPPED"
            continue
        try:
            logger.info("--- %s ---", name)
            if name == "data_loop":
                terminal_mgr = MT5TerminalManager(config, logger)
                alignment = terminal_mgr.session_alignment()
                logger.info(
                    "data_loop session: aligned=%s session=%s",
                    alignment.get("aligned"),
                    alignment.get("python_session_id"),
                )
            fn()
            results[name] = "OK"
        except Exception as exc:
            results[name] = f"FAILED: {exc}"
            logger.error("%s failed: %s", name, exc)
            logger.error(traceback.format_exc())

    return {"loops": results, "pipeline": "complete"}