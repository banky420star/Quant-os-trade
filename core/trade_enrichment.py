"""Enrich closed trades with signal metadata for edge database ingest."""

from __future__ import annotations

from typing import Any


def _signal_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("signal", record)


def build_signal_index(
    approved_data: dict[str, Any] | None = None,
    orders: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Map signal_id -> full signal payload from approved signals and orders."""
    index: dict[str, dict[str, Any]] = {}

    for record in (approved_data or {}).get("approved", []):
        sig = _signal_from_record(record)
        sid = sig.get("signal_id")
        if sid:
            index[sid] = sig

    for order in orders or []:
        sid = order.get("signal_id")
        if not sid or sid in index:
            continue
        index[sid] = {
            "signal_id": sid,
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "setup_type": order.get("setup_type"),
            "entry": order.get("entry"),
            "sl": order.get("sl"),
            "tp1": order.get("tp1"),
            "reason": order.get("reason"),
            "signal_meta": order.get("signal_meta"),
        }

    return index


def enrich_trade(
    trade: dict[str, Any],
    signal_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Attach signal metadata to a trade for edge DB / memory."""
    enriched = dict(trade)
    meta = enriched.get("signal_meta") or {}

    sid = enriched.get("signal_id")
    if sid and sid in signal_index:
        sig = signal_index[sid]
        meta = {
            "signal_id": sid,
            "symbol": sig.get("symbol", enriched.get("symbol")),
            "side": sig.get("side", enriched.get("side")),
            "setup_type": sig.get("setup_type", enriched.get("setup_type")),
            "entry": sig.get("entry", enriched.get("entry")),
            "sl": sig.get("sl", enriched.get("sl")),
            "tp1": sig.get("tp1", enriched.get("tp1")),
            "confidence": sig.get("confidence"),
            "confidence_tree": sig.get("confidence_tree"),
            "evidence": sig.get("evidence"),
            "market_context": sig.get("market_context"),
            "reason": sig.get("reason"),
            "strategy_rank": sig.get("strategy_rank"),
        }

    enriched["signal_meta"] = meta
    for key in ("confidence", "confidence_tree", "evidence", "market_context", "sl", "tp1"):
        if key not in enriched and meta.get(key) is not None:
            enriched[key] = meta[key]
    if not enriched.get("setup_type") and meta.get("setup_type"):
        enriched["setup_type"] = meta["setup_type"]
    # Carry the four plain-English narratives (entry/BE/trail/exit) onto the
    # enriched record so the edge DB / evaluator reports show reason+profit
    # together. entry_narrative lives on the signal meta; be/trail/exit are
    # stamped by PaperBroker._check_exits directly on the trade dict.
    if not enriched.get("entry_narrative") and meta.get("entry_narrative"):
        enriched["entry_narrative"] = meta["entry_narrative"]
    for key in ("be_narrative", "trail_narrative", "exit_narrative"):
        if not enriched.get(key) and trade.get(key):
            enriched[key] = trade[key]
    return enriched


def enrich_trades(
    trades: list[dict[str, Any]],
    *,
    approved_data: dict[str, Any] | None = None,
    orders: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Enrich a batch of trades with signal metadata."""
    index = build_signal_index(approved_data, orders)
    return [enrich_trade(t, index) for t in trades]