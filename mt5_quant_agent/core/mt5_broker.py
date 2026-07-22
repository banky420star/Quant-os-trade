"""MT5 Broker — place real orders on the connected MT5 account (demo or live)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from core.blue_guardian import (
    blue_guardian_enabled,
    can_close_position,
    entry_gates,
    record_position_open,

)
from core.learning_logger import log_decision
from core.learning_schema import build_decision_event, config_snapshot_hash
from core.position_sizing import calc_executable_volume, symbol_spec_from_mt5
from core.position_sync import _setup_type_from_comment
from core.dynamic_entry import symbol_capacity_available
from core.trade_limits import (
    enrich_positions_with_orders,
    is_duplicate_position,
    session_trade_capacity_available,
)
from core.strategy_entry import resolve_mt5_pending_type, strategy_entries_enabled
from core.symbol_manager import broker_symbol, logical_symbol
from core.trade_tracker import TradeTracker
from core.trade_journal import snapshot_signal_meta
from core.utils import read_json_state, utc_now_iso, write_json_state

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore


def _json_safe_kelly(kelly: dict[str, Any] | None) -> dict[str, Any] | None:
    """Make a Kelly verdict dict JSON-safe (math.inf -> None) for state files."""
    if not isinstance(kelly, dict):
        return None
    safe: dict[str, Any] = {}
    for k, v in kelly.items():
        if isinstance(v, float) and v != v:  # NaN
            safe[k] = None
        elif isinstance(v, float) and v in (float("inf"), float("-inf")):
            safe[k] = None
        else:
            safe[k] = v
    return safe


class MT5Broker:
    """Execute approved signals via mt5.order_send(). Requires verifier approval."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("mt5_broker")
        self.exec_cfg = config.get("execution", {})
        self.magic = int(self.exec_cfg.get("magic_number", 20250625))
        self.deviation = int(self.exec_cfg.get("deviation", 20))

    def _log_decision(self, signal: dict[str, Any], decision: str, reason: str,
                        features_at_entry: dict[str, Any] | None = None) -> None:
        """Log one decision event to logs/decisions.jsonl."""
        try:
            feat = features_at_entry or {}
            log_decision(build_decision_event(
                symbol=signal.get("symbol"),
                side=signal.get("side"),
                timeframe="M5",
                mode=self.config.get("execution", {}).get("mode", "mt5"),
                price=signal.get("entry"),
                spread_points=feat.get("spread_points") if isinstance(feat, dict) else None,
                atr=feat.get("atr") if isinstance(feat, dict) else None,
                volatility_regime=feat.get("volatility_regime") if isinstance(feat, dict) else None,
                confidence=signal.get("confidence"),
                decision=decision,
                entry_type=signal.get("entry_mode", "market"),
                guards={},
                reason=reason,
                config_hash=config_snapshot_hash(self.config),
                profile=self.config.get("active_profile"),
            ))
        except Exception:
            self.logger.debug("Decision log skipped for %s: %s", signal.get("signal_id"), decision)

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
        features_data = read_json_state("features.json", default={"symbols": {}})

        for record in approved:
            signal = record.get("signal", record)
            sid = signal.get("signal_id")
            if sid in executed:
                self.logger.info("Skip %s — signal already executed", sid)
                self._log_decision(signal, "skip", "already_executed")
                continue

            # --- Regime-flip trade replacement (USER-AUTHORIZED 2026-06-30) ---
            # When M15 trend AND market_regime bias both flipped (bullish<->bearish)
            # vs the prior tick, and this signal has high confidence (>=
            # regime_flip_min_confidence) in the OPPOSITE direction to an OPEN
            # position that is currently LOSING (profit < 0), close that losing
            # position and let this signal replace it. Cuts losers on a confirmed
            # turn; never touches a winning runner. See state/regime_history.json.
            flip_cfg = self.config.get("trading", {}) or {}
            if bool(flip_cfg.get("regime_flip_replace_enabled", False)):
                flip = self._detect_regime_flip(signal["symbol"], flip_cfg)
                if flip is not None:
                    min_conf = float(flip_cfg.get("regime_flip_min_confidence", 75))
                    if float(signal.get("confidence", 0) or 0) >= min_conf:
                        loser = self._find_losing_opposite(
                            open_positions, signal["symbol"], signal["side"]
                        )
                        if loser is not None:
                            self.logger.info(
                                "REGIME-FLIP REPLACE: closing losing %s %s #%s "
                                "profit=%.2f to replace with %s %s conf=%s",
                                loser["symbol"], loser["side"], loser["ticket"],
                                float(loser.get("profit", 0) or 0),
                                signal["symbol"], signal["side"],
                                signal.get("confidence"),
                            )
                            cr = self.close_position(
                                loser["ticket"], loser["symbol"],
                                loser["side"], loser["size"],
                            )
                            if cr.get("success"):
                                # Free the slot locally so the skip-chain + sizing
                                # below see the freed capacity (the MT5 close is
                                # async; the sync at the end of the loop re-reads).
                                open_positions = [
                                    p for p in open_positions
                                    if p.get("ticket") != loser["ticket"]
                                ]
                                self.logger.info(
                                    "REGIME-FLIP REPLACE: closed #%s -> placing "
                                    "replacement %s %s",
                                    loser["ticket"], signal["symbol"], signal["side"],
                                )
                            else:
                                self.logger.error(
                                    "REGIME-FLIP REPLACE: close #%s failed: %s "
                                    "-> skip replacement",
                                    loser["ticket"], cr.get("error"),
                                )
                                self._log_decision(signal, "skip", f"regime_flip_replace_failed: {cr.get('error')}")
                                continue
                    else:
                        self.logger.info(
                            "REGIME-FLIP REPLACE: flip on %s but conf %s < %s -> "
                            "no replace",
                            signal["symbol"], signal.get("confidence"), min_conf,
                        )
                        self._log_decision(signal, "skip", "regime_flip_confidence_too_low")

            bg_ok, _bg_code, _bg = entry_gates(self.config, open_positions, signal)
            if not bg_ok:
                self.logger.info(
                    "Skip %s — Blue Guardian entry gate",
                    signal["symbol"],
                )
                self._log_decision(signal, "skip", "blue_guardian_entry_gate")
                continue

            if not symbol_capacity_available(self.config, signal["symbol"], open_positions):
                self.logger.info(
                    "Skip %s — max open positions per symbol reached",
                    signal["symbol"],
                )
                self._log_decision(signal, "skip", "symbol_capacity_exceeded")
                continue

            if not session_trade_capacity_available(self.config, signal["symbol"], trades):
                self.logger.info(
                    "Skip %s — max session trades per symbol reached",
                    signal["symbol"],
                )
                self._log_decision(signal, "skip", "session_trade_capacity_exceeded")
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
                self._log_decision(signal, "skip", "duplicate_position")
                continue

            result = self._place_order(signal, account, open_positions)
            sym_feat = features_data.get("symbols", {}).get(signal["symbol"], {})
            order_record = self._build_order_record(signal, result, features_at_entry=sym_feat)
            orders.append(order_record)

            if result.get("success"):
                placed.append(order_record)
                ticket = result.get("ticket")
                if ticket is not None:
                    record_position_open(ticket)
                self._record_open_confidence(result.get("ticket"), signal.get("confidence"))
                # Re-sync so Blue Guardian max-total gate applies within this batch.
                open_positions = enrich_positions_with_orders(self._sync_positions(), orders)
                self._log_decision(signal, "execute", "order_placed", features_at_entry=sym_feat)
                self.logger.info(
                    "MT5 order placed: %s %s lot=%s ticket=%s",
                    signal["symbol"],
                    signal["side"],
                    result.get("volume"),
                    result.get("ticket"),
                )
            else:
                errors.append(order_record)
                self._log_decision(signal, "error", str(result.get("error", "order_failed")))
                self.logger.error(
                    "MT5 order failed: %s %s — %s",
                    signal["symbol"],
                    signal["side"],
                    result.get("error"),
                )

        positions = enrich_positions_with_orders(self._sync_positions(), orders)
        self._prune_open_confidence(positions)
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
        logical_sym = signal["symbol"]
        symbol = broker_symbol(logical_sym)
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
        # 2026-07-21 partial-TP fix: broker pre-empts the bot's
        # partial TP at TP1 if we send TP1 as limit here. Send TP2
        # so the bot can partial-close 50% at TP1, then the broker
        # fires the remaining 50% at TP2 = exactly the partial-TP
        # semantics. Falls back to TP1 if signal lacks tp2.
        tp = float(signal.get("tp2") or signal.get("tp1") or 0)
        filling = self._filling_mode(info)
        strategy_entry = float(signal.get("entry", 0))
        use_strategy = strategy_entries_enabled(self.config)
        entry_mode = signal.get("entry_mode", "market")
        bid = float(tick.bid)
        ask = float(tick.ask)

        order_kind = "market"
        is_pending = False
        requested_price = None

        if use_strategy and entry_mode == "limit" and strategy_entry > 0:
            if signal.get("within_reach") is False:
                return {
                    "success": False,
                    "error": f"entry_too_far_from_market:{signal.get('distance_atr')}atr",
                }
            _type_id, type_name = resolve_mt5_pending_type(side, strategy_entry, bid, ask)
            order_kind = type_name
            is_pending = True
            requested_price = strategy_entry
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

        ok_retcodes = {mt5.TRADE_RETCODE_DONE}
        placed_code = getattr(mt5, "TRADE_RETCODE_PLACED", None)
        if placed_code is not None:
            ok_retcodes.add(placed_code)
        if result.retcode not in ok_retcodes:
            return {
                "success": False,
                "error": f"retcode={result.retcode} {result.comment}",
                "retcode": result.retcode,
                "order_kind": order_kind,
                "pending": is_pending,
                "requested_price": requested_price,
            }

        return {
            "success": True,
            "ticket": result.order,
            "deal": result.deal,
            "volume": volume,
            "price": result.price,
            "comment": result.comment,
            "retcode": result.retcode,
            "order_kind": order_kind,
            "pending": is_pending,
            "requested_price": requested_price,
        }

    def _calc_volume(
        self,
        signal: dict[str, Any],
        account: Any,
        info: Any,
        open_positions: list[dict[str, Any]],
    ) -> float:
        vol, details = calc_executable_volume(
            signal,
            equity=float(account.equity),
            balance=float(account.balance),
            config=self.config,
            symbol_spec=symbol_spec_from_mt5(info),
            open_positions=open_positions,
            stamp_kelly=True,
        )
        if vol <= 0 and details.get("reject_reason") == "min_lot_stop_risk_exceeds_cap":
            self.logger.info(
                "Skip %s — min lot stop risk $%.2f exceeds cap $%.2f",
                signal["symbol"],
                float(details.get("stop_loss_usd", 0)),
                float(details.get("risk_cap_usd", 0)),
            )
        return vol

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
                "symbol": logical_symbol(pos.symbol),
                "broker_symbol": pos.symbol,
                "side": "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL",
                "entry": float(pos.price_open),
                "sl": float(pos.sl),
                "tp1": float(pos.tp),
                "size": float(pos.volume),
                "profit": float(pos.profit),
                "opened_at": datetime.fromtimestamp(int(pos.time), tz=timezone.utc).isoformat(),
                "setup_type": _setup_type_from_comment(pos.comment),
                "magic": pos.magic,
                "comment": pos.comment,
            })
        return synced

    def _record_open_confidence(self, ticket: Any, confidence: Any) -> None:
        """Persist {ticket: confidence} for the confidence-floor verifier gate.

        MT5 positions don't carry a confidence field, so we keep a sidecar map in
        state/position_confidence.json. On each fill we add the new ticket, then
        prune to currently-open tickets so closed positions can't inflate the floor.
        """
        if ticket is None or confidence is None:
            return
        try:
            cf = float(confidence)
        except (TypeError, ValueError):
            return
        cmap = read_json_state("position_confidence.json", default={}) or {}
        cmap[str(ticket)] = cf
        write_json_state("position_confidence.json", cmap)

    def _prune_open_confidence(self, open_positions: list[dict[str, Any]]) -> None:
        """Drop closed-position tickets from the confidence sidecar."""
        cmap = read_json_state("position_confidence.json", default={}) or {}
        if not cmap:
            return
        open_tickets = {str(p.get("ticket")) for p in open_positions if p.get("ticket") is not None}
        pruned = {t: v for t, v in cmap.items() if t in open_tickets}
        if len(pruned) != len(cmap):
            write_json_state("position_confidence.json", pruned)

    # --- Regime-flip trade replacement helpers (USER-AUTHORIZED 2026-06-30) ---

    def _detect_regime_flip(self, symbol: str, flip_cfg: dict[str, Any]) -> dict | None:
        """Return the latest flip record for `symbol` if it is fresh enough to
        act on, else None. Fresh = flip.at within regime_flip_window_sec.
        """
        rh = read_json_state("regime_history.json", default={}) or {}
        flip = (rh.get("flips") or {}).get(symbol)
        if not flip or not flip.get("at"):
            return None
        at = flip["at"]
        try:
            t = datetime.fromisoformat(at.replace("Z", "+00:00")) if at.endswith("Z") \
                else datetime.fromisoformat(at)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - t).total_seconds()
        except (ValueError, TypeError):
            return None
        if age < 0:
            return None
        window = float(flip_cfg.get("regime_flip_window_sec", 90))
        return flip if age <= window else None

    @staticmethod
    def _find_losing_opposite(
        open_positions: list[dict[str, Any]], symbol: str, side: str
    ) -> dict[str, Any] | None:
        """First OPEN position on `symbol` with the OPPOSITE side to `side`
        that is currently LOSING (unrealized profit < 0)."""
        opp = "SELL" if side == "BUY" else "BUY"
        for p in open_positions:
            if (
                p.get("symbol") == symbol
                and p.get("side") == opp
                and float(p.get("profit", 0) or 0) < 0.0
            ):
                return p
        return None

    def close_position(
        self, ticket: int, symbol: str, side: str, volume: float,
        reason: str = "regime_flip_replace",
    ) -> dict[str, Any]:
        """Close an open MT5 position via an opposite market deal."""
        if blue_guardian_enabled(self.config):
            pos_stub = {"ticket": ticket, "opened_at": None}
            ok, hold_reason = can_close_position(self.config, pos_stub, reason=reason)
            if not ok:
                return {"success": False, "error": hold_reason or "min_hold_active"}
        broker_sym = broker_symbol(symbol)
        if not mt5.symbol_select(broker_sym, True):
            return {"success": False, "error": f"symbol_select failed: {mt5.last_error()}"}
        info = mt5.symbol_info(broker_sym)
        tick = mt5.symbol_info_tick(broker_sym)
        if info is None or tick is None:
            return {"success": False, "error": f"no symbol info: {mt5.last_error()}"}
        if side == "BUY":
            order_type = mt5.ORDER_TYPE_SELL
            price = float(tick.bid)
        else:
            order_type = mt5.ORDER_TYPE_BUY
            price = float(tick.ask)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": broker_sym,
            "volume": float(volume),
            "type": order_type,
            "position": int(ticket),
            "price": price,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": f"qagent_{reason[:20]}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(info),
        }
        self.logger.info("Closing position #%s %s %s vol=%s",
                         ticket, symbol, side, volume)
        result = mt5.order_send(request)
        if result is None:
            return {"success": False, "error": str(mt5.last_error())}
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return {"success": False,
                    "error": f"retcode={result.retcode} {result.comment}",
                    "retcode": result.retcode}
        return {"success": True, "ticket": result.order, "deal": result.deal,
                "price": result.price}

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
        submitted = {"filled", "pending", "placed"}
        return {o["signal_id"] for o in orders if o.get("status") in submitted and o.get("signal_id")}

    def _build_order_record(
        self,
        signal: dict[str, Any],
        result: dict[str, Any],
        *,
        features_at_entry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        order_type = str(result.get("order_kind") or signal.get("entry_mode") or "market")
        is_pending = bool(result.get("pending"))
        status = "pending" if result.get("success") and is_pending else ("filled" if result.get("success") else "failed")
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
            "setup_type": signal.get("setup_type"),
            "reason": signal.get("reason"),
            "signal_meta": snapshot_signal_meta(
                signal,
                features_at_entry=features_at_entry,
                kelly=_json_safe_kelly(signal.get("kelly")),
            ),
            "status": status,
            "mt5_ticket": result.get("ticket"),
            "mt5_deal": result.get("deal"),
            "fill_price": None if is_pending else result.get("price"),
            "volume": result.get("volume"),
            "error": result.get("error"),
            "requested_price": result.get("requested_price"),
            "retcode": result.get("retcode"),
            "created_at": utc_now_iso(),
            "filled_at": utc_now_iso() if result.get("success") and not is_pending else None,
        }
