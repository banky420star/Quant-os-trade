"""Closed-trade detection for paper and MT5 portfolios."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from core.utils import utc_now_iso

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore


class TradeTracker:
    """Detect newly closed trades and merge into trade history."""

    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger("trade_tracker")

    def merge_new_trades(
        self,
        existing_trades: list[dict[str, Any]],
        incoming: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Append only trades not already recorded. Returns (all_trades, newly_added)."""
        known_ids = {t.get("trade_id") for t in existing_trades if t.get("trade_id")}
        merged = list(existing_trades)
        added: list[dict[str, Any]] = []
        for trade in incoming:
            tid = trade.get("trade_id")
            if tid and tid in known_ids:
                continue
            merged.append(trade)
            added.append(trade)
            if tid:
                known_ids.add(tid)
        return merged, added

    def detect_paper_closed(
        self,
        previous_positions: list[dict[str, Any]],
        current_positions: list[dict[str, Any]],
        prices: dict[str, float],
    ) -> list[dict[str, Any]]:
        """Infer closed paper positions when they disappear between cycles."""
        current_ids = {p.get("position_id") for p in current_positions}
        closed_trades: list[dict[str, Any]] = []

        for pos in previous_positions:
            pid = pos.get("position_id")
            if pid in current_ids:
                continue
            price = prices.get(pos["symbol"], pos.get("entry", 0))
            exit_price = price
            exit_reason = "closed_externally"
            pnl = self._calc_pnl(pos, exit_price)
            meta = pos.get("signal_meta") or {}
            closed_trades.append({
                "trade_id": str(uuid.uuid4()),
                "position_id": pid,
                "signal_id": pos.get("signal_id"),
                "symbol": pos["symbol"],
                "side": pos["side"],
                "entry": pos["entry"],
                "exit": exit_price,
                "sl": pos.get("sl"),
                "tp1": pos.get("tp1"),
                "pnl": round(pnl, 2),
                "result": "win" if pnl > 0 else "loss",
                "exit_reason": exit_reason,
                "setup_type": pos.get("setup_type"),
                "reason": pos.get("reason"),
                "signal_meta": meta,
                "confidence": meta.get("confidence"),
                "confidence_tree": meta.get("confidence_tree"),
                "evidence": meta.get("evidence"),
                "market_context": meta.get("market_context"),
                "closed_at": utc_now_iso(),
            })

        if closed_trades:
            self.logger.info("Detected %d paper closed trades", len(closed_trades))
        return closed_trades

    def sync_mt5_closed_deals(
        self,
        existing_trades: list[dict[str, Any]],
        magic: int,
        days: int = 30,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Fetch MT5 OUT deals with agent magic and merge into trade history."""
        if mt5 is None:
            return existing_trades, []

        from datetime import datetime, timedelta, timezone

        since = datetime.now(timezone.utc) - timedelta(days=days)
        deals = mt5.history_deals_get(since, datetime.now(timezone.utc))
        if not deals:
            return existing_trades, []

        incoming: list[dict[str, Any]] = []
        for deal in deals:
            if deal.magic != magic or deal.entry != mt5.DEAL_ENTRY_OUT:
                continue
            comment = deal.comment or ""
            setup_type = comment.replace("qagent_", "") if comment.startswith("qagent_") else comment
            deal_ts = datetime.fromtimestamp(int(deal.time), tz=timezone.utc).isoformat()
            incoming.append({
                "trade_id": str(deal.ticket),
                "mt5_deal": deal.ticket,
                "symbol": deal.symbol,
                "side": "BUY" if deal.type == mt5.DEAL_TYPE_BUY else "SELL",
                "entry": float(deal.price),
                "exit": float(deal.price),
                "pnl": float(deal.profit),
                "result": "win" if deal.profit > 0 else "loss",
                "exit_reason": "mt5_close",
                "setup_type": setup_type or "unknown",
                "closed_at": deal_ts,
            })

        merged, added = self.merge_new_trades(existing_trades, incoming)
        if added:
            wins = sum(1 for t in added if t.get("result") == "win")
            losses = len(added) - wins
            self.logger.info("MT5 closed trades: %d new (%d wins, %d losses)", len(added), wins, losses)
        return merged, added

    @staticmethod
    def _calc_pnl(pos: dict[str, Any], exit_price: float) -> float:
        diff = exit_price - float(pos.get("entry", 0))
        if pos.get("side") == "SELL":
            diff = -diff
        return diff * float(pos.get("size", 0.01))