"""Learning pipeline health — are closed trades rich enough to culture from?"""

from __future__ import annotations

from typing import Any

from core.strategy_policy import KNOWN_SETUPS, culturing_cell_from_trade
from core.utils import read_json_state

REQUIRED_TRADE_FIELDS = ("trade_id", "symbol", "setup_type", "side", "pnl", "result")
CONTEXT_FIELDS = ("market_context", "confidence", "signal_id")


def _has_market_context(trade: dict[str, Any]) -> bool:
    mc = trade.get("market_context")
    if isinstance(mc, dict) and mc.get("session"):
        return True
    meta = trade.get("signal_meta") or {}
    mctx = meta.get("market_context") if isinstance(meta.get("market_context"), dict) else {}
    return bool(mctx.get("session"))


def assess_trade_learning(trades: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Score how much context closed trades carry for culturing / arena / edge DB."""
    if trades is None:
        data = read_json_state("paper_trades.json", default={"trades": []}) or {}
        trades = list(data.get("trades") or [])

    clean_trades = [
        t for t in trades
        if not t.get("archive_polluted") and t.get("signal_id")
    ]
    total = len(trades)
    clean_n = len(clean_trades)
    if total == 0:
        return {
            "total_trades": 0,
            "ready_pct": 0.0,
            "culturing_ready_pct": 0.0,
            "missing_context": 0,
            "unknown_setup": 0,
            "culturing_cells": 0,
            "sufficient": False,
            "detail": "no closed trades yet",
        }

    missing_ctx = 0
    unknown_setup = 0
    culturing_ok = 0
    cells: set[str] = set()
    field_hits = {f: 0 for f in REQUIRED_TRADE_FIELDS + CONTEXT_FIELDS}

    for t in clean_trades:
        for f in REQUIRED_TRADE_FIELDS:
            if t.get(f) is not None:
                field_hits[f] += 1
        for f in CONTEXT_FIELDS:
            if t.get(f) is not None or (t.get("signal_meta") or {}).get(f):
                field_hits[f] += 1
        if not _has_market_context(t):
            missing_ctx += 1
        setup = t.get("setup_type")
        if not setup or setup not in KNOWN_SETUPS:
            unknown_setup += 1
        else:
            cell = culturing_cell_from_trade(t)
            if cell and "?" not in cell.split("|")[-1]:
                culturing_ok += 1
                cells.add(f"{t.get('symbol')}|{cell}")

    denom = max(1, clean_n or total)
    ready_pct = round(100.0 * (denom - missing_ctx) / denom, 1)
    cult_pct = round(100.0 * culturing_ok / denom, 1)
    min_n = int((read_json_state("forward_test_ledger.json", default={}) or {}).get("config", {}).get("min_n", 8))

    return {
        "total_trades": total,
        "linked_trades": clean_n,
        "legacy_unlinked": total - clean_n,
        "ready_pct": ready_pct,
        "culturing_ready_pct": cult_pct,
        "missing_context": missing_ctx,
        "unknown_setup": unknown_setup,
        "culturing_cells": len(cells),
        "min_n_for_veto": min_n,
        "cells_until_first_veto": max(0, min_n - culturing_ok),
        "sufficient": culturing_ok >= min_n or (total >= min_n and ready_pct >= 80.0),
        "field_coverage": {k: round(100.0 * v / denom, 1) for k, v in field_hits.items()},
        "detail": (
            f"{total} trades, {ready_pct}% with session context, "
            f"{culturing_ok} culturing-ready, {len(cells)} unique cells"
        ),
    }