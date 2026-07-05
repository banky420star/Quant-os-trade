"""Closed-trade detection for paper and MT5 portfolios."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from core.entry_narrative import build_exit_narrative
from core.exit_manager import near_take_profit
from core.symbol_manager import logical_symbol
from core.strategy_policy import normalize_setup_type
from core.trade_journal import build_mgmt_narratives
from core.utils import read_json_state, utc_now_iso

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
                "be_narrative": pos.get("be_narrative"),
                "trail_narrative": pos.get("trail_narrative"),
                "be_triggered": bool(pos.get("be_triggered")),
                "trail_active": bool(pos.get("trail_active")),
                "closed_at": utc_now_iso(),
            })

        if closed_trades:
            from core.entry_staging import clear_symbol
            for trade in closed_trades:
                sym = trade.get("symbol")
                if sym:
                    clear_symbol(str(sym))
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

        # USER-AUTHORIZED 2026-07-01 fix (forward-test / organic-culturing enabler):
        # Join each closing MT5 deal back to its originating order's signal_meta
        # by position ticket, so a closed live trade carries the FULL context tuple
        # (setup | regime | bias | confidence | market_context | evidence) it needs
        # for the adaptive learners to "culture" from live results. Previously only
        # setup_type was recovered from the deal comment and regime/confidence/
        # market_context/signal_id were dropped at close time — so the ranker's
        # by_context aggregates and the win-template gate (keyed on setup|regime|
        # bias) were starved of live data and could only learn from paper/replay.
        # The opening order for any closing position lives in paper_orders.json
        # (written when the position was placed), keyed by mt5_ticket == the MT5
        # position ticket == deal.position_id for both legs of the position.
        orders_state = read_json_state("paper_orders.json", default={})
        ticket_index: dict[str, dict[str, Any]] = {}
        for o in (orders_state.get("orders") or []):
            tkt = o.get("mt5_ticket")
            if tkt and o.get("status") == "filled":
                ticket_index[str(tkt)] = o

        mgmt_state = read_json_state("position_management.json", default={"positions": {}})
        mgmt_positions = mgmt_state.get("positions") if isinstance(mgmt_state.get("positions"), dict) else {}

        incoming: list[dict[str, Any]] = []
        for deal in deals:
            if deal.magic != magic or deal.entry != mt5.DEAL_ENTRY_OUT:
                continue
            comment = deal.comment or ""
            deal_ts = datetime.fromtimestamp(int(deal.time), tz=timezone.utc).isoformat()
            pos_id = getattr(deal, "position_id", None)
            order = ticket_index.get(str(pos_id)) if pos_id else None
            meta = (order or {}).get("signal_meta") or {}
            setup_type = normalize_setup_type(
                (order or {}).get("setup_type") or meta.get("setup_type") or comment,
                meta=meta,
                market_context=meta.get("market_context") if isinstance(meta.get("market_context"), dict) else {},
            )
            # True entry price = the opening fill price recorded on the order; fall
            # back to the deal price (== exit) when no matching order is found.
            entry_price = float(order.get("fill_price")) if order and order.get("fill_price") else float(deal.price)
            exit_price = float(deal.price)
            sl_at_close = meta.get("sl")
            # OUT deals run opposite to the position side (close BUY -> SELL deal).
            if deal.entry == mt5.DEAL_ENTRY_OUT:
                pos_side = "BUY" if deal.type == mt5.DEAL_TYPE_SELL else "SELL"
            else:
                pos_side = "BUY" if deal.type == mt5.DEAL_TYPE_BUY else "SELL"
            side = (order or {}).get("side") or meta.get("side") or pos_side
            mgmt_row = dict(mgmt_positions.get(str(pos_id), {})) if pos_id else {}
            be_triggered = bool(mgmt_row.get("break_even"))
            trail_active = bool(mgmt_row.get("trailing"))
            narr = build_mgmt_narratives(
                side=side,
                entry=entry_price,
                sl=sl_at_close,
                mgmt_row=mgmt_row,
            )
            exit_reason = "mt5_close"
            tp1 = float(meta.get("tp1") or 0)
            tp2 = float(meta.get("tp2") or 0)
            if mgmt_row.get("partial_tp_done") and deal.comment and "partial" in deal.comment.lower():
                exit_reason = "partial_take_profit"
            elif near_take_profit(exit_price, tp2 if mgmt_row.get("partial_tp_done") and tp2 > 0 else tp1):
                exit_reason = "take_profit"
            elif trail_active:
                exit_reason = "trailing_stop"
            elif be_triggered:
                exit_reason = "break_even_stop"
            exit_narrative = build_exit_narrative(
                side,
                entry_price,
                float(sl_at_close or entry_price),
                float(meta.get("tp1") or entry_price),
                exit_price,
                exit_reason,
                float(deal.profit),
            )
            incoming.append({
                "trade_id": str(deal.ticket),
                "mt5_deal": deal.ticket,
                "mt5_position": int(pos_id) if pos_id else None,
                "signal_id": (order or {}).get("signal_id") or meta.get("signal_id"),
                "symbol": logical_symbol(deal.symbol),
                "side": side,
                "entry": entry_price,
                "exit": exit_price,
                "sl": sl_at_close,
                "tp1": meta.get("tp1"),
                "pnl": float(deal.profit),
                "result": "win" if deal.profit > 0 else "loss",
                "exit_reason": exit_reason,
                "setup_type": (order or {}).get("setup_type") or meta.get("setup_type") or setup_type or "unknown",
                "reason": (order or {}).get("reason") or meta.get("reason"),
                "signal_meta": meta,
                "confidence": meta.get("confidence"),
                "confidence_tree": meta.get("confidence_tree"),
                "evidence": meta.get("evidence"),
                "market_context": meta.get("market_context"),
                "move_type": meta.get("market_context", {}).get("move_type") if isinstance(meta.get("market_context"), dict) else None,
                "market_intent": meta.get("market_context", {}).get("market_intent") if isinstance(meta.get("market_context"), dict) else None,
                "be_narrative": narr.get("be"),
                "trail_narrative": narr.get("trail"),
                "exit_narrative": exit_narrative,
                "be_triggered": be_triggered,
                "trail_active": trail_active,
                "position_mgmt": mgmt_row,
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
