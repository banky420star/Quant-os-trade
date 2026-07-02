"""Agent 7: Memory Loop — learn from paper trade outcomes."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.edge_database import EdgeDatabase
from core.memory_engine import MemoryEngine
from core.trade_enrichment import enrich_trades
from core.utils import load_config, read_json_state, setup_logger, write_json_state


def run() -> dict:
    """Record trade outcomes and update edge scores."""
    config = load_config()
    logger = setup_logger("memory_loop", "memory_loop.log")
    logger.info("Starting memory loop")

    trades_data = read_json_state("paper_trades.json", default={"trades": []})
    features = read_json_state("features.json", default={"symbols": {}})
    memory = read_json_state("memory.json", default={"records": [], "adjustments": []})
    edge_scores = read_json_state("edge_scores.json", default={"setups": {}})
    approved_data = read_json_state("approved_signals.json", default={"approved": []})
    orders_data = read_json_state("paper_orders.json", default={"orders": []})

    trades = trades_data.get("trades", [])
    market_ctx = read_json_state("market_context.json", default={})
    context_data = market_ctx.get("market_context", market_ctx) if isinstance(market_ctx, dict) else {}
    if not isinstance(context_data, dict):
        context_data = {}

    engine = MemoryEngine(logger)
    result = engine.process(trades, features, context_data, memory, edge_scores)

    new_records = result.get("new_records", [])
    wins = sum(1 for r in new_records if r.get("result") == "win")
    losses = sum(1 for r in new_records if r.get("result") == "loss")

    write_json_state("memory.json", result["memory"])
    write_json_state("edge_scores.json", result["edge_scores"])

    edge_db = EdgeDatabase(logger)
    source = config.get("execution", {}).get("mode", "paper")
    new_trade_ids = {r.get("trade_id") for r in new_records if r.get("trade_id")}
    raw_trades = [t for t in trades if t.get("trade_id") in new_trade_ids] if new_trade_ids else []
    trades_to_ingest = enrich_trades(
        raw_trades,
        approved_data=approved_data,
        orders=orders_data.get("orders", []),
    )

    edge_added = edge_db.ingest_batch(
        trades_to_ingest,
        features=features,
        context=context_data,
        source=source,
    )
    logger.info(
        "Memory saved — %d new (%d wins, %d losses), %d total, %d adjustments, %d edge records",
        len(new_records),
        wins,
        losses,
        len(result["memory"]["records"]),
        len(result["memory"]["adjustments"]),
        len(edge_added),
    )
    return result


if __name__ == "__main__":
    run()
