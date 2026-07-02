"""Memory Engine — learn which setup types the agent is actually good at."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from core.strategy_policy import normalize_setup_type
from core.utils import utc_now_iso


class MemoryEngine:
    """Record outcomes and track win rates per setup type."""

    DEFAULT_EDGE = 50.0
    WIN_BOOST = 3.0
    LOSS_PENALTY = 5.0

    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger("memory_engine")

    def process(
        self,
        trades: list[dict[str, Any]],
        features_data: dict[str, Any] | None = None,
        context_data: dict[str, Any] | None = None,
        existing_memory: dict[str, Any] | None = None,
        existing_edge_scores: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        memory = existing_memory or {"records": [], "adjustments": []}
        edge_scores = existing_edge_scores or {"setups": {}, "setup_stats": {}, "last_updated": None}

        memory.setdefault("records", [])
        memory.setdefault("adjustments", [])
        edge_scores.setdefault("setups", {})
        edge_scores.setdefault("setup_stats", {})

        processed_ids = {r.get("trade_id") for r in memory["records"]}
        new_trades = [t for t in trades if t.get("trade_id") not in processed_ids]
        new_records: list[dict[str, Any]] = []

        for trade in new_trades:
            record = self._build_record(trade, features_data, context_data)
            new_records.append(record)
            memory["records"].append(record)
            adjustment = self._compute_adjustment(trade, features_data, context_data)
            if adjustment:
                memory["adjustments"].append(adjustment)
                self._apply_edge_adjustment(edge_scores, trade, adjustment)

        edge_scores["setup_stats"] = self._compute_setup_stats(memory["records"])
        memory["setup_performance"] = self._format_performance(edge_scores["setup_stats"])
        memory["timestamp"] = utc_now_iso()
        memory["total_records"] = len(memory["records"])
        edge_scores["last_updated"] = utc_now_iso()

        wins = sum(1 for r in new_records if r.get("result") == "win")
        losses = sum(1 for r in new_records if r.get("result") == "loss")
        self.logger.info(
            "Memory: %d new records (%d wins, %d losses), %d total, %d setup types tracked",
            len(new_records),
            wins,
            losses,
            len(memory["records"]),
            len(edge_scores.get("setup_stats", {}).get("global", {})),
        )
        return {"memory": memory, "edge_scores": edge_scores, "new_records": new_records}

    def _compute_setup_stats(self, records: list[dict]) -> dict[str, Any]:
        stats: dict[str, dict[str, int]] = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})
        by_symbol: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})
        )
        by_cell: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})
        )

        for rec in records:
            setup = rec.get("setup_type", "unknown")
            symbol = rec.get("symbol", "unknown")
            regime = rec.get("regime", "unknown")
            session = rec.get("session", "unknown")
            result = rec.get("result")
            stats[setup]["total"] += 1
            by_symbol[symbol][setup]["total"] += 1
            by_cell[f"{symbol}|{setup}|{regime}|{session}"][setup]["total"] += 1
            if result == "win":
                stats[setup]["wins"] += 1
                by_symbol[symbol][setup]["wins"] += 1
                by_cell[f"{symbol}|{setup}|{regime}|{session}"][setup]["wins"] += 1
            elif result == "loss":
                stats[setup]["losses"] += 1
                by_symbol[symbol][setup]["losses"] += 1
                by_cell[f"{symbol}|{setup}|{regime}|{session}"][setup]["losses"] += 1

        output: dict[str, Any] = {"global": {}, "by_symbol": {}, "by_cell": {}}
        for setup, s in stats.items():
            wr = round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0
            output["global"][setup] = {**s, "win_rate_pct": wr}

        for symbol, setups in by_symbol.items():
            output["by_symbol"][symbol] = {}
            for setup, s in setups.items():
                wr = round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0
                output["by_symbol"][symbol][setup] = {**s, "win_rate_pct": wr}

        for cell_key, setups in by_cell.items():
            output["by_cell"][cell_key] = {}
            for setup, s in setups.items():
                wr = round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0
                output["by_cell"][cell_key][setup] = {**s, "win_rate_pct": wr}

        return output

    def _format_performance(self, setup_stats: dict) -> list[str]:
        lines = []
        for setup, s in setup_stats.get("global", {}).items():
            if s["total"] > 0:
                lines.append(f"{setup}: win rate {s['win_rate_pct']}% ({s['wins']}/{s['total']})")
        return lines

    def _build_record(
        self,
        trade: dict[str, Any],
        features_data: dict[str, Any] | None,
        context_data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        symbol = trade.get("symbol")
        feat = (features_data or {}).get("symbols", {}).get(symbol, {})
        ctx_root = context_data if isinstance(context_data, dict) else {}
        ctx = {}
        if ctx_root:
            symbols_ctx = ctx_root.get("symbols", {}) if isinstance(ctx_root.get("symbols"), dict) else {}
            market_ctx = ctx_root.get("market_context", {}) if isinstance(ctx_root.get("market_context"), dict) else {}
            market_symbols = market_ctx.get("symbols", {}) if isinstance(market_ctx.get("symbols"), dict) else {}
            ctx = symbols_ctx.get(symbol, market_symbols.get(symbol, {}))

        meta = trade.get("signal_meta") or {}
        mctx = meta.get("market_context") or trade.get("market_context") or {}
        if not isinstance(mctx, dict):
            mctx = {}
        regime = mctx.get("market_regime", {})
        setup_type = normalize_setup_type(
            trade.get("setup_type") or meta.get("setup_type"),
            meta=meta,
            market_context=mctx if isinstance(mctx, dict) else {},
        )

        return {
            "trade_id": trade.get("trade_id"),
            "signal_id": trade.get("signal_id"),
            "symbol": symbol,
            "setup_type": setup_type,
            "side": trade.get("side"),
            "entry": trade.get("entry"),
            "exit": trade.get("exit"),
            "sl": trade.get("sl") or meta.get("sl"),
            "tp1": trade.get("tp1") or meta.get("tp1"),
            "result": trade.get("result"),
            "pnl": trade.get("pnl"),
            "reason": trade.get("reason") or meta.get("reason"),
            "exit_reason": trade.get("exit_reason"),
            "confidence": trade.get("confidence") or meta.get("confidence"),
            "confidence_tree": trade.get("confidence_tree") or meta.get("confidence_tree"),
            "evidence": trade.get("evidence") or meta.get("evidence"),
            "market_context": mctx,
            "regime": regime.get("primary") or mctx.get("regime") or ctx.get("regime"),
            "session": mctx.get("session") or ctx.get("session"),
            "move_type": ctx.get("move_type"),
            "market_intent": ctx.get("market_intent"),
            "m5_trend": feat.get("m5_trend"),
            "m15_trend": feat.get("m15_trend"),
            "recorded_at": utc_now_iso(),
        }

    def _compute_adjustment(
        self,
        trade: dict[str, Any],
        features_data: dict[str, Any] | None,
        context_data: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        symbol = trade.get("symbol")
        setup = normalize_setup_type(trade.get("setup_type"), market_context=trade.get("market_context") or {})
        result = trade.get("result")
        feat = (features_data or {}).get("symbols", {}).get(symbol, {})

        if result == "loss" and setup in ("false_breakout", "range_fade"):
            return {
                "setup_type": setup,
                "symbol": symbol,
                "adjustment": f"Reduce confidence on {setup} — historically lower edge",
                "delta": -self.LOSS_PENALTY * 1.5,
                "created_at": utc_now_iso(),
            }

        if result == "loss" and not feat.get("timeframe_alignment"):
            return {
                "setup_type": setup,
                "symbol": symbol,
                "adjustment": "Reduce confidence on misaligned timeframe setups",
                "delta": -self.LOSS_PENALTY,
                "created_at": utc_now_iso(),
            }

        if result == "win" and setup == "trend_continuation":
            return {
                "setup_type": setup,
                "symbol": symbol,
                "adjustment": "Boost trend continuation — agent edge confirmed",
                "delta": self.WIN_BOOST * 1.5,
                "created_at": utc_now_iso(),
            }

        if result == "win":
            return {"setup_type": setup, "symbol": symbol, "adjustment": f"Winning {setup}", "delta": self.WIN_BOOST, "created_at": utc_now_iso()}
        if result == "loss":
            return {"setup_type": setup, "symbol": symbol, "adjustment": f"Losing {setup}", "delta": -self.LOSS_PENALTY, "created_at": utc_now_iso()}
        return None

    def _apply_edge_adjustment(self, edge_scores: dict[str, Any], trade: dict[str, Any], adjustment: dict[str, Any]) -> None:
        symbol = trade.get("symbol", "unknown")
        setup = trade.get("setup_type", "unknown")
        setups = edge_scores.setdefault("setups", {})
        symbol_edges = setups.setdefault(symbol, {})
        current = symbol_edges.get(setup, self.DEFAULT_EDGE)
        delta = adjustment.get("delta", 0)
        symbol_edges[setup] = max(10, min(90, current + delta))
