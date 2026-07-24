"""Agent 1: Data Loop — MT5 infrastructure pipeline (read-only, no trades).

Pipeline:
  Terminal Manager -> Connection Manager -> Symbol Manager -> Data Collector -> History Manager

2026-07-22 architectural change:
=================================
The polling cadence moved from this 30-second run() loop to the
MT5TerminalManager ``_event_loop_worker`` thread (100ms transactions,
1Hz candle updates). ``run()`` is now a lightweight heartbeat that
ensures the event-loop worker is alive; the heavy one-shot work
(symbol discovery, history download, initial candle pull) lives in
``startup()`` which the supervisor calls ONCE at session start.

Pipeline orchestration:
  * startup()     — call ONCE: connect, snapshot, discover symbols, history,
                    AND start_event_loop() so future ticks come from the worker.
  * run()         — call periodically: ensures event-loop worker is alive,
                    refreshes candles if the worker hasn't ticked in N seconds
                    (belt-and-braces), writes health.
  * candle_refresh_now() — manual trigger for tests + /api endpoints.

The supervisor does NOT need to know about this split; calling
``startup()`` once at boot and ``run()`` per cycle is the new contract.
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


def startup(*, logger=None) -> dict:
    """One-shot initialization: connect, snapshot, discover, history, start event worker.

    Idempotent: ``MT5TerminalManager.start_event_loop`` already returns
    False on subsequent calls so calling startup() twice is safe — but
    only the FIRST call will actually run the heavy pipeline. The
    supervisor should treat startup() as a contract: call once at boot.
    """
    ensure_dirs()
    config = load_config()
    log = logger or setup_logger("data_loop", "data_loop.log")
    mt5_cfg = config["mt5"]
    entry_tf = mt5_cfg["timeframes"]["entry"]
    bias_tf = mt5_cfg["timeframes"]["bias"]
    timings: dict[str, float] = {}

    log.info("=== Data Loop startup() (one-shot infrastructure) ===")
    log.info(
        "MT5: use_logged_in_account=%s account_mode=%s",
        mt5_cfg.get("use_logged_in_account", True),
        mt5_cfg.get("account_mode", "demo"),
    )

    terminal_mgr = MT5TerminalManager(config, log)
    alignment = terminal_mgr.ensure_terminal(auto_launch=mt5_cfg.get("auto_launch_terminal", False))
    log.info("Terminal status: %s aligned=%s", alignment.get("status"), alignment.get("aligned"))

    connection = MT5ConnectionManager(config, log)
    data: dict = {}
    try:
        t0 = time.perf_counter()
        connection.connect()
        timings["connect_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        account = connection.account_snapshot()
        write_json_state("account.json", {"timestamp": utc_now_iso(), **account})
        log.info("Account: login=%s server=%s mode=%s",
                 account["login"], account["server"], account["account_mode"])

        symbol_mgr = SymbolManager(config, log)
        t0 = time.perf_counter()
        broker_symbols = symbol_mgr.discover()
        timings["symbol_discovery_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        write_json_state("broker_symbols.json", broker_symbols)

        collector = DataCollector(config, connection, symbol_mgr, log)
        t0 = time.perf_counter()
        data = collector.pull_latest()
        timings["collect_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        _validate_payload(data, entry_tf, bias_tf)
        write_json_state("latest_candles.json", data)

        history_mgr = HistoryManager(config, collector, log)
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
        log.info(
            "Saved latest_candles.json — login=%s mode=%s candles=%d timings=%s",
            account["login"], account["account_mode"], total, timings,
        )

        # Start the event-loop worker ONLY on first startup() call.
        started = terminal_mgr.start_event_loop(
            poll_interval_sec=0.10,
            candle_interval_sec=1.0,
            connect_now=False,  # we already have a connection from above
        )
        log.info("Event loop worker start: %s (already running=%s)",
                 "spawned" if started else "already running",
                 not started)

        HealthMonitor(config, connection, history_mgr, log).write_health(loop_timings=timings)
        log.info("=== Data Loop startup() complete ===")
        return data

    except ConnectionError as exc:
        log.error("Data Loop startup() connection failed: %s", exc)
        log.error(traceback.format_exc())
        HealthMonitor(config, logger=log).write_health(loop_timings=timings)
        raise
    except Exception as exc:
        log.error("Data Loop startup() failed: %s", exc)
        log.error(traceback.format_exc())
        raise
    finally:
        connection.disconnect()


def candle_refresh_now(config: dict | None = None, logger=None) -> dict | bool:
    """Manual one-shot candle refresh — used by /api endpoints + tests.

    Returns the data dict on success, False on failure (e.g. MT5
    disconnected). This is the SAME path that the event-loop worker
    uses internally via ``mt5.copy_rates_from_pos``; centralizing
    it here means tests + tools exercise the same code-path the worker
    does in production.
    """
    if config is None:
        config = load_config()
    log = logger or setup_logger("data_loop", "data_loop.log")
    mt5_cfg = config["mt5"]
    entry_tf = mt5_cfg["timeframes"]["entry"]
    bias_tf = mt5_cfg["timeframes"]["bias"]
    connection = MT5ConnectionManager(config, log)
    symbol_mgr = SymbolManager(config, log)
    try:
        connection.connect()
        broker_symbols = symbol_mgr.discover()
        collector = DataCollector(config, connection, symbol_mgr, log)
        data = collector.pull_latest()
        _validate_payload(data, entry_tf, bias_tf)
        write_json_state("latest_candles.json", data)
        return data
    except Exception as exc:
        log.warning("candle_refresh_now failed: %s", exc)
        return False
    finally:
        connection.disconnect()


def run() -> dict | bool:
    """Lightweight heartbeat — ensures the event-loop worker is alive
    and writes health. Refreshes candles ONLY when the worker has
    been quiet for too long (e.g. MT5 disconnected and reconnected).

    The poll-cycle for this function is the supervisor's pipeline
    interval (typically 30s). The much-faster 100ms MT5 transactions
    poll + 1Hz candle_update events come from
    ``MT5TerminalManager._event_loop_worker`` once startup() spawned
    it — we deliberately do NOT call ``candle_refresh_now()`` per
    cycle because that would double-hit MT5 (rate budget waste).
    Only fall back to manual refresh when ``last_event.timestamp``
    is older than ``STALE_THRESHOLD_SEC``.
    """
    STALE_THRESHOLD_SEC = 30.0
    ensure_dirs()
    config = load_config()
    logger = setup_logger("data_loop", "data_loop.log")

    try:
        terminal_mgr = MT5TerminalManager(config, logger)
        # If startup() hasn't been called yet (e.g. legacy supervisor),
        # spin it up here so we never silently miss the event worker.
        started = terminal_mgr.start_event_loop(
            poll_interval_sec=0.10,
            candle_interval_sec=1.0,
            connect_now=False,
        )
        if started:
            logger.info("Event-loop worker spawned lazily from run()")
            # Run only one candle refresh on first start to seed latest_candles.json
            # — the worker takes over from there.
            return candle_refresh_now(config=config, logger=logger) or True

        # Standard heartbeat: check whether the worker's last event is fresh.
        last = terminal_mgr.last_event
        now = time.time()
        if last is None or (
            last.timestamp
            and (now - _parse_iso_age(last.timestamp)) > STALE_THRESHOLD_SEC
        ):
            logger.info(
                "Event worker stale (last=%s); refreshing candles once",
                getattr(last, "timestamp", None),
            )
            return candle_refresh_now(config=config, logger=logger) or True
        return True
    except Exception as exc:
        logger.error("Data Loop run() failed (non-fatal): %s", exc)
        logger.debug(traceback.format_exc())
        return False


def _parse_iso_age(iso_ts: str) -> float:
    """Best-effort seconds-since-ISO8601; returns 0 on parse error."""
    try:
        from datetime import datetime, timezone
        s = iso_ts.replace("Z", "+00:00")
        t = datetime.fromisoformat(s)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except Exception:
        return 0.0


if __name__ == "__main__":
    import sys as _sys
    if "--startup" in _sys.argv:
        _sys.argv.remove("--startup")
        startup()
    else:
        run()
