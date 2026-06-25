"""MT5 Broker — place real orders on the connected MT5 account (demo or live)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from core.exposure import calc_risk_based_size, cap_size_to_exposure_limits
from core.position_sync import _setup_type_from_comment
from core.dynamic_entry import pyramid_layer_index, scale_lot_for_layer, symbol_capacity_available
from core.trade_limits import (
    enrich_positions_with_orders,
    is_duplicate_position,
    session_trade_capacity_available,
)
from core.strategy_entry import resolve_mt5_pending_type, strategy_entries_enabled
from core.trade_tracker import TradeTracker
from core.utils import read_json_state, utc_now_iso, write_json_state

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore


class MT5Broker:
    """Execute approved signals via mt5.order_send(). Requires verifier approval."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("mt5_broker")
        self.exec_cfg = config.get("execution", {})
        self.magic = int(self.exec_cfg.get("magic_number", 20250625))
        self.deviation = int(self.exec_cfg.get("deviation", 20))

    def process_approved_signals(
        self,
        approved: list[dict[str, Any]],
        existing_orders: list[dict] | None = None,
        existing_trades: list[dict] | None = None,
        existing_positions: list[dict] | None = None,
        executed_signal_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Place market orders on MT5 for approved signals."""
        if mt5 is None:
            raise RuntimeError("MetaTrader5 package not installed")

        account = mt5.account_info()
        if account is None:
            raise RuntimeError(f"Not logged in to MT5: {mt5.last_error()}")

        self._validate_account_mode(account)

        orders = list(existing_orders or [])
        trades = list(existing_trades or [])
        prior_positions = list(existing_positions or [])
        executed = executed_signal_ids or self._executed_signal_ids(orders)
        placed: list[dict] = []
        errors: list[dict] = []
        open_positions = enrich_positions_with_orders(self._sync_positions(), orders)

        for record in approved:
            signal = record.get("signal", record)
            sid = signal.get("signal_id")
            if sid in executed:
                self.logger.info("Skip %s — signal already executed", sid)
                continue

            if not symbol_capacity_available(self.config, signal["symbol"], open_positions):
                self.logger.info(
                    "Skip %s — max open positions per symbol reached",
                    signal["symbol"],
                )
                continue

            if not session_trade_capacity_available(self.config, signal["symbol"], trades):
                self.logger.info(
                    "Skip %s — max session trades per symbol reached",
                    signal["symbol"],
                )
                continue

            if is_duplicate_position(
                self.config,
                signal,
                open_positions,
                executed_signal_ids=executed,
            ):
                self.logger.info(
                    "Skip %s %s %s — pyramid/duplicate rules",
                    signal["symbol"],
                    signal["side"],
                    signal.get("setup_type"),
                )
                continue

            result = self._place_order(signal, account, open_positions)
            order_record = self._build_order_record(signal, result)
            orders.append(order_record)

            if result.get("success"):
                placed.append(order_record)
                self.logger.info(
                    "MT5 order placed: %s %s lot=%s ticket=%s",
                    signal["symbol"],
                    signal["side"],
                    result.get("volume"),
                    result.get("ticket"),
                )
            else:
                errors.append(order_record)
                self.logger.error(
                    "MT5 order failed: %s %s — %s",
                    signal["symbol"],
                    signal["side"],
                    result.get("error"),
                )

        positions = enrich_positions_with_orders(self._sync_positions(), orders)
        balance = self._account_balance(account)
        tracker = TradeTracker(self.logger)
        trades, new_closed = tracker.sync_mt5_closed_deals(trades, self.magic)

        return {
            "timestamp": utc_now_iso(),
            "mode": "mt5",
            "account": {
                "login": account.login,
                "server": account.server,
                "balance": float(account.balance),
                "equity": float(account.equity),
                "account_mode": self._account_mode_name(account),
            },
            "balance": balance,
            "orders": orders,
            "positions": positions,
            "placed": placed,
            "errors": errors,
            "trades": trades,
            "new_closed_trades": new_closed,
        }

    def _validate_account_mode(self, account: Any) -> None:
        expected = self.config.get("mt5", {}).get("account_mode", "demo")
        trade_mode = int(getattr(account, "trade_mode", -1))
        is_demo = trade_mode == 0

        if not self.exec_cfg.get("mt5_trading_enabled", False):
            raise RuntimeError("MT5 trading disabled — set execution.mt5_trading_enabled: true")

        if expected == "demo" and not is_demo:
            if not self.exec_cfg.get("allow_live_account", False):
                raise RuntimeError(
                    "Refusing to trade on non-demo account. "
                    "Set execution.allow_live_account: true to override (real money risk)."
                )
            self.logger.warning("Trading on LIVE account — real money at risk")

        terminal = mt5.terminal_info()
        if terminal and not terminal.trade_allowed:
            raise RuntimeError(
                "MT5 Algo Trading is OFF — enable the 'Algo Trading' button in the toolbar "
                "and uncheck 'Disable algorithmic trading via external Python API' in "
                "Tools -> Options -> Expert Advisors"
            )
        if not account.trade_allowed:
            raise RuntimeError("MT5 account trade_allowed=False — check broker/account permissions")

    def _place_order(
        self,
        signal: dict[str, Any],
        account: Any,
        open_positions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        symbol = signal["symbol"]
        side = signal["side"]

        if not mt5.symbol_select(symbol, True):
            return {"success": False, "error": f"symbol_select failed: {mt5.last_error()}"}

        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if info is None or tick is None:
            return {"success": False, "error": f"no symbol info: {mt5.last_error()}"}

        volume = self._calc_volume(signal, account, info, open_positions or [])
        if volume <= 0:
            return {"success": False, "error": "exposure_limit_exceeded"}

        sl = float(signal["sl"])
        tp = float(signal["tp1"])
        filling = self._filling_mode(info)
        strategy_entry = float(signal.get("entry", 0))
        use_strategy = strategy_entries_enabled(self.config)
        entry_mode = signal.get("entry_mode", "market")
        bid = float(tick.bid)
        ask = float(tick.ask)

        if use_strategy and entry_mode == "limit" and strategy_entry > 0:
            if signal.get("within_reach") is False:
                return {
                    "success": False,
                    "error": f"entry_too_far_from_market:{signal.get('distance_atr')}atr",
                }
            _type_id, type_name = resolve_mt5_pending_type(side, strategy_entry, bid, ask)
            type_map = {
                "buy_limit": mt5.ORDER_TYPE_BUY_LIMIT,
                "buy_stop": mt5.ORDER_TYPE_BUY_STOP,
                "sell_limit": mt5.ORDER_TYPE_SELL_LIMIT,
                "sell_stop": mt5.ORDER_TYPE_SELL_STOP,
            }
            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": symbol,
                "volume": volume,
                "type": type_map[type_name],
                "price": strategy_entry,
                "sl": sl,
                "tp": tp,
                "deviation": self.deviation,
                "magic": self.magic,
                "comment": f"qagent_{signal.get('setup_type', 'signal')[:20]}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling,
            }
        else:
            if side == "BUY":
                order_type = mt5.ORDER_TYPE_BUY
                price = ask
            else:
                order_type = mt5.ORDER_TYPE_SELL
                price = bid
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "price": price,
                "sl": sl,
                "tp": tp,
                "deviation": self.deviation,
                "magic": self.magic,
                "comment": f"qagent_{signal.get('setup_type', 'signal')[:20]}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling,
            }

        self.logger.info("Sending order: %s", {k: v for k, v in request.items() if k != "comment"})
        result = mt5.order_send(request)

        if result is None:
            return {"success": False, "error": str(mt5.last_error())}

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return {
                "success": False,
                "error": f"retcode={result.retcode} {result.comment}",
                "retcode": result.retcode,
            }

        return {
            "success": True,
            "ticket": result.order,
            "deal": result.deal,
            "volume": volume,
            "price": result.price,
            "comment": result.comment,
        }

    def _calc_volume(
        self,
        signal: dict[str, Any],
        account: Any,
        info: Any,
        open_positions: list[dict[str, Any]],
    ) -> float:
        risk_pct = float(self.config["signals"].get("default_risk_percent", 1))
        max_lot = float(self.exec_cfg.get("max_lot", 0.1))
        default_lot = float(self.exec_cfg.get("default_lot", 0.01))
        equity = float(account.equity)

        entry = float(signal.get("entry", 0))
        sl = float(signal["sl"])
        risk_dist = abs(entry - sl)

        if risk_dist <= 0:
            ideal = default_lot
        else:
            risk_money = equity * (risk_pct / 100.0)
            tick_value = float(getattr(info, "trade_tick_value", 0) or 0)
            tick_size = float(getattr(info, "trade_tick_size", 0) or info.point or 0)
            if tick_value > 0 and tick_size > 0:
                ticks = risk_dist / tick_size
                ideal = risk_money / (ticks * tick_value)
            else:
                ideal = calc_risk_based_size(equity, risk_pct, entry, sl, max_size=max_lot)

        vmin = float(info.volume_min or 0.01)
        ideal = min(ideal, max_lot)
        # Micro accounts: tick-based risk sizing can fall below broker minimum lot.
        if ideal < vmin:
            ideal = min(max(default_lot, vmin), max_lot)
        capped, allowed = cap_size_to_exposure_limits(
            ideal,
            entry,
            signal["symbol"],
            open_positions,
            self.config,
            min_size=vmin,
        )
        if not allowed:
            return 0.0
        vol = max(capped, vmin)
        layer = int(signal.get("pyramid_layer", pyramid_layer_index(signal, open_positions)))
        vol = scale_lot_for_layer(self.config, signal["symbol"], vol, layer)
        return self._normalize_volume(vol, info)

    def _normalize_volume(self, volume: float, info: Any) -> float:
        step = float(info.volume_step or 0.01)
        vmin = float(info.volume_min or step)
        vmax = float(info.volume_max or 100.0)
        vol = max(vmin, min(vmax, volume))
        steps = round(vol / step)
        return round(steps * step, 2)

    def _filling_mode(self, info: Any) -> int:
        filling = int(getattr(info, "filling_mode", 0))
        if filling & 1:
            return mt5.ORDER_FILLING_FOK
        if filling & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def _sync_positions(self) -> list[dict[str, Any]]:
        positions = mt5.positions_get()
        if not positions:
            return []
        synced = []
        for pos in positions:
            if pos.magic != self.magic:
                continue
            synced.append({
                "position_id": str(pos.ticket),
                "ticket": pos.ticket,
                "symbol": pos.symbol,
                "side": "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL",
                "entry": float(pos.price_open),
                "sl": float(pos.sl),
                "tp1": float(pos.tp),
                "size": float(pos.volume),
                "profit": float(pos.profit),
                "opened_at": utc_now_iso(),
                "setup_type": _setup_type_from_comment(pos.comment),
                "magic": pos.magic,
                "comment": pos.comment,
            })
        return synced

    def _account_balance(self, account: Any) -> dict[str, float]:
        baseline = read_json_state("mt5_baseline.json", default={})
        login = int(account.login)
        if baseline.get("login") != login or not baseline.get("starting_cash"):
            starting = float(account.balance)
            write_json_state("mt5_baseline.json", {
                "login": login,
                "server": account.server,
                "starting_cash": starting,
                "set_at": utc_now_iso(),
            })
        else:
            starting = float(baseline["starting_cash"])
        return {
            "cash": float(account.balance),
            "equity": float(account.equity),
            "starting_cash": starting,
        }

    def _account_mode_name(self, account: Any) -> str:
        return {0: "demo", 1: "contest", 2: "real"}.get(int(account.trade_mode), "unknown")

    def _executed_signal_ids(self, orders: list[dict]) -> set[str]:
        return {o["signal_id"] for o in orders if o.get("status") == "filled" and o.get("signal_id")}

    def _build_order_record(self, signal: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        return {
            "order_id": str(uuid.uuid4()),
            "signal_id": signal["signal_id"],
            "symbol": signal["symbol"],
            "side": signal["side"],
            "type": "market",
            "entry": signal["entry"],
            "sl": signal["sl"],
            "tp1": signal["tp1"],
            "tp2": signal.get("tp2"),
            "setup_type": signal.get("setup_type"),
            "reason": signal.get("reason"),
            "signal_meta": {
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
            },
            "status": "filled" if result.get("success") else "failed",
            "mt5_ticket": result.get("ticket"),
            "mt5_deal": result.get("deal"),
            "fill_price": result.get("price"),
            "volume": result.get("volume"),
            "error": result.get("error"),
            "created_at": utc_now_iso(),
            "filled_at": utc_now_iso() if result.get("success") else None,
        }