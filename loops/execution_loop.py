"""Agent 5: Execution Loop — paper simulation OR real MT5 demo orders."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.mt5_client import MT5Client, format_mt5_connection_error, log_session_alignment
from core.mt5_connection_manager import MT5ConnectionManager
from core.mt5_broker import MT5Broker
from core.paper_broker import PaperBroker
from core.trade_tracker import TradeTracker
from core.utils import (
    fail_safe_missing,
    load_config,
    read_json_state,
    setup_logger,
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

    if fail_safe_missing("approved_signals.json", logger):
        return None

    approved_data = read_json_state("approved_signals.json")
    approved = approved_data.get("approved", [])

    if not approved:
        logger.info("No approved signals to execute")
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
            result = broker.process_approved_signals(
                approved,
                orders,
                existing_trades=trades,
                existing_positions=positions,
            )
            write_json_state("paper_orders.json", {
                "timestamp": result["timestamp"],
                "mode": "mt5",
                "balance": result["balance"],
                "account": result["account"],
                "orders": result["orders"],
            })
            write_json_state("paper_positions.json", {
                "timestamp": result["timestamp"],
                "mode": "mt5",
                "positions": result["positions"],
            })
            write_json_state("paper_trades.json", {
                "timestamp": result["timestamp"],
                "mode": "mt5",
                "trades": result["trades"],
            })
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
            return result
        finally:
            connection.disconnect()
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

        write_json_state("paper_orders.json", {"timestamp": result["timestamp"], "balance": result["balance"], "orders": result["orders"]})
        write_json_state("paper_positions.json", {"timestamp": result["timestamp"], "positions": result["positions"]})
        write_json_state("paper_trades.json", {"timestamp": result["timestamp"], "trades": result["trades"]})
        logger.info("Paper portfolio: cash=%.2f equity=%.2f positions=%d", result["balance"]["cash"], result["balance"]["equity"], len(result["positions"]))
        return result


if __name__ == "__main__":
    run()