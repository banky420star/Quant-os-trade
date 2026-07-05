"""Agent 4: Verifier Loop — approve or reject candidate signals."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.mt5_client import MT5Client, format_mt5_connection_error, log_session_alignment
from core.position_sizing import symbol_spec_from_mt5
from core.symbol_manager import broker_symbol
from core.mt5_connection_manager import MT5ConnectionManager
from core.position_sync import fetch_mt5_agent_positions
from core.trade_limits import enrich_positions_with_orders
from core.verifier import Verifier
from core.utils import (
    fail_safe_missing,
    load_config,
    read_json_state,
    setup_logger,
    utc_now_iso,
    write_json_state,
)


def _paper_spread_fallback(
    symbols: list[str],
    features: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, float]:
    """Use feature spread when present; otherwise spread=0 (within max_spread allowance)."""
    feature_symbols = features.get("symbols", {})
    max_spread_cfg = config.get("filters", {}).get("max_spread_points", {})
    spread_data: dict[str, float] = {}

    for symbol in symbols:
        feat = feature_symbols.get(symbol, {})
        spread = feat.get("spread_points", feat.get("spread"))
        if spread is not None:
            spread_data[symbol] = float(spread)
        else:
            # Paper mode: unknown spread treated as 0 (always <= configured max)
            spread_data[symbol] = 0.0
        spread_data[symbol] = min(spread_data[symbol], float(max_spread_cfg.get(symbol, 999)))

    return spread_data


def _collect_spread_data(
    config: dict[str, Any],
    features: dict[str, Any],
    logger,
) -> tuple[dict[str, float], str]:
    """Fetch live spreads via MT5Agent path; fall back for paper mode on IPC failure."""
    symbols = list(config["mt5"]["symbols"])
    paper_mode = config.get("execution", {}).get("mode") == "paper"
    spread_data: dict[str, float] = {}
    source = "mt5"

    client = MT5Client(config, logger)
    try:
        client.connect()
        for symbol in symbols:
            spread_data[symbol] = client.get_spread_points(symbol)
        if not any(spread_data.values()):
            fallback = _paper_spread_fallback(symbols, features, config)
            if any(fallback.values()):
                spread_data = fallback
                source = "features_fallback"
                logger.warning(
                    "MT5 returned zero spreads for all symbols — using features.json fallback"
                )
            else:
                raise ConnectionError("MT5 connected but returned zero spreads for all symbols")
    except (ConnectionError, OSError, RuntimeError) as exc:
        session_info = log_session_alignment(logger)
        err_msg = format_mt5_connection_error(exc, session_info) if isinstance(exc, ConnectionError) else str(exc)
        if paper_mode:
            spread_data = _paper_spread_fallback(symbols, features, config)
            source = "paper_fallback"
            logger.warning("MT5 spread fetch failed (%s) — using paper-mode spread fallback", err_msg)
        else:
            fallback = _paper_spread_fallback(symbols, features, config)
            if any(fallback.values()):
                spread_data = fallback
                source = "features_fallback"
                logger.warning(
                    "MT5 spread fetch failed (%s) — using features.json fallback",
                    err_msg,
                )
            else:
                logger.error("MT5 spread fetch failed (%s)", err_msg)
                raise
    finally:
        client.disconnect()

    return spread_data, source


def _collect_symbol_specs(
    config: dict[str, Any],
    logger,
) -> dict[str, dict[str, float]]:
    """Live broker contract specs for executable sizing (falls back in exposure layer)."""
    if config.get("execution", {}).get("mode") != "mt5":
        return {}
    specs: dict[str, dict[str, float]] = {}
    client = MT5Client(config, logger)
    try:
        import MetaTrader5 as mt5

        client.connect()
        for symbol in config["mt5"]["symbols"]:
            broker = broker_symbol(symbol)
            if not mt5.symbol_select(broker, True):
                continue
            info = mt5.symbol_info(broker)
            if info is not None:
                specs[symbol] = symbol_spec_from_mt5(info)
    except (ConnectionError, OSError, RuntimeError) as exc:
        logger.warning("Symbol spec fetch failed (%s) — using defaults", exc)
    finally:
        client.disconnect()
    return specs


def _collect_active_positions(
    config: dict[str, Any],
    logger,
) -> tuple[list[dict[str, Any]], str]:
    """Use live MT5 positions in mt5 mode; paper_positions.json in paper mode."""
    mode = config.get("execution", {}).get("mode", "paper")
    if mode != "mt5":
        positions = read_json_state("paper_positions.json", default={"positions": []})
        active = positions.get("positions", positions) if isinstance(positions, dict) else positions
        return list(active or []), "paper"

    connection = MT5ConnectionManager(config, logger)
    try:
        connection.connect()
        active = fetch_mt5_agent_positions(config, logger)
        write_json_state("paper_positions.json", {
            "timestamp": utc_now_iso(),
            "mode": "mt5",
            "source": "mt5_sync",
            "positions": active,
        })
        return active, "mt5"
    finally:
        connection.disconnect()


def run() -> dict | None:
    """Verify candidate signals before paper execution."""
    config = load_config()
    logger = setup_logger("verifier_loop", "verifier_loop.log")
    logger.info("Starting verifier loop (mode=%s)", config.get("execution", {}).get("mode", "paper"))
    log_session_alignment(logger)

    if fail_safe_missing("candidate_signals.json", logger):
        return None
    if fail_safe_missing("features.json", logger):
        return None

    candidates_data = read_json_state("candidate_signals.json")
    candidates = candidates_data.get("candidates", [])
    features = read_json_state("features.json")
    active_positions, position_source = _collect_active_positions(config, logger)
    orders_data = read_json_state("paper_orders.json", default={"orders": []})
    active_positions = enrich_positions_with_orders(
        active_positions,
        orders_data.get("orders", []),
    )
    kill_data = read_json_state("kill_switch.json", default={"kill_switch": False})

    spread_data, spread_source = _collect_spread_data(config, features, logger)
    if spread_source == "paper_fallback":
        logger.info(
            "Using paper-mode spread fallback (spread=0 or features; within max_spread allowance): %s",
            spread_data,
        )

    orders_data = read_json_state("paper_orders.json", default={"balance": {}})
    account_data = read_json_state("account.json", default={})
    balance = orders_data.get("balance", {})
    equity = float(
        balance.get("equity")
        or account_data.get("equity")
        or balance.get("cash")
        or account_data.get("balance")
        or config["execution"].get("starting_cash", 1000)
    )
    acct_balance = float(
        account_data.get("balance")
        or balance.get("cash")
        or equity
    )
    symbol_specs = _collect_symbol_specs(config, logger)

    trades_data = read_json_state("paper_trades.json", default={"trades": []})
    closed_trades = list(trades_data.get("trades", []))

    verifier = Verifier(config, logger)
    approved, rejected = verifier.verify_batch(
        candidates,
        features,
        active_signals=active_positions,
        kill_switch=kill_data.get("kill_switch", False),
        spread_data=spread_data,
        equity=equity,
        balance=acct_balance,
        closed_trades=closed_trades,
        symbol_specs=symbol_specs or None,
    )

    meta = {
        "timestamp": utc_now_iso(),
        "spread_source": spread_source,
        "position_source": position_source,
        "active_position_count": len(active_positions),
        "spread_data": spread_data,
        "kill_switch": kill_data.get("kill_switch", False),
        "equity": equity,
        "candidate_count": len(candidates),
    }
    write_json_state(
        "approved_signals.json",
        {**meta, "count": len(approved), "approved": approved},
    )
    write_json_state(
        "rejected_signals.json",
        {**meta, "count": len(rejected), "rejected": rejected},
    )
    if approved:
        from core.signal_archive import archive_signals

        n = archive_signals(approved, source="verifier")
        if n:
            logger.info("Signal archive: +%d approved signals (durable for trade enrichment)", n)
    logger.info("Approved %d, rejected %d (spread_source=%s)", len(approved), len(rejected), spread_source)
    return {"approved": approved, "rejected": rejected, "spread_source": spread_source}


if __name__ == "__main__":
    run()