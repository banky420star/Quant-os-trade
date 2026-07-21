"""Position / trade manager loop — BE/trail, stale closes, ghost upgrades.

Part of the main pipeline (see ``core/pipeline.py``). Extends classic
position management with the trade_manager cycle:
  * watch open trades
  * log indicator + management params
  * flag stale losers
  * score ghost BE/trail upgrades and promote winners
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.mt5_connection_manager import MT5ConnectionManager
from core.position_manager import (
    manage_mt5_positions,
    manage_paper_positions,
    manage_partial_tp_mt5,
    manage_partial_tp_paper,
)
from core.position_sync import fetch_mt5_agent_positions
from core.trade_manager import run_trade_manager_cycle
from core.utils import load_config, read_json_state, setup_logger, write_json_state


def run() -> dict:
    config = load_config()
    logger = setup_logger("position_manager_loop", "position_manager_loop.log")
    mode = config.get("execution", {}).get("mode", "paper")
    features = read_json_state("features.json", default={"symbols": {}})
    paper_trades = read_json_state("paper_trades.json", default={"trades": []}) or {}
    closed = list(paper_trades.get("trades") or []) if isinstance(paper_trades, dict) else []

    if mode == "mt5":
        connection = MT5ConnectionManager(config, logger)
        try:
            connection.connect()
            positions = fetch_mt5_agent_positions(config, logger)
            partial_summary = manage_partial_tp_mt5(config, positions, features, logger)
            if partial_summary.get("partial_closes"):
                positions = fetch_mt5_agent_positions(config, logger)
            summary = manage_mt5_positions(config, positions, features, logger)
            summary["partial_tp"] = partial_summary
            if positions:
                write_json_state("paper_positions.json", {
                    "timestamp": summary["timestamp"],
                    "mode": "mt5",
                    "source": "mt5_sync",
                    "positions": positions,
                })
            tm = run_trade_manager_cycle(
                config,
                positions,
                features,
                closed_trades=closed,
                logger=logger,
            )
            summary["trade_manager"] = tm
            logger.info(
                "Position manager MT5: %d updated, %d errors, ghosts=%s stale=%d",
                summary.get("updated", 0),
                len(summary.get("errors", [])),
                (tm.get("ghost") or {}).get("ghosts_scored"),
                len(tm.get("stale_candidates") or []),
            )
            return summary
        finally:
            connection.disconnect()

    positions_data = read_json_state("paper_positions.json", default={"positions": []})
    positions = list(positions_data.get("positions", []))
    prices = {
        sym: float(feat.get("price", 0))
        for sym, feat in features.get("symbols", {}).items()
        if feat.get("price")
    }

    if not positions:
        # Still run ghost scoring on closed trades when flat
        tm = run_trade_manager_cycle(
            config, [], features, closed_trades=closed, logger=logger,
        )
        logger.info("No paper positions to manage (ghosts scored=%s)", (tm.get("ghost") or {}).get("ghosts_scored"))
        return {"updated": 0, "actions": [], "trade_manager": tm}

    positions, partial_trades, partial_summary = manage_partial_tp_paper(
        config, positions, features, logger,
    )
    updated, summary = manage_paper_positions(config, positions, features, logger)
    write_json_state("paper_positions.json", {
        "timestamp": summary["timestamp"],
        "mode": "paper",
        "positions": updated,
        "prices": prices,
        "partial_trades": partial_trades,
    })
    summary["partial_tp"] = partial_summary
    tm = run_trade_manager_cycle(
        config,
        updated,
        features,
        closed_trades=closed,
        logger=logger,
    )
    summary["trade_manager"] = tm
    logger.info(
        "Position manager paper: %d SL updates, ghosts=%s stale=%d",
        summary.get("updated", 0),
        (tm.get("ghost") or {}).get("ghosts_scored"),
        len(tm.get("stale_candidates") or []),
    )
    return summary


if __name__ == "__main__":
    run()
