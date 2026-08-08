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
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_collector import DataCollector
from core.health_monitor import HealthMonitor
from core.history_manager import HistoryManager
from core.mt5_connection_manager import MT5ConnectionManager
from core.mt5_terminal_manager import MT5TerminalManager, latest_market_timestamp
from core.symbol_manager import SymbolManager
from core.utils import ensure_dirs, load_config, setup_logger, utc_now_iso, write_json_state


def _validate_payload(data: dict, entry_tf: str, bias_tf: str, m1_tf: str | None = None) -> None:
    """Validate the collector payload. ``m1_tf`` is validated when the M1
    structure shadow engine is enabled (see collector)."""
    for key in ("timestamp", "source", "symbol_map", "account", "symbols"):
        if key not in data:
            raise ValueError(f"Missing field: {key}")
    if data["source"] != "mt5":
        raise ValueError(f"Expected source=mt5, got {data['source']!r}")
    required = (entry_tf, bias_tf) + ((m1_tf,) if m1_tf else ())
    for logical, payload in data["symbols"].items():
        if "broker_symbol" not in payload:
            raise ValueError(f"{logical}: missing broker_symbol")
        for tf in required:
            if tf not in payload or not isinstance(payload[tf], list):
                if tf == m1_tf:
                    continue  # M1 is shadow/optional — its absence must not fail the payload
                raise ValueError(f"{logical}: invalid {tf}")


def _m1_tf_if_enabled(config: dict) -> str | None:
    """Return 'M1' when the M1 structure shadow engine is enabled, else None."""
    m1_cfg = config.get("m1_structure") or {}
    return "M1" if bool(m1_cfg.get("enabled", False)) else None


def _candle_refresh_interval(config: dict) -> float:
    """Cadence of the real market-data refresh inside the single event worker.

    The M1 shadow engine consumes the freshest possible candle data; the
    default 10s matches the M1 service cadence and keeps the current FORMING
    M1 candle comfortably inside the max_data_age_seconds freshness gate.
    Configurable via ``m1_structure.candle_refresh_interval_seconds``.
    """
    m1_cfg = config.get("m1_structure") or {}
    return float(m1_cfg.get("candle_refresh_interval_seconds", 10))


def _make_candle_refresh_fn(config: dict, logger) -> Any:
    """The worker's real refresh path: pull via the shared MT5Owner session,
    write state/latest_candles.json atomically, record feed health.
    Returns the payload dict on success, False on failure."""
    def _refresh() -> dict | bool:
        return candle_refresh_now(config=config, logger=logger)
    return _refresh


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
    m1_tf = _m1_tf_if_enabled(config)
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

        _validate_payload(data, entry_tf, bias_tf, m1_tf)
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

        collected_tfs = (entry_tf, bias_tf) + ((m1_tf,) if m1_tf else ())
        total = sum(len(data["symbols"][s].get(tf, [])) for s in data["symbols"] for tf in collected_tfs)
        log.info(
            "Saved latest_candles.json — login=%s mode=%s candles=%d timings=%s",
            account["login"], account["account_mode"], total, timings,
        )

        # Start the process-global event-loop worker ONLY on first startup()
        # call. It runs the REAL candle refresh every
        # m1_structure.candle_refresh_interval_seconds (default 10s), keeping
        # latest_candles.json current independently of the slow pipeline.
        started = terminal_mgr.start_event_loop(
            poll_interval_sec=0.10,
            candle_interval_sec=_candle_refresh_interval(config),
            connect_now=False,  # we already have a connection from above
            candle_refresh_fn=_make_candle_refresh_fn(config, log),
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
    uses internally; centralizing it here means tests + tools exercise
    the same code-path the worker does in production. Records the outcome
    in the shared feed-health state so observability stays coherent.
    """
    if config is None:
        config = load_config()
    log = logger or setup_logger("data_loop", "data_loop.log")
    mt5_cfg = config["mt5"]
    entry_tf = mt5_cfg["timeframes"]["entry"]
    bias_tf = mt5_cfg["timeframes"]["bias"]
    m1_tf = _m1_tf_if_enabled(config)
    connection = MT5ConnectionManager(config, log)
    symbol_mgr = SymbolManager(config, log)
    try:
        connection.connect()
        broker_symbols = symbol_mgr.discover()
        collector = DataCollector(config, connection, symbol_mgr, log)
        data = collector.pull_latest()
        _validate_payload(data, entry_tf, bias_tf, m1_tf)
        write_json_state("latest_candles.json", data)
        MT5TerminalManager.record_candle_refresh_result(
            ok=True, market_ts=latest_market_timestamp(data),
        )
        return data
    except Exception as exc:
        log.warning("candle_refresh_now failed: %s", exc)
        MT5TerminalManager.record_candle_refresh_result(ok=False, error=str(exc))
        return False
    finally:
        connection.disconnect()


def run() -> dict | bool:
    """Lightweight heartbeat — ensures the single process-owned event worker
    is alive and writes health. Refreshes candles ONLY when the feed is
    genuinely stale (e.g. MT5 disconnected and reconnected).

    Single-worker contract (2026-08-08): the event worker is owned at class
    level inside MT5TerminalManager, so this function NEVER spawns a second
    worker — repeated calls are no-ops while the shared worker is alive. The
    worker itself performs the real candle refresh every
    m1_structure.candle_interval_seconds (default 10s); we deliberately do
    NOT call ``candle_refresh_now()`` per cycle because that would double-hit
    MT5 (rate budget waste). Only fall back to manual refresh when the feed's
    last successful refresh is older than ``STALE_THRESHOLD_SEC``.
    """
    STALE_THRESHOLD_SEC = 30.0
    ensure_dirs()
    config = load_config()
    logger = setup_logger("data_loop", "data_loop.log")

    try:
        terminal_mgr = MT5TerminalManager(config, logger)
        # If startup() hasn't been called yet (e.g. legacy supervisor),
        # spin up the process-global worker here so we never silently miss
        # the event worker. Returns True only when a NEW worker was started.
        started = terminal_mgr.start_event_loop(
            poll_interval_sec=0.10,
            candle_interval_sec=_candle_refresh_interval(config),
            connect_now=False,
            candle_refresh_fn=_make_candle_refresh_fn(config, logger),
        )
        if started:
            logger.info("Event-loop worker spawned lazily from run() (single owner)")
            # Run only one candle refresh on first start to seed
            # latest_candles.json — the worker takes over from there.
            return candle_refresh_now(config=config, logger=logger) or True

        # Worker already running. Refresh manually ONLY when the feed is
        # genuinely stale — the worker does continuous refreshes itself.
        feed = terminal_mgr.feed_status()
        if not feed.get("worker_alive"):
            logger.warning("Event worker not alive; manual refresh once")
            return candle_refresh_now(config=config, logger=logger) or True
        last_success = feed.get("last_refresh_success_at")
        if last_success and (time.time() - _parse_iso_age(last_success)) <= STALE_THRESHOLD_SEC:
            return True  # worker is refreshing on cadence; do not double-hit MT5
        logger.info(
            "Feed quiet since %s; refreshing candles once", last_success,
        )
        return candle_refresh_now(config=config, logger=logger) or True
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
