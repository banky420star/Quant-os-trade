"""Enrich closed trades with signal metadata for edge database ingest."""

from __future__ import annotations

from typing import Any

from core.signal_archive import load_archive
from core.strategy_policy import normalize_setup_type


def _signal_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("signal", record)


def _trigger_index_from_arena() -> dict[str, dict[str, Any]]:
    from core.utils import read_json_state

    idx: dict[str, dict[str, Any]] = {}
    for row in (read_json_state("strategy_arena.json", default={}) or {}).get("trigger_log") or []:
        sid = row.get("signal_id")
        if sid:
            idx[str(sid)] = row
    return idx


def build_signal_index(
    approved_data: dict[str, Any] | None = None,
    orders: list[dict[str, Any]] | None = None,
    *,
    include_archive: bool = True,
    include_triggers: bool = True,
) -> dict[str, dict[str, Any]]:
    """Map signal_id -> full signal payload from all durable sources."""
    index: dict[str, dict[str, Any]] = {}

    if include_archive:
        for sid, sig in (load_archive().get("signals") or {}).items():
            if isinstance(sig, dict):
                index[str(sid)] = dict(sig)

    for record in (approved_data or {}).get("approved", []):
        sig = _signal_from_record(record)
        sid = sig.get("signal_id")
        if sid:
            index[sid] = {**index.get(sid, {}), **sig}

    for order in orders or []:
        sid = order.get("signal_id")
        if not sid:
            continue
        base = index.get(sid, {})
        meta = order.get("signal_meta") or {}
        index[sid] = {
            **base,
            "signal_id": sid,
            "symbol": order.get("symbol") or base.get("symbol"),
            "side": order.get("side") or base.get("side"),
            "setup_type": order.get("setup_type") or base.get("setup_type"),
            "entry": order.get("entry") or base.get("entry"),
            "sl": order.get("sl") or base.get("sl"),
            "tp1": order.get("tp1") or base.get("tp1"),
            "tp2": order.get("tp2") or base.get("tp2"),
            "reason": order.get("reason") or base.get("reason"),
            "signal_meta": meta or base.get("signal_meta"),
            "market_context": base.get("market_context") or meta.get("market_context"),
            "confidence": base.get("confidence") or meta.get("confidence"),
            "confidence_tree": base.get("confidence_tree") or meta.get("confidence_tree"),
            "evidence": base.get("evidence") or meta.get("evidence"),
            "trigger_summary": base.get("trigger_summary") or meta.get("trigger_summary"),
            "trigger_context": base.get("trigger_context") or meta.get("trigger_context"),
            "entry_narrative": base.get("entry_narrative") or meta.get("entry_narrative"),
        }

    if include_triggers:
        for sid, trig in _trigger_index_from_arena().items():
            if sid not in index:
                index[sid] = {}
            row = index[sid]
            row.setdefault("trigger_summary", trig.get("trigger_summary"))
            row.setdefault("trigger_context", trig.get("trigger_context"))
            row.setdefault("symbol", trig.get("symbol"))
            row.setdefault("setup_type", trig.get("setup_type"))
            row.setdefault("side", trig.get("side"))
            row.setdefault("confidence", trig.get("confidence"))

    return index


def enrich_trade(
    trade: dict[str, Any],
    signal_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Attach full signal metadata to a trade for learning pipelines."""
    enriched = dict(trade)
    meta = dict(enriched.get("signal_meta") or {})
    if not isinstance(meta, dict):
        meta = {}

    sid = enriched.get("signal_id") or meta.get("signal_id")
    sig = signal_index.get(str(sid or ""), {})

    if sig:
        meta = {
            **meta,
            "signal_id": sid or sig.get("signal_id"),
            "symbol": sig.get("symbol", enriched.get("symbol")),
            "side": sig.get("side", enriched.get("side")),
            "setup_type": sig.get("setup_type", enriched.get("setup_type")),
            "entry": sig.get("entry", enriched.get("entry")),
            "sl": sig.get("sl", enriched.get("sl")),
            "tp1": sig.get("tp1", enriched.get("tp1")),
            "tp2": sig.get("tp2", enriched.get("tp2")),
            "confidence": sig.get("confidence"),
            "confidence_tree": sig.get("confidence_tree"),
            "evidence": sig.get("evidence"),
            "market_context": sig.get("market_context") or meta.get("market_context"),
            "reason": sig.get("reason"),
            "strategy_rank": sig.get("strategy_rank"),
            "trigger_summary": sig.get("trigger_summary") or meta.get("trigger_summary"),
            "trigger_context": sig.get("trigger_context") or meta.get("trigger_context"),
            "entry_narrative": sig.get("entry_narrative"),
            "features_at_entry": sig.get("features_at_entry") or meta.get("features_at_entry"),
            "kelly": sig.get("kelly") or meta.get("kelly"),
            "regime_primary": sig.get("regime_primary") or meta.get("regime_primary"),
            "regime_bias": sig.get("regime_bias") or meta.get("regime_bias"),
            "session": sig.get("session") or meta.get("session"),
            "phase": sig.get("phase") or meta.get("phase"),
            "move_type": sig.get("move_type") or meta.get("move_type"),
        }

    enriched["signal_meta"] = meta
    for key in (
        "confidence", "confidence_tree", "evidence", "market_context",
        "sl", "tp1", "tp2", "trigger_summary", "trigger_context",
    ):
        if key not in enriched and meta.get(key) is not None:
            enriched[key] = meta[key]

    if meta.get("setup_type"):
        cleaned = normalize_setup_type(
            enriched.get("setup_type") or meta.get("setup_type"),
            meta=meta,
            market_context=meta.get("market_context") if isinstance(meta.get("market_context"), dict) else {},
        )
        if cleaned:
            enriched["setup_type"] = cleaned

    if not enriched.get("entry_narrative") and meta.get("entry_narrative"):
        enriched["entry_narrative"] = meta["entry_narrative"]
    for key in ("be_narrative", "trail_narrative", "exit_narrative"):
        if not enriched.get(key) and trade.get(key):
            enriched[key] = trade[key]

    # Flatten market_context for culturing_cell_from_trade and forward-test ledger.
    mctx = enriched.get("market_context")
    if isinstance(mctx, dict) and mctx:
        enriched["market_context"] = mctx

    enriched["learning_enriched"] = True
    enriched["learning_enriched_at"] = meta.get("archived_at") or trade.get("closed_at")
    return enriched


def enrich_trades(
    trades: list[dict[str, Any]],
    *,
    approved_data: dict[str, Any] | None = None,
    orders: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Enrich a batch of trades with signal metadata from all durable sources."""
    index = build_signal_index(approved_data, orders)
    return [enrich_trade(t, index) for t in trades]