"""Cycle 17: forced deep history download (read-only) for a longer M5 OOS window.

Replicates ensure_benchmark_history.py's MT5 connection path but UNCONDITIONALLY
calls download_full (no min-days gate) so we can pull ~200k bars and deepen the
M5/M15 window from ~260 days to ~2.5+ years. Read-only: pulls candles, writes
parquet. Does not place orders. Demo session (account_mode=demo,
live_trading_enabled=false).
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.data_collector import DataCollector
from core.history_manager import HistoryManager
from core.mt5_connection_manager import MT5ConnectionManager
from core.symbol_manager import SymbolManager
from core.utils import load_config, read_json_state, setup_logger, write_json_state


def main() -> int:
    config = load_config()
    log = setup_logger("deep_ingest", "deep_ingest.log")
    entry_tf = config["mt5"]["timeframes"]["entry"]   # M5
    bias_tf = config["mt5"]["timeframes"]["bias"]     # M15
    log.info("max_bars=%s entry_tf=%s bias_tf=%s",
             config["history"]["max_bars"], entry_tf, bias_tf)

    conn = MT5ConnectionManager(config, log)
    conn.connect()
    try:
        broker = read_json_state("broker_symbols.json", default={})
        symbol_map = broker.get("resolved", {})
        if not symbol_map:
            sm = SymbolManager(config, log)
            broker = sm.discover()
            symbol_map = broker["resolved"]
            write_json_state("broker_symbols.json", broker)
        sm = SymbolManager(config, log)
        sm.set_resolved(symbol_map)
        collector = DataCollector(config, conn, sm, log)
        history = HistoryManager(config, collector, log)

        report = {"max_bars": config["history"]["max_bars"]}
        report["M5"] = history.download_full(symbol_map, entry_tf)
        report["M15"] = history.download_full(symbol_map, bias_tf)
        print(json.dumps(report, indent=2, default=str))
        return 0
    finally:
        conn.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())