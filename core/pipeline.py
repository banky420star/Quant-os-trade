"""Trading pipeline — sequential loops with per-loop fault isolation."""

from __future__ import annotations

import logging
import traceback
from typing import Any

from core.mt5_terminal_manager import MT5TerminalManager

PIPELINE_LOOPS: list[tuple[str, Any]] = []


def _init_loops() -> list[tuple[str, Any]]:
    from loops import (
        data_loop,
        execution_loop,
        feature_loop,
        health_loop,
        market_context_loop,
        memory_loop,
        position_manager_loop,
        risk_loop,
        signal_loop,
        verifier_loop,
    )
    return [
        ("data_loop", data_loop.run),
        ("feature_loop", feature_loop.run),
        ("market_context_loop", market_context_loop.run),
        ("risk_loop", risk_loop.run),
        ("signal_loop", signal_loop.run),
        ("verifier_loop", verifier_loop.run),
        ("execution_loop", execution_loop.run),
        ("position_manager_loop", position_manager_loop.run),
        ("memory_loop", memory_loop.run),
        ("health_loop", lambda: health_loop.run(connect=True)),
    ]


def run_pipeline(config: dict[str, Any], logger: logging.Logger) -> dict[str, Any]:
    """Run all trading loops; failures in one loop do not stop the rest."""
    loops = PIPELINE_LOOPS or _init_loops()
    results: dict[str, str] = {}

    for name, fn in loops:
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