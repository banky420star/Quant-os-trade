"""Backfill parquet history so benchmark replay can meet coverage thresholds."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.data_collector import DataCollector
from core.history_manager import HistoryManager
from core.mt5_connection_manager import MT5ConnectionManager
from core.performance_benchmark import parquet_calendar_span, resolve_benchmark_replay_params
from core.symbol_manager import SymbolManager
from core.utils import ensure_dirs, load_config, read_json_state, setup_logger, write_json_state


def ensure_history(
    config: dict | None = None,
    *,
    scratch: Path | None = None,
    logger=None,
) -> dict:
    ensure_dirs()
    config = config or load_config()
    log = logger or setup_logger("ensure_benchmark_history", "ensure_benchmark_history.log")
    perf = config.get("performance", {})
    min_days = float(perf.get("min_replay_window_days", 7))
    symbols = list(config["mt5"]["symbols"])
    entry_tf = config["mt5"]["timeframes"]["entry"]
    bias_tf = config["mt5"]["timeframes"]["bias"]

    before = parquet_calendar_span(config, symbols)
    resolved_before = resolve_benchmark_replay_params(config, symbols)
    report: dict = {
        "min_replay_window_days": min_days,
        "before": {"history_span": before, "replay_params": resolved_before},
        "mt5_backfill_attempted": False,
        "mt5_backfill_ok": False,
        "after": None,
    }

    needs_backfill = any(
        (before.get(sym, {}).get("calendar_days") or 0) < min_days for sym in symbols
    ) or resolved_before.get("estimated_replay_window_days", 0) < min_days

    if needs_backfill:
        log.info("History below %.1f days — attempting MT5 full download", min_days)
        report["mt5_backfill_attempted"] = True
        connection = MT5ConnectionManager(config, log)
        try:
            connection.connect()
            broker = read_json_state("broker_symbols.json", default={})
            symbol_map = broker.get("resolved", {})
            if not symbol_map:
                symbol_mgr = SymbolManager(config, log)
                broker = symbol_mgr.discover()
                symbol_map = broker["resolved"]
                write_json_state("broker_symbols.json", broker)

            symbol_mgr = SymbolManager(config, log)
            symbol_mgr.set_resolved(symbol_map)
            collector = DataCollector(config, connection, symbol_mgr, log)
            history = HistoryManager(config, collector, log)
            history.download_full(symbol_map, entry_tf)
            history.download_full(symbol_map, bias_tf)
            report["mt5_backfill_ok"] = True
            log.info("MT5 history download complete")
        except Exception as exc:
            log.warning("MT5 backfill failed: %s", exc)
            report["mt5_backfill_error"] = str(exc)
        finally:
            connection.disconnect()

    after = parquet_calendar_span(config, symbols)
    resolved_after = resolve_benchmark_replay_params(config, symbols)
    report["after"] = {"history_span": after, "replay_params": resolved_after}
    report["coverage_sufficient"] = resolved_after.get("estimated_replay_window_days", 0) >= min_days

    if scratch:
        scratch.mkdir(parents=True, exist_ok=True)
        out = scratch / "benchmark_history.json"
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        log.info("Wrote %s", out)

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure benchmark parquet history")
    parser.add_argument(
        "--scratch",
        type=Path,
        default=Path(r"C:\Users\ADMINI~1\AppData\Local\Temp\3\grok-goal-5d7c28d4f228\implementer"),
    )
    args = parser.parse_args()
    report = ensure_history(scratch=args.scratch)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("coverage_sufficient") else 1


if __name__ == "__main__":
    raise SystemExit(main())