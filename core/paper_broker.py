"""Paper trading broker — simulated fills only, never live MT5 orders."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from core.exposure import calc_risk_based_size, cap_size_to_exposure_limits
from core.dynamic_entry import evaluate_dynamic_entry, symbol_capacity_available
from core.trade_limits import is_duplicate_position, session_trade_capacity_available
from core.utils import utc_now_iso


class PaperBroker:
    """Simulate order fills and track paper portfolio."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("paper_broker")
        if config["execution"].get("mode") == "mt5":
            raise RuntimeError("PaperBroker cannot run when execution.mode is mt5 — use MT5Broker")

    def process_approved_signals(
        self,
        approved: list[dict[str, Any]],
        prices: dict[str, float],
        existing_orders: list[dict] | None = None,
        existing_positions: list[dict] | None = None,
        existing_trades: list[dict] | None = None,
        balance_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create paper orders and simulate fills from latest prices."""
        orders = list(existing_orders or [])
        positions = list(existing_positions or [])
        trades = list(existing_trades or [])

        starting_cash = float(self.config["execution"].get("starting_cash", 1000))
        balance = balance_state or {"cash": starting_cash, "equity": starting_cash, "starting_cash": starting_cash}

        risk_pct = float(self.config["signals"].get("default_risk_percent", 1))
        executed_signal_ids = {p.get("signal_id") for p in positions if p.get("signal_id")}

        for record in approved:
            signal = record.get("signal", record)
            if not symbol_capacity_available(self.config, signal["symbol"], positions):
                self.logger.info("Paper skip %s — max open per symbol", signal["symbol"])
                continue
            if not session_trade_capacity_available(self.config, signal["symbol"], trades):
                self.logger.info("Paper skip %s — max session trades per symbol", signal["symbol"])
                continue
            if is_duplicate_position(
                self.config,
                signal,
                positions,
                executed_signal_ids=executed_signal_ids,
            ):
                continue

            symbol = signal["symbol"]
            feat = {"price": prices.get(symbol, signal.get("entry")), "atr": signal.get("entry", 1) * 0.001}
            dyn_ok, signal, dyn_reason = evaluate_dynamic_entry(self.config, signal, positions, feat)
            if not dyn_ok:
                self.logger.info("Paper skip %s — %s", symbol, dyn_reason)
                orders.append(self._create_rejected_order(signal, feat["price"], dyn_reason or "dynamic_entry"))
                continue

            price = prices.get(symbol, signal.get("entry"))
            if not price:
                self.logger.warning("No price for %s — skipping", symbol)
                continue

            equity = float(balance.get("equity", balance.get("cash", starting_cash)))
            ideal_size = calc_risk_based_size(
                equity,
                risk_pct,
                float(signal.get("entry", price)),
                float(signal["sl"]),
            )
            capped_size, allowed = cap_size_to_exposure_limits(
                ideal_size,
                float(signal.get("entry", price)),
                symbol,
                positions,
                self.config,
            )
            if not allowed:
                self.logger.warning(
                    "Paper trade blocked — exposure limit exceeded for %s %s",
                    symbol,
                    signal["side"],
                )
                orders.append(self._create_rejected_order(signal, price, "exposure_limit_exceeded"))
                continue

            order = self._create_order(signal, price)
            orders.append(order)

            fill_price = float(signal.get("entry", price)) if order.get("type") == "limit" else price
            if self._should_fill(order, price):
                position, trade, pnl = self._fill_order(order, fill_price, capped_size)
                positions.append(position)
                if trade:
                    trades.append(trade)
                balance["cash"] += pnl
                order["status"] = "filled"
                order["filled_at"] = utc_now_iso()
                order["size"] = capped_size
                self.logger.info(
                    "Paper fill: %s %s @ %s size=%.4f (equity=%.2f)",
                    symbol,
                    signal["side"],
                    price,
                    capped_size,
                    equity,
                )

        balance["equity"] = balance["cash"] + self._unrealized_pnl(positions, prices)
        closed_positions, new_trades, realized = self._check_exits(positions, prices)
        positions = [p for p in positions if p["position_id"] not in {c["position_id"] for c in closed_positions}]
        trades.extend(new_trades)
        balance["cash"] += realized
        balance["equity"] = balance["cash"] + self._unrealized_pnl(positions, prices)

        return {
            "timestamp": utc_now_iso(),
            "mode": "paper",
            "balance": balance,
            "orders": orders,
            "positions": positions,
            "trades": trades,
            "closed_positions": closed_positions,
        }

    @staticmethod
    def _signal_meta(signal: dict[str, Any]) -> dict[str, Any]:
        return {
            "signal_id": signal.get("signal_id"),
            "symbol": signal.get("symbol"),
            "side": signal.get("side"),
            "setup_type": signal.get("setup_type"),
            "entry": signal.get("entry"),
            "sl": signal.get("sl"),
            "tp1": signal.get("tp1"),
            "confidence": signal.get("confidence"),
            "confidence_tree": signal.get("confidence_tree"),
            "evidence": signal.get("evidence"),
            "market_context": signal.get("market_context"),
            "reason": signal.get("reason"),
            "strategy_rank": signal.get("strategy_rank"),
        }

    def _create_order(self, signal: dict[str, Any], price: float) -> dict[str, Any]:
        order_type = signal.get("order_type", "market")
        return {
            "order_id": str(uuid.uuid4()),
            "signal_id": signal["signal_id"],
            "symbol": signal["symbol"],
            "side": signal["side"],
            "type": order_type,
            "entry": signal["entry"],
            "sl": signal["sl"],
            "tp1": signal["tp1"],
            "tp2": signal.get("tp2"),
            "price": price,
            "status": "pending",
            "created_at": utc_now_iso(),
            "setup_type": signal.get("setup_type"),
            "reason": signal.get("reason"),
            "signal_meta": self._signal_meta(signal),
        }

    def _create_rejected_order(self, signal: dict[str, Any], price: float, reason: str) -> dict[str, Any]:
        return {
            "order_id": str(uuid.uuid4()),
            "signal_id": signal["signal_id"],
            "symbol": signal["symbol"],
            "side": signal["side"],
            "type": "market",
            "entry": signal["entry"],
            "sl": signal["sl"],
            "tp1": signal["tp1"],
            "price": price,
            "status": "rejected",
            "error": reason,
            "created_at": utc_now_iso(),
            "setup_type": signal.get("setup_type"),
        }

    def _should_fill(self, order: dict[str, Any], price: float) -> bool:
        if order["type"] == "market":
            return True
        if order["side"] == "BUY":
            return price <= order["entry"]
        return price >= order["entry"]

    def _fill_order(
        self,
        order: dict[str, Any],
        fill_price: float,
        size: float,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, float]:
        position = {
            "position_id": str(uuid.uuid4()),
            "order_id": order["order_id"],
            "signal_id": order["signal_id"],
            "symbol": order["symbol"],
            "side": order["side"],
            "entry": fill_price,
            "sl": order["sl"],
            "tp1": order["tp1"],
            "tp2": order.get("tp2"),
            "size": size,
            "opened_at": utc_now_iso(),
            "setup_type": order.get("setup_type"),
            "reason": order.get("reason"),
            "signal_meta": order.get("signal_meta"),
        }
        return position, None, 0.0

    def _unrealized_pnl(self, positions: list[dict], prices: dict[str, float]) -> float:
        total = 0.0
        for pos in positions:
            price = prices.get(pos["symbol"], pos["entry"])
            diff = price - pos["entry"]
            if pos["side"] == "SELL":
                diff = -diff
            total += diff * pos["size"]
        return total

    def _check_exits(self, positions: list[dict], prices: dict[str, float]) -> tuple[list, list, float]:
        closed = []
        trades = []
        realized = 0.0

        for pos in positions:
            price = prices.get(pos["symbol"], pos["entry"])
            exit_reason = None
            exit_price = price

            if pos["side"] == "BUY":
                if price <= pos["sl"]:
                    exit_reason = "stop_loss"
                    exit_price = pos["sl"]
                elif price >= pos["tp1"]:
                    exit_reason = "take_profit"
                    exit_price = pos["tp1"]
            else:
                if price >= pos["sl"]:
                    exit_reason = "stop_loss"
                    exit_price = pos["sl"]
                elif price <= pos["tp1"]:
                    exit_reason = "take_profit"
                    exit_price = pos["tp1"]

            if exit_reason:
                diff = exit_price - pos["entry"]
                if pos["side"] == "SELL":
                    diff = -diff
                pnl = diff * pos["size"]
                realized += pnl
                closed.append({**pos, "closed_at": utc_now_iso(), "exit_price": exit_price, "exit_reason": exit_reason, "pnl": round(pnl, 2)})
                meta = pos.get("signal_meta") or {}
                trades.append(
                    {
                        "trade_id": str(uuid.uuid4()),
                        "position_id": pos["position_id"],
                        "signal_id": pos["signal_id"],
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
                    }
                )
        return closed, trades, realized