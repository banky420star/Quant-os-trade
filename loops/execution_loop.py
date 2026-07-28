"""Agent 5: Execution Loop — paper simulation OR real MT5 demo orders."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.learning_logger import log_decision
from core.learning_schema import build_decision_event, config_snapshot_hash
from core.market_hours import clear_backoff, in_backoff, is_market_closed_error, record_market_closed
from core.mt5_client import MT5Client, format_mt5_connection_error, log_session_alignment
from core.mt5_connection_manager import MT5ConnectionManager
from core.mt5_broker import MT5Broker
from core.paper_broker import PaperBroker


def _close_bankbot_opposite_positions(
    approved: list[dict],
    positions: list[dict],
    config: dict,
    logger: logging.Logger,
) -> tuple[list[dict], list[dict]]:
    """Bankbot reversal-aware close (pyramiding=0 semantics).

    Bankbot is a reversal strategy — when a bankbot BUY signal fires, any
    existing bankbot SELL position should be closed before opening the BUY.
    Since the bot mixes bankbot + decision engine signals, we close ALL
    existing positions for symbols that have incoming bankbot signals so
    the new bankbot position opens clean (matching the Pine Script behavior).

    Includes a minimum-hold gate (default 60s) so freshly opened positions
    are NOT immediately reversed by the next cycle's bankbot signal.

    Modifies ``positions`` in-place for paper mode (removes closed ones)
    and returns (modified_approved, closed).  For MT5 mode the caller
    handles the actual close via MT5Broker.close_position().
    """
    from core.blue_guardian import position_age_seconds

    closed: list[dict] = []
    bb_symbols: set[str] = set()
    bb_sides: dict[str, str] = {}
    _min_hold = int(
        (config.get("trading") or {})
        .get("signal_reversal_early_close", {})
        .get("min_hold_seconds", 60)
    )
    for row in approved:
        sig = row.get("signal", row) if isinstance(row, dict) else {}
        if isinstance(sig, dict) and sig.get("source") == "bankbot":
            sym = sig.get("symbol", "")
            side = sig.get("side", "")
            if sym and side:
                bb_symbols.add(sym)
                bb_sides[sym] = side

    if not bb_symbols:
        return approved, closed

    kept_positions: list[dict] = []
    for pos in positions:
        sym = pos.get("symbol", "")
        side = pos.get("side", "")
        if sym in bb_symbols and side and bb_sides.get(sym) and bb_sides[sym] != side:
            # Check minimum hold: skip if position was just opened
            _age = position_age_seconds(pos, {})
            if _age < _min_hold:
                logger.info(
                    "BANKBOT REVERSAL SKIP (age gate): %s %s ticket=%s age=%.0fs < %ds",
                    sym, side, pos.get("ticket"), _age, _min_hold,
                )
                kept_positions.append(pos)
                continue
            # Opposite-side position found — close it
            closed.append(pos)
            logger.info(
                "BANKBOT REVERSAL: closing %s %s ticket=%s before opening new %s %s",
                sym, side, pos.get("ticket"), sym, bb_sides.get(sym),
            )
        else:
            kept_positions.append(pos)

    if closed:
        positions.clear()
        positions.extend(kept_positions)
        logger.info("Bankbot reversal closed %d opposite-side positions", len(closed))

    return approved, closed
from core.trade_tracker import TradeTracker
from core.state_store import (
    approved_available,
    read_approved_signals,
    sync_store_from_doc,
)
from core.daily_pnl import today_realized_pnl_usd, today_utc_day_stamp
from core.utils import (
    fail_safe_missing,
    load_config,
    read_json_state,
    setup_logger,
    utc_now_iso,
    write_json_state,
)


def _price_fallback_from_features(features: dict) -> dict[str, float]:
    prices: dict[str, float] = {}
    for symbol, feat in features.get("symbols", {}).items():
        price = feat.get("price")
        if price:
            prices[symbol] = float(price)
    return prices


def _collect_prices(config: dict, logger) -> tuple[dict[str, float], str]:
    symbols = list(config["mt5"]["symbols"])
    paper_mode = config.get("execution", {}).get("mode") == "paper"
    prices: dict[str, float] = {}
    source = "mt5"

    client = MT5Client(config, logger)
    try:
        client.connect()
        for symbol in symbols:
            tick = client.get_current_price(symbol)
            if tick:
                prices[symbol] = tick["mid"]
        if not prices:
            raise ConnectionError("MT5 connected but returned no tick prices")
    except (ConnectionError, OSError, RuntimeError) as exc:
        session_info = log_session_alignment(logger)
        err_msg = format_mt5_connection_error(exc, session_info) if isinstance(exc, ConnectionError) else str(exc)
        if paper_mode:
            features = read_json_state("features.json", default={})
            prices = _price_fallback_from_features(features)
            source = "paper_fallback"
            logger.warning("MT5 price fetch failed (%s) — using features.json", err_msg)
        else:
            raise
    finally:
        client.disconnect()

    return prices, source


def _drain_intents_phase(config: dict, mode: str, logger) -> dict | None:
    """Drain any pending intents BEFORE the slow approval-driven path.

    2026-07-22 architectural change: ``MT5TerminalManager.intent_queue``
    is the chokepoint every decoupled producer (fast_tick_loop live
    entries, fast_position_guard SL ratchet intents, future web/api
    POST /trade) writes into. If intents are queued when execution_loop
    fires, process them FIRST so 1Hz fast-tick entries beat a 30s slow
    cycle to the punch (would otherwise race — e.g. fast_tick opens at
    T+50ms and slow cycle CLOSES that ticket at T+200ms with stale
    duplicate-position logic).

    The MT5 ``_shared_trade_lock`` inside MT5Broker guarantees the
    actual MT5 mutation is single-writer. This function just sequences
    intent processing BEFORE the slow-path so the slow path's
    duplicate-position check sees fresh state.

    Returns a result dict if any intents were drained (caller can
    skip the slow path downstream), else None.
    """
    try:
        from core.mt5_terminal_manager import MT5TerminalManager
    except ImportError:  # pragma: no cover — circular-import safe
        return None
    try:
        mgr = MT5TerminalManager(config, logger)
        intents = mgr.drain_intents(max_items=50)
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("_drain_intents_phase: drain failed (%s)", exc)
        return None
    if not intents:
        return None
    opens = [it for it in intents if it.get("action") == "open"]
    closes = [it for it in intents if it.get("action") == "close"]
    logger.info(
        "Intent drain: %d items (opens=%d closes=%d)",
        len(intents), len(opens), len(closes),
    )
    if mode != "mt5":
        # Paper mode: log intent drain but let paper_broker fall through.
        logger.info("Paper mode: dequeuing %d intents (no broker order_send)",
                    len(intents))
        return {
            "timestamp": utc_now_iso(),
            "mode": mode,
            "mode_effective": "paper",
            "broker_invoked": False,
            "drained": len(intents),
            "placed": 0,
        }
    if not opens and not closes:
        return {
            "timestamp": utc_now_iso(),
            "mode": mode,
            "mode_effective": "mt5",
            "broker_invoked": False,
            "drained": len(intents),
            "placed": 0,
        }
    try:
        broker = MT5Broker(config, logger)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("_drain_intents_phase: no broker (%s)", exc)
        return None
    placed_count = 0
    try:
        if opens:
            approved = [{"signal": it} for it in opens]
            res = broker.process_approved_signals(approved)
            placed_count += len(res.get("placed") or [])
        for cl in closes:
            cr = broker.close_position(
                int(cl.get("ticket", 0) or 0),
                cl.get("symbol", ""),
                cl.get("side", ""),
                float(cl.get("volume") or cl.get("size") or 0.01),
                reason=str(cl.get("reason") or "intent_drain"),
            )
            if cr.get("success"):
                placed_count += 1
            else:
                logger.warning(
                    "Intent drain close failed ticket=%s: %s",
                    cl.get("ticket"), cr.get("error"),
                )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("_drain_intents_phase: broker call failed (%s)", exc)
    return {
        "timestamp": utc_now_iso(),
        "mode": mode,
        "mode_effective": "mt5",
        "broker_invoked": True,
        "drained": len(intents),
        "placed": placed_count,
    }


def _check_execution_allowed(config: dict, logger) -> bool:
    # 2026-07-28 hotfix: when kill-switches are force-disabled, bypass the
    # entire gate stack (kill_switch.json, risk_state.kill_switch, daily
    # profit halt). This is a load-bearing fail-open for live trading.
    force_off = bool(
        (config or {}).get("practice", {}).get("force_disable_all_kill_triggers", False)
    )
    if force_off:
        try:
            write_json_state(
                "kill_switch.json",
                {"kill_switch": False, "reason": None, "activated_at": None},
            )
        except Exception:
            pass
        try:
            rs = read_json_state("risk_state.json", default={}) or {}
            if isinstance(rs, dict) and rs.get("kill_switch"):
                rs["kill_switch"] = False
                write_json_state("risk_state.json", rs)
        except Exception:
            pass
        logger.info(
            "FORCE_DISABLE_ALL_KILL_TRIGGERS: execution_gate bypassed (kill_switch.json + "
            "risk_state.kill_switch cleared)"
        )
        return True

    # ----- Read kill_switch.json ONCE (always, regardless of halt_usd) -----
    # Reading at module top of the function every cycle keeps the rest of the
    # gate trip-free from UnboundLocalError when the user disables halt_usd.
    # The 3 sections below all use this single read:
    #   1. day-rollover clear (writes back, then resets `existing = {}`)
    #   2. top-of-function kill_switch check (`if existing.get("kill_switch")`)
    #   3. daily-profit-halt gate (writes new halt info when kill_switch is
    #      still off and pnl >= threshold)
    existing = read_json_state("kill_switch.json", default={}) or {}
    existing_reason = str(existing.get("reason") or "")

    # ----- Day-rollover auto-clear BEFORE the top kill_switch check -----
    # If a daily_profit_halt from a previous UTC day is still in kill_switch.json
    # at the start of a new UTC day, clear it NOW so the rest of this function
    # can correctly re-evaluate trading for today. Without this guard, the
    # top-of-function `if kill.get("kill_switch"): return False` would keep the
    # bot permanently halted after the day rollover (the daily halt block at
    # the bottom of this function never executes when kill_switch=True already).
    halt_usd = float((config.get("risk") or {}).get("daily_profit_halt_usd", 0) or 0)
    stamp_today = today_utc_day_stamp()
    if halt_usd > 0:
        existing_day_stamp = existing.get("day_stamp")
        is_old_halt = (
            existing.get("kill_switch")
            and "daily_profit_halt" in existing_reason
            and existing_day_stamp
            and existing_day_stamp != stamp_today
        )
        if is_old_halt:
            write_json_state(
                "kill_switch.json",
                {
                    "kill_switch": False,
                    "cleared_at": utc_now_iso(),
                    "previous_reason": existing_reason,
                    "previous_day_stamp": existing_day_stamp,
                },
            )
            logger.info(
                "Daily profit halt cleared (new UTC day %s; was halt from %s)",
                stamp_today,
                existing_day_stamp,
            )
            existing = {}

    # ----- Top-of-function kill_switch check (manual halts, etc.) -----
    if existing.get("kill_switch"):
        logger.error("Execution blocked — kill switch is ON: %s", existing.get("reason"))
        return False

    risk = read_json_state("risk_state.json", default={})
    if risk.get("kill_switch"):
        logger.error("Execution blocked — risk_state kill switch active")
        return False
    # live_trading_enabled is the hard guard for the MT5 (real-order) path. When
    # false, MT5 execution is refused outright even on a demo account -- the flag
    # means what its name says, and the dashboard's "blocked" status must be true.
    # Paper mode is unaffected (paper is simulated, fine for research).
    exec_cfg = config.get("execution", {})
    mode = exec_cfg.get("mode", "paper")
    if mode == "mt5" and not exec_cfg.get("live_trading_enabled", False):
        logger.error(
            "Execution blocked — live_trading_enabled is false (paper/research mode "
            "only). Set execution.live_trading_enabled: true to allow MT5 orders."
        )
        return False

    # ----- Daily-profit-halt gate -----
    # Only fires on a CLEAN cycle (no kill_switch already set — handled at top).
    # When today's CLOSED-trade realized PnL >= risk.daily_profit_halt_usd,
    # write kill_switch.json with reason 'daily_profit_halt_usd:<N>_target_hit_pnl=<P>'
    # + today's day_stamp and return False. Float / unrealized PnL is ignored —
    # only closed-trade pnl counts (core/daily_pnl.today_realized_pnl_usd).
    if halt_usd > 0:
        pnl_today = today_realized_pnl_usd("paper_trades.json")
        if pnl_today >= halt_usd:
            # Invariant: existing.get("kill_switch") is False at this point
            # (top check fired otherwise). So writing a fresh daily halt is
            # always safe — no risk of clobbering a manual halt's reason.
            write_json_state(
                "kill_switch.json",
                {
                    "kill_switch": True,
                    "reason": (
                        f"daily_profit_halt_usd:{halt_usd:.0f}_"
                        f"target_hit_pnl={pnl_today:.2f}"
                    ),
                    "day_stamp": stamp_today,
                    "halted_at": utc_now_iso(),
                    "pnl_today_usd": pnl_today,
                },
            )
            logger.info(
                "DAILY PROFIT HALT: pnl_today=%.2f >= target=$%.0f — kill_switch "
                "ON for the remainder of UTC day %s",
                pnl_today,
                halt_usd,
                stamp_today,
            )
            return False
    return True


def run() -> dict | None:
    """Execute approved signals — paper mode or real MT5 orders.

    2026-07-22 architectural change: this is now also a queue consumer.
    The drain phase runs BEFORE the slow approval path so any
    fast_tick_intent / fast_position_guard_intent gets processed within
    the same cycle. If we placed anything, we return early — the slow
    path's duplicate-position logic would see fresh state anyway, so
    running both in the same cycle is fine but wastes an MT5 connect.
    """
    config = load_config()
    logger = setup_logger("execution_loop", "execution_loop.log")
    mode = config["execution"].get("mode", "paper")
    logger.info("Starting execution loop (mode=%s)", mode)
    log_session_alignment(logger)

    if not _check_execution_allowed(config, logger):
        return None

    # ----- Queue drain phase (2026-07-22) -----------------------------
    # Fast-tick / fast-position-guard / future decoupled producers push
    # "open"/"close" intents onto MT5TerminalManager.intent_queue
    # between pipeline cycles. Drain them BEFORE the slow path so 1Hz
    # fast-tick entries win the race against the 30s slow cycle.
    # _shared_trade_lock inside MT5Broker guarantees single-mutator at
    # the mt5.order_send() call, so this drain is race-free.
    drained = _drain_intents_phase(config, mode, logger)
    if drained:
        logger.info("Execution loop: returning early after drain phase")
        return drained
    # ----- End drain phase -------------------------------------------

    bg = read_json_state("blue_guardian.json", default={})
    if bg.get("enabled") and bg.get("trading_paused"):
        logger.error("Execution blocked — Blue Guardian daily pause: %s", bg.get("pause_reason"))
        return None

    if not approved_available(config) and fail_safe_missing("approved_signals.json", logger):
        return None

    approved_data = read_approved_signals(config) or {}
    approved = approved_data.get("approved", [])

    # Market-closed back-off: skip symbols whose market is in a back-off window
    # so we do not spam failed orders every cycle while oil/equities are closed.
    if approved:
        kept = []
        for row in approved:
            sig = row.get("signal", row) if isinstance(row, dict) else {}
            sym = sig.get("symbol")
            if sym and in_backoff(sym, config):
                logger.info("Skipping %s order - market-closed back-off active", sym)
                continue
            kept.append(row)
        approved = kept

    if not approved:
        logger.info("No approved signals to execute")
        try:
            log_decision(build_decision_event(
                decision="idle",
                mode=mode,
                reason="no_approved_signals",
                config_hash=config_snapshot_hash(config),
            ))
        except Exception:
            pass
        return {"timestamp": None, "mode": mode, "placed": 0}

    orders_state = read_json_state("paper_orders.json", default={"orders": []})
    positions_state = read_json_state("paper_positions.json", default={"positions": []})
    trades_state = read_json_state("paper_trades.json", default={"trades": []})

    orders = orders_state.get("orders", [])
    positions = positions_state.get("positions", [])
    trades = trades_state.get("trades", [])
    balance = orders_state.get("balance")

    if mode == "mt5":
        connection = MT5ConnectionManager(config, logger)
        try:
            connection.connect()
            broker = MT5Broker(config, logger)

            # Bankbot reversal close (pyramiding=0): close opposite-side
            # positions before placing bankbot trades, mimicking the
            # original Pine Script behavior where a new signal replaces
            # the old position on the same symbol.
            approved, _bb_closed = _close_bankbot_opposite_positions(
                approved, positions, config, logger,
            )
            if _bb_closed:
                for _pos in _bb_closed:
                    try:
                        _cr = broker.close_position(
                            _pos.get("ticket"),
                            _pos.get("symbol", ""),
                            _pos.get("side", ""),
                            float(_pos.get("size") or _pos.get("volume") or 0.01),
                            reason="bankbot_reversal",
                        )
                        if _cr.get("success"):
                            logger.info(
                                "BANKBOT REVERSAL closed MT5 #%s %s %s",
                                _pos.get("ticket"), _pos.get("symbol"), _pos.get("side"),
                            )
                        else:
                            logger.warning(
                                "BANKBOT REVERSAL close FAILED: #%s %s -> %s",
                                _pos.get("ticket"), _pos.get("symbol"), _cr.get("error"),
                            )
                    except Exception as _bbexc:
                        logger.warning(
                            "BANKBOT REVERSAL close EXCEPTION: #%s %s -> %s",
                            _pos.get("ticket"), _pos.get("symbol"), _bbexc,
                        )

            result = broker.process_approved_signals(
                approved,
                orders,
                existing_trades=trades,
                existing_positions=positions,
            )
            orders_doc = {
                "timestamp": result["timestamp"],
                "mode": "mt5",
                "balance": result["balance"],
                "account": result["account"],
                "orders": result["orders"],
            }
            positions_doc = {
                "timestamp": result["timestamp"],
                "mode": "mt5",
                "positions": result["positions"],
            }
            trades_doc = {
                "timestamp": result["timestamp"],
                "mode": "mt5",
                "trades": result["trades"],
            }
            write_json_state("paper_orders.json", orders_doc)
            write_json_state("paper_positions.json", positions_doc)
            write_json_state("paper_trades.json", trades_doc)
            sync_store_from_doc(config, "orders", orders_doc)
            sync_store_from_doc(config, "positions", positions_doc)
            sync_store_from_doc(config, "trades", trades_doc)
            new_closed = result.get("new_closed_trades", [])
            if new_closed:
                wins = sum(1 for t in new_closed if t.get("result") == "win")
                logger.info("Closed trades detected: %d (%d wins, %d losses)", len(new_closed), wins, len(new_closed) - wins)
            logger.info(
                "MT5 execution: placed=%d errors=%d positions=%d equity=%.2f",
                len(result.get("placed", [])),
                len(result.get("errors", [])),
                len(result["positions"]),
                result["balance"]["equity"],
            )
            from core.audit_log import append_event
            from core.ops_alerts import alert_execution_error

            for order in result.get("placed", []):
                append_event(
                    "order.placed",
                    symbol=order.get("symbol"),
                    details={"ticket": order.get("ticket"), "side": order.get("side"), "lot": order.get("volume")},
                )
            for err in result.get("errors", []):
                sym = err.get("symbol", "?")
                msg = str(err.get("error") or err)
                append_event("order.error", symbol=sym, details={"error": msg})
                alert_execution_error(config, sym, msg)
                if is_market_closed_error(err):
                    record_market_closed(sym, config)
                    logger.info("Market closed for %s - backing off %.0f min", sym, float((config.get('execution') or {}).get('market_closed_backoff_minutes', 5)))
            for order in result.get("placed", []):
                # a successful placement means the market reopened -> clear back-off
                clear_backoff(order.get("symbol"))
            return result
        finally:
            connection.disconnect()
    else:
        if config["execution"].get("live_trading_enabled") is True:
            logger.error("live_trading_enabled blocked — use mode: mt5 instead")
            return None

        prices, _price_source = _collect_prices(config, logger)
        prior_positions = list(positions)

        # Bankbot reversal close (paper mode): same logic as MT5 path
        approved, _bb_closed_paper = _close_bankbot_opposite_positions(
            approved, positions, config, logger,
        )
        # For paper mode, the positions were already removed in-place by the
        # helper function. No actual broker close needed.

        broker = PaperBroker(config, logger)
        result = broker.process_approved_signals(approved, prices, orders, positions, trades, balance)

        tracker = TradeTracker(logger)
        result["trades"], added = tracker.merge_new_trades(trades, result["trades"][len(trades):])
        disappeared = tracker.detect_paper_closed(prior_positions, result["positions"], prices)
        if disappeared:
            result["trades"], more = tracker.merge_new_trades(result["trades"], disappeared)
            added.extend(more)
        if added:
            wins = sum(1 for t in added if t.get("result") == "win")
            logger.info("Paper closed trades: %d new (%d wins, %d losses)", len(added), wins, len(added) - wins)

        orders_doc = {"timestamp": result["timestamp"], "balance": result["balance"], "orders": result["orders"]}
        positions_doc = {"timestamp": result["timestamp"], "positions": result["positions"]}
        trades_doc = {"timestamp": result["timestamp"], "trades": result["trades"]}
        write_json_state("paper_orders.json", orders_doc)
        write_json_state("paper_positions.json", positions_doc)
        write_json_state("paper_trades.json", trades_doc)
        sync_store_from_doc(config, "orders", orders_doc)
        sync_store_from_doc(config, "positions", positions_doc)
        sync_store_from_doc(config, "trades", trades_doc)
        logger.info("Paper portfolio: cash=%.2f equity=%.2f positions=%d", result["balance"]["cash"], result["balance"]["equity"], len(result["positions"]))
        return result


if __name__ == "__main__":
    run()