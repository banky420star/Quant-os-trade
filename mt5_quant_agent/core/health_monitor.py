"""Health Monitor — MT5, connection, account, data freshness, system resources."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any

from core.history_manager import HistoryManager
from core.mt5_connection_manager import MT5ConnectionManager
from core.mt5_terminal_manager import MT5TerminalManager
from core.utils import LOGS_DIR, STATE_DIR, utc_now_iso, write_json_state

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore


class HealthMonitor:
    """Produce state/health.json with system and MT5 vitals."""

    def __init__(
        self,
        config: dict[str, Any],
        connection: MT5ConnectionManager | None = None,
        history: HistoryManager | None = None,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self.connection = connection
        self.history = history
        self.logger = logger or logging.getLogger("health_monitor")
        self.terminal_manager = MT5TerminalManager(config, logger)

    def run_checks(self, loop_timings: dict[str, float] | None = None) -> dict[str, Any]:
        """Run all health checks and return health payload."""
        t0 = time.perf_counter()
        alignment = self.terminal_manager.session_alignment()
        mt5_alive = self.terminal_manager.is_alive(self.config.get("mt5", {}).get("path"))

        connection_alive = False
        account_logged_in = False
        ping: dict[str, Any] = {}
        account: dict[str, Any] = {}
        connect_latency_ms = None

        if self.connection and self.connection.connected:
            ping = self.connection.ping()
            connection_alive = ping.get("alive", False)
            account_logged_in = ping.get("logged_in", False)
            account = self.connection.account_snapshot()
            connect_latency_ms = account.get("connect_latency_ms")

        candles_fresh = self._check_candle_freshness()
        history_status = {}
        if self.history and self.connection:
            try:
                from core.utils import read_json_state
                broker = read_json_state("broker_symbols.json", default={})
                symbol_map = broker.get("resolved", {})
                if symbol_map:
                    history_status = self.history.status(symbol_map)
            except Exception as exc:
                history_status = {"error": str(exc)}

        disk = shutil.disk_usage(str(STATE_DIR.parent))
        memory_mb = None
        if psutil:
            memory_mb = round(psutil.virtual_memory().percent, 1)

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        issues: list[str] = []

        if not mt5_alive:
            issues.append("mt5_terminal_not_running")
        if not alignment.get("aligned"):
            issues.append("session_mismatch")
        if not connection_alive:
            issues.append("connection_down")
        if not account_logged_in:
            issues.append("account_not_logged_in")
        if candles_fresh.get("stale"):
            issues.append("stale_candles")
        if disk.free < 500_000_000:
            issues.append("low_disk_space")

        overall = "healthy" if not issues else ("degraded" if mt5_alive else "unhealthy")

        health = {
            "timestamp": utc_now_iso(),
            "status": overall,
            "issues": issues,
            "mt5": {
                "terminal_alive": mt5_alive,
                "session_aligned": alignment.get("aligned"),
                "processes": alignment.get("mt5_processes"),
                "recommended_terminal": alignment.get("recommended_terminal"),
            },
            "connection": {
                "alive": connection_alive,
                "logged_in": account_logged_in,
                "latency_ms": connect_latency_ms,
                "ping": ping,
            },
            "account": account,
            "candles": candles_fresh,
            "history": history_status,
            "system": {
                "disk_free_gb": round(disk.free / 1_073_741_824, 2),
                "memory_pct": memory_mb,
                "loop_timings_ms": loop_timings or {},
            },
            "check_duration_ms": elapsed_ms,
        }

        self.logger.info("Health check: status=%s issues=%s", overall, issues)
        return health

    def write_health(self, loop_timings: dict[str, float] | None = None) -> dict[str, Any]:
        health = self.run_checks(loop_timings)
        write_json_state("health.json", health)
        return health

    def _check_candle_freshness(self) -> dict[str, Any]:
        from core.utils import read_json_state

        candles = read_json_state("latest_candles.json", default={})
        if not candles or candles.get("source") != "mt5":
            return {"fresh": False, "stale": True, "reason": "no_mt5_candles", "age_minutes": None}

        ts = candles.get("timestamp")
        age_min = None
        try:
            from datetime import datetime, timezone
            then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age_min = round((datetime.now(timezone.utc) - then).total_seconds() / 60, 1)
        except Exception:
            pass

        max_age = int(self.config.get("health", {}).get("max_candle_age_minutes", 30))
        stale = age_min is not None and age_min > max_age
        return {
            "fresh": not stale,
            "stale": stale,
            "source": candles.get("source"),
            "timestamp": ts,
            "age_minutes": age_min,
            "max_age_minutes": max_age,
        }