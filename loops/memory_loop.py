"""Agent 7: Memory Loop — learn from paper trade outcomes."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.edge_database import EdgeDatabase
from core.learning_health import assess_trade_learning
from core.memory_engine import MemoryEngine
from core.strategy_arena import arena_enabled, record_outcomes
from core.trade_enrichment import enrich_trades
from core.trade_history import trade_history_filename
from core.utils import load_config, read_json_state, setup_logger, utc_now_iso, write_json_state


def _needs_enrichment(trade: dict) -> bool:
    if trade.get("archive_polluted"):
        return False
    if not trade.get("learning_enriched"):
        return True
    meta = trade.get("signal_meta") or {}
    mc = trade.get("market_context") or meta.get("market_context") or {}
    return not (isinstance(mc, dict) and mc.get("session"))


def _backfill_context(
    trades: list[dict],
    *,
    approved_data: dict,
    orders: list[dict],
    logger,
) -> list[dict]:
    """Enrich historical closes missing session/regime for culturing."""
    stale = [t for t in trades if _needs_enrichment(t)]
    if not stale:
        return trades
    enriched = enrich_trades(stale, approved_data=approved_data, orders=orders)
    by_id = {t["trade_id"]: t for t in enriched if t.get("trade_id")}
    out = [by_id.get(t.get("trade_id"), t) for t in trades]
    logger.info("Trade context backfill: enriched %d / %d historical closes", len(enriched), len(stale))
    return out


def _persist_enriched_trades(all_trades: list[dict], enriched_new: list[dict], ledger: str = "paper_trades.json") -> None:
    """Write enriched context back onto the active ledger so culturing reads full cells."""
    if not enriched_new:
        return
    by_id = {t.get("trade_id"): t for t in enriched_new if t.get("trade_id")}
    merged = []
    for t in all_trades:
        tid = t.get("trade_id")
        merged.append(by_id.get(tid, t) if tid in by_id else t)
    write_json_state(ledger, {
        "timestamp": utc_now_iso(),
        "trades": merged,
        "last_enrichment": utc_now_iso(),
    })


def run() -> dict:
    """Record trade outcomes and update edge scores."""
    config = load_config()
    logger = setup_logger("memory_loop", "memory_loop.log")
    logger.info("Starting memory loop")

    # 2026-08-04 — read the ACTIVE closed-trade ledger, not a hardcoded
    # paper_trades.json. In MT5 mode (execution.mode=mt5, the growth/demo
    # profile) closed trades live in mt5_trades.json and paper_trades.json
    # is empty/stale, so the Memory tab (edge_scores.setup_stats.global)
    # and the Research section (edge_database aggregates) never populated.
    # Same plumbing gap that broke the daily tracker + calibration.
    ledger = trade_history_filename(config)
    orders_data = read_json_state("paper_orders.json", default={"orders": []})

    trades_data = read_json_state(ledger, default={"trades": []})
    features = read_json_state("features.json", default={"symbols": {}})
    memory = read_json_state("memory.json", default={"records": [], "adjustments": []})
    edge_scores = read_json_state("edge_scores.json", default={"setups": {}})
    approved_data = read_json_state("approved_signals.json", default={"approved": []})

    trades = list(trades_data.get("trades", []))
    orders = orders_data.get("orders", [])
    trades = _backfill_context(
        trades,
        approved_data=approved_data,
        orders=orders,
        logger=logger,
    )
    if trades != trades_data.get("trades", []):
        write_json_state(ledger, {
            "timestamp": utc_now_iso(),
            "trades": trades,
            "last_backfill": utc_now_iso(),
        })

    market_ctx = read_json_state("market_context.json", default={})
    context_data = market_ctx.get("market_context", market_ctx) if isinstance(market_ctx, dict) else {}
    if not isinstance(context_data, dict):
        context_data = {}

    processed_ids = {r.get("trade_id") for r in memory.get("records", []) if r.get("trade_id")}
    raw_new = [t for t in trades if t.get("trade_id") and t.get("trade_id") not in processed_ids]

    enriched_new = enrich_trades(
        raw_new,
        approved_data=approved_data,
        orders=orders_data.get("orders", []),
    ) if raw_new else []

    if enriched_new:
        _persist_enriched_trades(trades, enriched_new, ledger=ledger)
        by_id = {t["trade_id"]: t for t in enriched_new}
        trades = [by_id.get(t.get("trade_id"), t) for t in trades]

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
    trades_to_ingest = [
        t for t in trades
        if t.get("trade_id") in new_trade_ids
    ] if new_trade_ids else []

    edge_added = edge_db.ingest_batch(
        trades_to_ingest,
        features=features,
        context=context_data,
        source=source,
    )

    if arena_enabled(config) and trades_to_ingest:
        arena_result = record_outcomes(trades_to_ingest, config, logger=logger)
        logger.info(
            "Arena outcomes recorded: %d (leaders updated)",
            arena_result.get("recorded", 0),
        )

    learning = assess_trade_learning(trades)
    write_json_state("learning_health.json", {**learning, "updated_at": utc_now_iso()})
    logger.info("Learning health: %s", learning.get("detail"))

    logger.info(
        "Memory saved — %d new (%d wins, %d losses), %d total, %d adjustments, %d edge records",
        len(new_records),
        wins,
        losses,
        len(result["memory"]["records"]),
        len(result["memory"]["adjustments"]),
        len(edge_added),
    )
    return {**result, "learning_health": learning}


if __name__ == "__main__":
    run()