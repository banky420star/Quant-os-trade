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
from core.trade_tracker import TradeTracker
from core.state_store import (
    approved_available,
    read_approved_signals,
    sync_store_from_doc,
)
from core.utils import (
    fail_safe_missing,
    load_config,
    read_json_state,
    setup_logger,
    write_json_state,
)


def _mt5_drain_with_ipc_retry(
    config: dict,
    logger: logging.Logger,
    approved: list,
    orders: list,
    trades: list,
    positions: list,
) -> dict:
    """Run the MT5 broker.process_approved_signals call with a single IPC reconnect retry.

    Hotfix 2026-07-28: MT5 order placement occasionally fails with
    ``(-10004, 'No IPC connection')`` when a previous loop (fast_position_guard
    / data_loop) disconnected the terminal between the ``execution_loop``
    session check and the actual ``order_send`` call. Without a retry, every
    approved signal hits the dead IPC handle and the bot sits idle for the
    rest of the cycle. We re-establish the connection up to once and run the
    broker call again; second failure is logged and surfaced to the dashboard
    via ``paper_orders.json`` so the operator can see it instead of silently
    dropping the intent.
    """
    # Tight IPC error signatures — NOT a bare substring match. Real Money
    # 2026-07-28: bare "IPC" would catch unrelated module paths; only
    # MT5-specific error codes / messages should trigger the reconnect.
    IPC_ERR_SIGS = ("-10004", "No IPC connection", "IPC pipe closed", "not connected to MetaTrader")
    attempts = 0
    while attempts < 2:
        attempts += 1
        connection = MT5ConnectionManager(config, logger)
        try:
            connection.connect()
            broker = MT5Broker(config, logger)
            try:
                result = broker.process_approved_signals(
                    approved,
                    orders,
                    existing_trades=trades,
                    existing_positions=positions,
                )
                return _finalize_mt5_execution(config=config, logger=logger, result=result)
            except Exception as exc:
                err_str = str(exc) or ""
                if attempts < 2 and any(sig in err_str for sig in IPC_ERR_SIGS):
                    logger.warning(
                        "MT5 IPC error during process_approved_signals (attempt %d): %s - reconnecting",
                        attempts, err_str,
                    )
                    continue
                raise
        except Exception as exc:
            err_str = str(exc) or ""
            if attempts < 2 and any(sig in err_str for sig in IPC_ERR_SIGS):
                logger.warning(
                    "MT5 IPC error in outer attempt %d: %s - reconnecting and retrying once",
                    attempts, err_str,
                )
                continue
            logger.error("MT5 execution outer failure: %s", err_str)
            raise
        finally:
            try:
                connection.disconnect()
            except Exception:
                pass
    # Unreachable. The inner except in the second iteration (attempts == 2)
    # raises directly when the IPC error fires — both attempts either return
    # early on success or raise before the loop re-evaluates. The post-loop
    # raise was kept here previously as belt-and-braces but never executed.
    # If a future change ever lands here, fail loudly rather than silently.


def _finalize_mt5_execution(*, config: dict, logger, result: dict) -> dict:
    """Persist + alert on the broker result produced by _mt5_drain_with_ipc_retry."""
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
    # REVIEW FIX 2026-07-28: do NOT swallow audit/alert errors silently.
    # Original inline code did not wrap these calls — let any failure surface
    # to the supervisor so the operator sees the alert failure rather than
    # not knowing the audit silently failed.
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
        clear_backoff(order.get("symbol"))
    return result


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


def _check_execution_allowed(config: dict, logger) -> bool:
    kill = read_json_state("kill_switch.json", default={"kill_switch": False})
    if kill.get("kill_switch"):
        logger.error("Execution blocked — kill switch is ON: %s", kill.get("reason"))
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
    return True


def run() -> dict | None:
    """Execute approved signals — paper mode or real MT5 orders."""
    config = load_config()
    logger = setup_logger("execution_loop", "execution_loop.log")
    mode = config["execution"].get("mode", "paper")
    logger.info("Starting execution loop (mode=%s)", mode)
    log_session_alignment(logger)

    if not _check_execution_allowed(config, logger):
        return None

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
        return _mt5_drain_with_ipc_retry(
            config=config,
            logger=logger,
            approved=approved,
            orders=orders,
            trades=trades,
            positions=positions,
        )
    else:
        if config["execution"].get("live_trading_enabled") is True:
            logger.error("live_trading_enabled blocked — use mode: mt5 instead")
            return None

        prices, _price_source = _collect_prices(config, logger)
        prior_positions = list(positions)
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