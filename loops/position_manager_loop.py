"""Position manager loop — break-even and per-symbol trailing stops."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.mt5_connection_manager import MT5ConnectionManager
from core.position_manager import manage_mt5_positions, manage_paper_positions
from core.position_sync import fetch_mt5_agent_positions
from core.utils import load_config, read_json_state, setup_logger, write_json_state


def run() -> dict:
    config = load_config()
    logger = setup_logger("position_manager_loop", "position_manager_loop.log")
    mode = config.get("execution", {}).get("mode", "paper")
    features = read_json_state("features.json", default={"symbols": {}})

    if mode == "mt5":
        connection = MT5ConnectionManager(config, logger)
        try:
            connection.connect()
            positions = fetch_mt5_agent_positions(config, logger)
            summary = manage_mt5_positions(config, positions, features, logger)
            if positions:
                write_json_state("paper_positions.json", {
                    "timestamp": summary["timestamp"],
                    "mode": "mt5",
                    "source": "mt5_sync",
                    "positions": positions,
                })
            logger.info(
                "Position manager MT5: %d updated, %d errors",
                summary.get("updated", 0),
                len(summary.get("errors", [])),
            )
            return summary
        finally:
            connection.disconnect()

    positions_data = read_json_state("paper_positions.json", default={"positions": []})
    positions = list(positions_data.get("positions", []))
    if not positions:
        logger.info("No paper positions to manage")
        return {"updated": 0, "actions": []}

    prices = {
        sym: float(feat.get("price", 0))
        for sym, feat in features.get("symbols", {}).items()
        if feat.get("price")
    }
    updated, summary = manage_paper_positions(config, positions, features, logger)
    write_json_state("paper_positions.json", {
        "timestamp": summary["timestamp"],
        "mode": "paper",
        "positions": updated,
        "prices": prices,
    })
    logger.info("Position manager paper: %d SL updates", summary.get("updated", 0))
    return summary


if __name__ == "__main__":
    run()