"""History Loop — bulk download or incremental Parquet updates."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_collector import DataCollector
from core.history_manager import HistoryManager
from core.mt5_connection_manager import MT5ConnectionManager
from core.symbol_manager import SymbolManager
from core.utils import ensure_dirs, load_config, read_json_state, setup_logger, write_json_state


def run(mode: str | None = None) -> dict:
    ensure_dirs()
    config = load_config()
    logger = setup_logger("history_loop", "history_loop.log")
    mode = mode or config.get("history", {}).get("update_mode", "incremental")
    entry_tf = config["mt5"]["timeframes"]["entry"]

    logger.info("=== History Loop starting mode=%s ===", mode)

    broker = read_json_state("broker_symbols.json", default={})
    symbol_map = broker.get("resolved", {})

    connection = MT5ConnectionManager(config, logger)
    try:
        connection.connect()
        if not symbol_map:
            symbol_mgr = SymbolManager(config, logger)
            broker = symbol_mgr.discover()
            symbol_map = broker["resolved"]
            write_json_state("broker_symbols.json", broker)

        symbol_mgr = SymbolManager(config, logger)
        symbol_mgr.set_resolved(symbol_map)
        collector = DataCollector(config, connection, symbol_mgr, logger)
        history = HistoryManager(config, collector, logger)

        if mode == "full":
            result = history.download_full(symbol_map, entry_tf)
        else:
            result = history.update_incremental(symbol_map, entry_tf)
            bias_tf = config["mt5"]["timeframes"]["bias"]
            inc2 = history.update_incremental(symbol_map, bias_tf)
            result["symbols"].update(inc2.get("symbols", {}))

        status = history.status(symbol_map)
        write_json_state("history_status.json", status)
        logger.info("=== History Loop complete ===")
        return {"run": result, "status": status}
    finally:
        connection.disconnect()


if __name__ == "__main__":
    run()