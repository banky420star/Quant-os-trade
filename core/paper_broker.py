"""Paper trading broker — simulated fills only, never live MT5 orders."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from core.exposure import calc_risk_based_size, cap_size_to_exposure_limits
from core.kelly_sizing import resolve_risk_percent
from core.dynamic_entry import evaluate_dynamic_entry, symbol_capacity_available
from core.entry_narrative import build_exit_narrative
from core.learning_logger import log_decision
from core.learning_schema import build_decision_event, config_snapshot_hash
from core.trade_limits import is_duplicate_position, session_trade_capacity_available
from core.utils import utc_now_iso


class PaperBroker:
    """Simulate order fills and track paper portfolio."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("paper_broker")
        if config["execution"].get("mode") == "mt5":
            raise RuntimeError("PaperBroker cannot run when execution.mode is mt5 — use MT5Broker")

    def _log_decision(self, signal: dict[str, Any], decision: str, reason: str,
                        features_at_entry: dict[str, Any] | None = None) -> None:
        """Log one decision event to logs/decisions.jsonl."""
        try:
            feat = features_at_entry or {}
            log_decision(build_decision_event(
                symbol=signal.get("symbol"),
                side=signal.get("side"),
                timeframe="M5",
                mode=self.config.get("execution", {}).get("mode", "paper"),
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

        default_risk = float(self.config["signals"].get("default_risk_percent", 1))
        executed_signal_ids = {p.get("signal_id") for p in positions if p.get("signal_id")}

        for record in approved:
            signal = record.get("signal", record)
            if not symbol_capacity_available(self.config, signal["symbol"], positions):
                self.logger.info("Paper skip %s — max open per symbol", signal["symbol"])
                self._log_decision(signal, "skip", "symbol_capacity_exceeded")
                continue
            if not session_trade_capacity_available(self.config, signal["symbol"], trades):
                self.logger.info("Paper skip %s — max session trades per symbol", signal["symbol"])
                self._log_decision(signal, "skip", "session_trade_capacity_exceeded")
                continue
            if is_duplicate_position(
                self.config,
                signal,
                positions,
                executed_signal_ids=executed_signal_ids,
            ):
                self._log_decision(signal, "skip", "duplicate_position")
                continue

            symbol = signal["symbol"]
            feat = {"price": prices.get(symbol, signal.get("entry")), "atr": float(signal.get("entry") or 1) * 0.001}
            dyn_ok, signal, dyn_reason = evaluate_dynamic_entry(self.config, signal, positions, feat)
            if not dyn_ok:
                self.logger.info("Paper skip %s — %s", symbol, dyn_reason)
                self._log_decision(signal, "skip", f"dynamic_entry: {dyn_reason}")
                orders.append(self._create_rejected_order(signal, feat["price"], dyn_reason or "dynamic_entry"))
                continue

            price = prices.get(symbol, signal.get("entry"))
            if not price:
                self.logger.warning("No price for %s — skipping", symbol)
                self._log_decision(signal, "skip", "no_price_data")
                continue

            equity = float(balance.get("equity", balance.get("cash", starting_cash)))
            risk_pct, kelly = resolve_risk_percent(signal, self.config, default_risk)
            signal["kelly"] = kelly
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
                self._log_decision(signal, "skip", "exposure_limit_exceeded")
                orders.append(self._create_rejected_order(signal, price, "exposure_limit_exceeded"))
                continue

            from core.utils import read_json_state
            sym_features = read_json_state("features.json", default={}).get("symbols", {}).get(symbol, {})
            order = self._create_order(signal, price, features_at_entry=sym_features)
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
                self._log_decision(signal, "execute", "filled", features_at_entry=sym_features)
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
    def _signal_meta(signal: dict[str, Any], features_at_entry: dict[str, Any] | None = None) -> dict[str, Any]:
        from core.trade_journal import snapshot_signal_meta
        return snapshot_signal_meta(signal, features_at_entry=features_at_entry)

    def _create_order(
        self,
        signal: dict[str, Any],
        price: float,
        *,
        features_at_entry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        order_type = signal.get("order_type", "market")
        return {
            "order_id": str(uuid.uuid4()),
            "signal_id": signal.get("signal_id", signal.get("id", "unknown")),
            "symbol": signal.get("symbol", "unknown"),
            "side": signal.get("side", "unknown"),
            "type": order_type,
            "entry": signal.get("entry", 0),
            "sl": signal.get("sl", 0),
            "tp1": signal.get("tp1", 0),
            "tp2": signal.get("tp2"),
            "price": price,
            "status": "pending",
            "created_at": utc_now_iso(),
            "setup_type": signal.get("setup_type"),
            "reason": signal.get("reason"),
            "signal_meta": self._signal_meta(signal, features_at_entry=features_at_entry),
        }

    def _create_rejected_order(self, signal: dict[str, Any], price: float, reason: str) -> dict[str, Any]:
        return {
            "order_id": str(uuid.uuid4()),
            "signal_id": signal.get("signal_id", signal.get("id", "unknown")),
            "symbol": signal.get("symbol", "unknown"),
            "side": signal.get("side", "unknown"),
            "type": "market",
            "entry": signal.get("entry", 0),
            "sl": signal.get("sl", 0),
            "tp1": signal.get("tp1", 0),
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
        be = self.config.get("trading", {}).get("break_even", {}) or {}
        psym = (be.get("per_symbol") or {}).get(symbol, {}) or {}
        return {"enabled": bool(be.get("enabled", False)), "parent": be, "sym": psym}

    def _trail_cfg(self, symbol: str) -> dict[str, Any]:
        tr = self.config.get("trading", {}).get("trailing", {}) or {}
        psym = (tr.get("per_symbol") or {}).get(symbol, {}) or {}
        return {"enabled": bool(tr.get("enabled", False)), "parent": tr, "sym": psym}

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

                from core.exit_manager import (
                    partial_close_volume,
                    partial_tp_enabled,
                    post_partial_sl,
                    profit_rr,
                    tp1_reached,
                    tp2_reached,
                    trail_activation_allowed,
                    trail_distance_multiplier,
                )
                from core.position_manager import (
                    _default_broker_point,
                    _exit_trigger_met,
                    _points_to_price,
                    _trail_distance_price,
                )

                point = _default_broker_point(symbol)
                profit_usd = pos.get("profit")
                try:
                    profit_usd = float(profit_usd) if profit_usd is not None else None
                except (TypeError, ValueError):
                    profit_usd = None

                # --- Break-even: USD OR broker points OR ATR mult (latched). ---
                be = self._be_cfg(symbol)
                be_sym, be_parent = be["sym"], be["parent"]
                if be["enabled"] and atr > 0 and (
                    pos.get("be_triggered")
                    or _exit_trigger_met(
                        profit_usd,
                        profit,
                        be_sym,
                        be_parent,
                        usd_key="trigger_profit_usd",
                        points_key="trigger_points",
                        atr_mult_key="trigger_atr_mult",
                        atr=atr,
                        point=point,
                    )
                ):
                    lock_pts = _points_to_price(
                        be_sym.get("lock_profit_points", be_parent.get("lock_profit_points")), point,
                    )
                    usd_trig = be_sym.get("trigger_profit_usd", be_parent.get("trigger_profit_usd"))
                    usd_lock = be_sym.get("lock_profit_usd", be_parent.get("lock_profit_usd"))
                    usd_triggered = (
                        profit_usd is not None
                        and usd_trig is not None
                        and profit_usd >= float(usd_trig)
                    )
                    if lock_pts is not None:
                        lock_dist = lock_pts
                    elif usd_triggered and usd_lock is not None:
                        lock_dist = max(0.0, float(usd_lock))
                    else:
                        lock_dist = atr * float(
                            be_sym.get("lock_profit_atr_mult", be_parent.get("lock_profit_atr_mult", 0.1))
                        )
                    lock_sl = entry + direction * lock_dist
                    if (side == "BUY" and lock_sl > float(pos["sl"])) or (side == "SELL" and lock_sl < float(pos["sl"])):
                        pos["sl"] = lock_sl
                        pos["be_triggered"] = True
                        pos["be_narrative"] = (
                            f"Break-even: +{profit:.5f} favourable triggered SL move to "
                            f"{lock_sl:.5f} (locked {lock_dist:.5f} over entry)."
                        )

                if pos.get("initial_sl") is None:
                    pos["initial_sl"] = sl_start

                # --- Partial TP1: bank half, arm runner to TP2. ---
                if partial_tp_enabled(self.config) and not pos.get("partial_tp_done"):
                    tp1_level = float(pos.get("tp1", 0))
                    if tp1_reached(side, fav_price, tp1_level):
                        pcfg = self.config.get("trading", {}).get("exits", {}).get("partial_tp", {})
                        close_vol = partial_close_volume(
                            float(pos.get("size", 0.01)),
                            float(pcfg.get("fraction", 0.5)),
                            min_remain=float(pcfg.get("min_volume_remain", 0.01)),
                        )
                        if close_vol > 0:
                            risk_sl = float(pos.get("initial_sl") or sl_start)
                            lock_sl = post_partial_sl(side, entry, risk_sl, atr, self.config)
                            diff = (tp1_level - entry) if side == "BUY" else (entry - tp1_level)
                            partial_pnl = diff * close_vol
                            trades.append({
                                "trade_id": str(uuid.uuid4()),
                                "position_id": pos["position_id"],
                                "signal_id": pos.get("signal_id"),
                                "symbol": symbol,
                                "side": side,
                                "entry": entry,
                                "exit": tp1_level,
                                "sl": pos.get("sl"),
                                "tp1": tp1_level,
                                "pnl": round(partial_pnl, 2),
                                "result": "win" if partial_pnl > 0 else "loss",
                                "exit_reason": "partial_take_profit",
                                "partial": True,
                                "setup_type": pos.get("setup_type"),
                                "closed_at": utc_now_iso(),
                            })
                            realized += partial_pnl
                            pos["size"] = round(float(pos["size"]) - close_vol, 2)
                            pos["sl"] = lock_sl
                            pos["be_triggered"] = True
                            pos["partial_tp_done"] = True
                            runner_tp = float(pos.get("tp2") or tp1_level)
                            pos["tp1"] = runner_tp
                            # Runner managed on the next step — same-bar low can sit
                            # below the new lock SL even on a winning TP1 bar.
                            continue

                # --- Trailing: deferred until partial TP or min R. ---
                tr = self._trail_cfg(symbol)
                tr_sym, tr_parent = tr["sym"], tr["parent"]
                mgmt_row = {
                    "trailing": bool(pos.get("trail_active")),
                    "partial_tp_done": bool(pos.get("partial_tp_done")),
                }
                rr = profit_rr(side, entry, float(pos.get("initial_sl") or sl_start), fav_price)
                if tr["enabled"] and atr > 0 and trail_activation_allowed(
                    mgmt_row,
                    profit_rr_value=rr,
                    profit_usd=profit_usd,
                    profit_dist=profit,
                    trail_sym=tr_sym,
                    trail_cfg=tr_parent,
                    atr=atr,
                    point=point,
                    config=self.config,
                    exit_trigger_met=_exit_trigger_met,
                ):
                    trail_dist = float(_trail_distance_price(tr_sym, tr_parent, atr, point))
                    trail_dist *= trail_distance_multiplier(mgmt_row, self.config)
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
            tp_target = float(pos["tp1"])
            tp2 = float(pos.get("tp2") or 0)
            if side == "BUY":
                if adv_price <= sl_start:
                    # stop-first at the pre-step SL (original / prior BE / prior trail)
                    exit_reason = ("trailing_stop" if trail_start else ("break_even_stop" if be_start else "stop_loss"))
                    exit_price = sl_start
                elif adv_price <= sl_now and (pos.get("be_triggered") or pos.get("trail_active")):
                    # adverse hit this step's newly-raised SL but not the pre-step SL
                    exit_reason = "trailing_stop" if pos.get("trail_active") else "break_even_stop"
                    exit_price = sl_now
                elif pos.get("partial_tp_done") and tp2 > 0 and tp2_reached(side, fav_price, tp2):
                    exit_reason = "take_profit"
                    exit_price = tp2
                elif (
                    not partial_tp_enabled(self.config)
                    and fav_price >= tp_target
                    and not pos.get("trail_active")
                ):
                    exit_reason = "take_profit"
                    exit_price = tp_target
            else:
                if adv_price >= sl_start:
                    exit_reason = ("trailing_stop" if trail_start else ("break_even_stop" if be_start else "stop_loss"))
                    exit_price = sl_start
                elif adv_price >= sl_now and (pos.get("be_triggered") or pos.get("trail_active")):
                    exit_reason = "trailing_stop" if pos.get("trail_active") else "break_even_stop"
                    exit_price = sl_now
                elif pos.get("partial_tp_done") and tp2 > 0 and tp2_reached(side, fav_price, tp2):
                    exit_reason = "take_profit"
                    exit_price = tp2
                elif (
                    not partial_tp_enabled(self.config)
                    and fav_price <= tp_target
                    and not pos.get("trail_active")
                ):
                    exit_reason = "take_profit"
                    exit_price = tp_target

            if exit_reason:
                diff = exit_price - entry
                if side == "SELL":
                    diff = -diff
                pnl = diff * pos["size"]
                realized += pnl
                meta = pos.get("signal_meta") or {}
                exit_narrative = build_exit_narrative(side, entry, sl_now, tp_target, exit_price, exit_reason, pnl)
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
                        "tp1": tp_target,
                        "tp2": pos.get("tp2"),
                        "partial_tp_done": bool(pos.get("partial_tp_done")),
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