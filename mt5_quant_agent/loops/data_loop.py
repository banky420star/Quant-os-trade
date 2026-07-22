"""Agent 1: Data Loop — MT5 infrastructure pipeline (read-only, no trades).

Pipeline:
  Terminal Manager -> Connection Manager -> Symbol Manager -> Data Collector -> History Manager
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_collector import DataCollector
from core.health_monitor import HealthMonitor
from core.history_manager import HistoryManager
from core.mt5_connection_manager import MT5ConnectionManager
from core.mt5_terminal_manager import MT5TerminalManager
from core.symbol_manager import SymbolManager
from core.utils import ensure_dirs, load_config, setup_logger, utc_now_iso, write_json_state


def _validate_payload(data: dict, entry_tf: str, bias_tf: str) -> None:
    for key in ("timestamp", "source", "symbol_map", "account", "symbols"):
        if key not in data:
            raise ValueError(f"Missing field: {key}")
    if data["source"] != "mt5":
        raise ValueError(f"Expected source=mt5, got {data['source']!r}")
    for logical, payload in data["symbols"].items():
        if "broker_symbol" not in payload:
            raise ValueError(f"{logical}: missing broker_symbol")
        for tf in (entry_tf, bias_tf):
            if tf not in payload or not isinstance(payload[tf], list):
                raise ValueError(f"{logical}: invalid {tf}")


def run() -> dict:
    """Pull real MT5 candles via infrastructure layer. Never places trades."""
    ensure_dirs()
    config = load_config()
    logger = setup_logger("data_loop", "data_loop.log")
    mt5_cfg = config["mt5"]
    entry_tf = mt5_cfg["timeframes"]["entry"]
    bias_tf = mt5_cfg["timeframes"]["bias"]
    timings: dict[str, float] = {}

    logger.info("=== Data Loop starting (infrastructure pipeline) ===")
    logger.info(
        "MT5: use_logged_in_account=%s account_mode=%s",
        mt5_cfg.get("use_logged_in_account", True),
        mt5_cfg.get("account_mode", "demo"),
    )

    terminal_mgr = MT5TerminalManager(config, logger)
    alignment = terminal_mgr.ensure_terminal(auto_launch=mt5_cfg.get("auto_launch_terminal", False))
    logger.info("Terminal status: %s aligned=%s", alignment.get("status"), alignment.get("aligned"))

    connection = MT5ConnectionManager(config, logger)
    try:
        t0 = time.perf_counter()
        connection.connect()
        timings["connect_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        account = connection.account_snapshot()
        write_json_state("account.json", {"timestamp": utc_now_iso(), **account})
        logger.info("Account: login=%s server=%s mode=%s", account["login"], account["server"], account["account_mode"])

        symbol_mgr = SymbolManager(config, logger)
        t0 = time.perf_counter()
        broker_symbols = symbol_mgr.discover()
        timings["symbol_discovery_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        write_json_state("broker_symbols.json", broker_symbols)

        collector = DataCollector(config, connection, symbol_mgr, logger)
        t0 = time.perf_counter()
        data = collector.pull_latest()
        timings["collect_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        _validate_payload(data, entry_tf, bias_tf)
        write_json_state("latest_candles.json", data)

        history_mgr = HistoryManager(config, collector, logger)
        if config.get("history", {}).get("enabled", True):
            t0 = time.perf_counter()
            mode = config.get("history", {}).get("update_mode", "incremental")
            if mode == "full":
                history_mgr.download_full(broker_symbols["resolved"], entry_tf)
            else:
                history_mgr.update_incremental(broker_symbols["resolved"], entry_tf)
                history_mgr.update_incremental(broker_symbols["resolved"], bias_tf)
            timings["history_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            write_json_state("history_status.json", history_mgr.status(broker_symbols["resolved"]))

        total = sum(len(data["symbols"][s].get(tf, [])) for s in data["symbols"] for tf in (entry_tf, bias_tf))
        logger.info(
            "Saved latest_candles.json — login=%s mode=%s candles=%d timings=%s",
            account["login"],
            account["account_mode"],
            total,
            timings,
        )

        monitor = HealthMonitor(config, connection, history_mgr, logger)
        health = monitor.write_health(loop_timings=timings)
        logger.info("Health: %s", health["status"])
        logger.info("=== Data Loop complete ===")
        return data

    except ConnectionError as exc:
        logger.error("Data Loop connection failed: %s", exc)
        logger.error(traceback.format_exc())
        HealthMonitor(config, logger=logger).write_health(loop_timings=timings)
        raise
    except Exception as exc:
        logger.error("Data Loop failed: %s", exc)
        logger.error(traceback.format_exc())
        raise
    finally:
        connection.disconnect()


if __name__ == "__main__":
    run()