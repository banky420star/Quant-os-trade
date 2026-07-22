"""Optimizer Loop — parameter search via replay."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.strategy_optimizer import StrategyOptimizer
from core.utils import load_config, setup_logger


def run(symbol: str | None = None) -> dict:
    config = load_config()
    logger = setup_logger("optimizer_loop", "optimizer_loop.log")
    logger.info("=== Optimizer Loop starting ===")
    optimizer = StrategyOptimizer(config, logger)
    result = optimizer.run(symbol=symbol)
    if result.get("best"):
        logger.info(
            "Best params: %s score=%.2f",
            result["best"]["params"],
            result["best"]["score"],
        )
    logger.info("=== Optimizer Loop complete ===")
    return result


if __name__ == "__main__":
    run()