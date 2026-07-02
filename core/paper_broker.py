"""Paper trading broker — simulated fills only, never live MT5 orders."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from core.exposure import calc_risk_based_size, cap_size_to_exposure_limits
from core.dynamic_entry import evaluate_dynamic_entry, symbol_capacity_available
from core.entry_narrative import build_exit_narrative
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
        bar_highlow: dict[str, tuple[float, float]] | None = None,
    ) -> dict[str, Any]:
        """Simulate fills + manage exits.

        ``bar_highlow`` (optional) maps symbol -> (high, low) over the step since
        the last call. When supplied, _check_exits uses intrabar high/low for
        stop/TP and peak/trigger evaluation — the realistic model that captures
        intrabar stop-runs and trailing whipsaw. When None, it falls back to the
        legacy close-to-close model (used by callers without intrabar context).
        """
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
            feat = {"price": prices.get(symbol, signal.get("entry")), "atr": float(signal.get("entry") or 1) * 0.001}
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
        closed_positions, new_trades, realized = self._check_exits(positions, prices, bar_highlow)
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
            "entry_narrative": signal.get("entry_narrative"),
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
            # ATR used by break-even / trailing. The decision engine sets SL at
            # ~1.5xATR, so |entry-sl|/1.5 is a faithful on-position ATR proxy
            # when the feature ATR isn't carried on the order.
            "atr": abs(fill_price - order["sl"]) / 1.5 if order["sl"] else 0.0,
            # Dynamic-exit state (persisted across bars while the position is open)
            "be_triggered": False,
            "trail_active": False,
            "peak": fill_price,  # most favourable price reached (high for BUY, low for SELL)
            # Set on fill, cleared after the first _check_exits pass. While True,
            # _check_exits uses close-to-close for this position because the entry
            # bar's intrabar high/low are FUTURE relative to a close-time fill
            # (look-ahead); a position opened at the close cannot be stopped/TP'd
            # on that bar's own range.
            "just_opened": True,
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

    def _be_cfg(self, symbol: str) -> dict[str, Any]:
        from core.position_manager import _default_broker_point, _points_to_price

        be = self.config.get("trading", {}).get("break_even", {}) or {}
        psym = (be.get("per_symbol") or {}).get(symbol, {}) or {}
        point = _default_broker_point(symbol)
        trigger_pts = _points_to_price(psym.get("trigger_points", be.get("trigger_points")), point)
        lock_pts = _points_to_price(psym.get("lock_profit_points", be.get("lock_profit_points")), point)
        return {
            "enabled": bool(be.get("enabled", False)),
            "trigger_dist": lambda atr, _tp=trigger_pts, _ps=psym, _be=be: (
                _tp if _tp is not None
                else float(_ps.get("trigger_atr_mult", _be.get("trigger_atr_mult", 0.5))) * atr
            ),
            "lock_dist": lambda atr, _lp=lock_pts, _ps=psym, _be=be: (
                _lp if _lp is not None
                else float(_ps.get("lock_profit_atr_mult", _be.get("lock_profit_atr_mult", 0.1))) * atr
            ),
        }

    def _trail_cfg(self, symbol: str) -> dict[str, Any]:
        from core.position_manager import _default_broker_point, _trail_distance_price

        tr = self.config.get("trading", {}).get("trailing", {}) or {}
        psym = (tr.get("per_symbol") or {}).get(symbol, {}) or {}
        point = _default_broker_point(symbol)
        act_pts = None
        if point:
            from core.position_manager import _points_to_price
            act_pts = _points_to_price(psym.get("activation_points", tr.get("activation_points")), point)
        return {
            "enabled": bool(tr.get("enabled", False)),
            "activation_dist": lambda atr, _ap=act_pts, _ps=psym, _tr=tr: (
                _ap if _ap is not None
                else float(_ps.get("activation_atr_mult", _tr.get("activation_atr_mult", 0.75))) * atr
            ),
            "trail_dist": lambda atr, _ps=psym, _tr=tr, _pt=point: _trail_distance_price(
                _ps, _tr, atr, _pt
            ),
        }

    def _check_exits(
        self,
        positions: list[dict],
        prices: dict[str, float],
        bar_highlow: dict[str, tuple[float, float]] | None = None,
    ) -> tuple[list, list, float]:
        """Apply break-even + trailing (per config), then SL/TP1 exit.

        Two exit models:

        * **Intrabar** (``bar_highlow`` supplied): peak, BE/trail triggers, and the
          SL/TP breach are evaluated against the step's high/low. This captures
          intrabar stop-runs and trailing-stop whipsaw. Because the step's
          high/low ORDER is unknown, the exit check is resolved **conservatively
          (stop-first)**: if the adverse extreme touched the SL as it entered the
          step, the stop is assumed to have hit first (this step's favourable
          trigger is ignored) — the honest lower bound. This step's BE/trail move
          is only credited if the adverse extreme stayed above the pre-step SL.

        * **Close-to-close** (``bar_highlow`` None, legacy): everything evaluated
          against the single per-bar close. Kept for callers without intrabar
          context. NOTE: this model overstates dynamic-exit performance (see
          memory exit-model-bias-found-2026-06-27) — prefer the intrabar path.

        Narratives (be/trail/exit) are stamped from the real moved levels.
        """
        closed = []
        trades = []
        realized = 0.0

        for pos in positions:
            symbol = pos["symbol"]
            price = prices.get(symbol, pos["entry"])
            side = pos["side"]
            entry = float(pos["entry"])
            _sl = pos.get("sl")
            atr = float(pos.get("atr") or 0.0) or (
                abs(entry - float(_sl)) / 1.5 if _sl is not None else 0.0
            )
            direction = 1.0 if side == "BUY" else -1.0

            # Capture SL + dynamic-exit state AS THEY ENTERED THIS STEP, before
            # any BE/trail move this step. Used for the conservative stop-first
            # intrabar check: if the adverse extreme touched the pre-step SL, we
            # assume the stop hit first (full loss / prior locked level) and ignore
            # this step's favourable trigger. Only if the adverse extreme stayed
            # above the pre-step SL do we let this step's trigger lock a win.
            sl_start = float(pos["sl"])
            be_start = bool(pos.get("be_triggered"))
            trail_start = bool(pos.get("trail_active"))

            hl = bar_highlow.get(symbol) if bar_highlow else None
            if pos.get("just_opened"):
                # Position was opened THIS step at the close. Its entry bar's
                # intrabar high/low are future relative to the fill (look-ahead),
                # so use close-to-close for this one step -- a position opened at
                # the close cannot be stopped/TP'd on that bar's own range.
                fav_price = price
                adv_price = price
                pos["just_opened"] = False  # one-shot; next step uses intrabar
            elif hl is not None:
                hi, lo = float(hl[0]), float(hl[1])
                fav_price = hi if side == "BUY" else lo
                adv_price = lo if side == "BUY" else hi
            else:
                fav_price = price
                adv_price = price
            profit = (fav_price - entry) * direction  # favourable excursion > 0

            # Conservative stop-first test: if the step's adverse extreme touched
            # the pre-step SL, the stop is assumed to have hit first -- we exit at
            # sl_start and do NOT credit this step's BE/trail move (honest lower
            # bound; matches the docstring). Only if the adverse extreme stayed
            # beyond the pre-step SL do we let this step lock a win. Skipping the
            # mutation here also keeps the recorded sl/be/trail flags on a
            # stop-first-exit trade consistent with the pre-step exit state.
            if side == "BUY":
                stop_first = adv_price <= sl_start
            else:
                stop_first = adv_price >= sl_start

            if not stop_first:
                # Track most favourable price reached (for trailing).
                if side == "BUY":
                    pos["peak"] = max(float(pos.get("peak", entry)), fav_price)
                else:
                    pos["peak"] = min(float(pos.get("peak", entry)), fav_price)

                # --- Break-even: move SL to entry + lock_profit_atr once +trigger. ---
                be = self._be_cfg(symbol)
                trigger_dist = float(be["trigger_dist"](atr))
                lock_dist = float(be["lock_dist"](atr))
                if be["enabled"] and not pos.get("be_triggered") and atr > 0 and profit >= trigger_dist:
                    lock_sl = entry + direction * lock_dist
                    pos["sl"] = lock_sl
                    pos["be_triggered"] = True
                    pos["be_narrative"] = (
                        f"Break-even: +{profit:.5f} favourable triggered SL move to "
                        f"{lock_sl:.5f} (locked {lock_dist:.5f} over entry)."
                    )

                # --- Trailing: once +activation, trail SL to peak - trail_atr (only favourable direction). ---
                tr = self._trail_cfg(symbol)
                if tr["enabled"] and atr > 0 and profit >= float(tr["activation_dist"](atr)):
                    trail_dist = float(tr["trail_dist"](atr))
                    new_sl = float(pos["peak"]) - direction * trail_dist
                    improved = (side == "BUY" and new_sl > float(pos["sl"])) or (side == "SELL" and new_sl < float(pos["sl"]))
                    if improved:
                        pos["sl"] = new_sl
                        pos["trail_active"] = True
                        pos["trail_narrative"] = (
                            f"Trailing: SL trailed to {new_sl:.5f} "
                            f"({trail_dist:.5f} behind peak {pos['peak']:.5f})."
                        )

            # --- Exit against SL / TP1 (conservative stop-first intrabar). ---
            # When bar_highlow is supplied the adverse/favourable extremes of the
            # step are known but their ORDER is not. We resolve the ambiguity
            # pessimistically (the honest lower bound): if the adverse extreme
            # touched the SL as it entered the step, assume the stop hit first and
            # exit at that pre-step level — this step's favourable trigger is
            # ignored. Only if the adverse extreme stayed above the pre-step SL do
            # we credit this step's BE/trail move and exit at the raised level.
            # Close-to-close (no bar_highlow) keeps adv==fav==close, so sl_start ==
            # sl_now and this collapses to the legacy behaviour.
            exit_reason = None
            exit_price = price
            sl_now = float(pos["sl"])
            tp1 = float(pos["tp1"])
            if side == "BUY":
                if adv_price <= sl_start:
                    # stop-first at the pre-step SL (original / prior BE / prior trail)
                    exit_reason = ("trailing_stop" if trail_start else ("break_even_stop" if be_start else "stop_loss"))
                    exit_price = sl_start
                elif adv_price <= sl_now and (pos.get("be_triggered") or pos.get("trail_active")):
                    # adverse hit this step's newly-raised SL but not the pre-step SL
                    exit_reason = "trailing_stop" if pos.get("trail_active") else "break_even_stop"
                    exit_price = sl_now
                elif fav_price >= tp1 and not pos.get("be_triggered") and not pos.get("trail_active"):
                    exit_reason = "take_profit"
                    exit_price = tp1
            else:
                if adv_price >= sl_start:
                    exit_reason = ("trailing_stop" if trail_start else ("break_even_stop" if be_start else "stop_loss"))
                    exit_price = sl_start
                elif adv_price >= sl_now and (pos.get("be_triggered") or pos.get("trail_active")):
                    exit_reason = "trailing_stop" if pos.get("trail_active") else "break_even_stop"
                    exit_price = sl_now
                elif fav_price <= tp1 and not pos.get("be_triggered") and not pos.get("trail_active"):
                    exit_reason = "take_profit"
                    exit_price = tp1

            if exit_reason:
                diff = exit_price - entry
                if side == "SELL":
                    diff = -diff
                pnl = diff * pos["size"]
                realized += pnl
                meta = pos.get("signal_meta") or {}
                exit_narrative = build_exit_narrative(side, entry, sl_now, tp1, exit_price, exit_reason, pnl)
                closed.append({**pos, "closed_at": utc_now_iso(),
                               "exit_price": exit_price, "exit_reason": exit_reason, "pnl": round(pnl, 2)})
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
                        "entry_narrative": meta.get("entry_narrative"),
                        "be_narrative": pos.get("be_narrative"),
                        "trail_narrative": pos.get("trail_narrative"),
                        "exit_narrative": exit_narrative,
                        "be_triggered": bool(pos.get("be_triggered")),
                        "trail_active": bool(pos.get("trail_active")),
                        "closed_at": utc_now_iso(),
                    }
                )
        return closed, trades, realized