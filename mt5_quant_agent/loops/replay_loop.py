"""Replay Loop — backtest on Parquet history (paper mode, no MT5)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.replay_engine import ReplayEngine
from core.utils import load_config, setup_logger


def run(symbol: str | None = None) -> dict:
    config = load_config()
    logger = setup_logger("replay_loop", "replay_loop.log")
    logger.info("=== Replay Loop starting ===")
    engine = ReplayEngine(config, logger)
    result = engine.run(symbol=symbol)
    logger.info("=== Replay Loop complete — PnL=%.2f ===", result.get("pnl_total", 0))
    return result


if __name__ == "__main__":
    run()