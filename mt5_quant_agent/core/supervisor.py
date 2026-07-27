"""Supervisor — manages trading services with auto-restart and health monitoring."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from pathlib import Path

from core.utils import read_json_state, utc_now_iso, write_json_state, STATE_DIR

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore


@dataclass
class ServiceState:
    name: str
    label: str
    status: str = "idle"
    last_run: str | None = None
    last_duration_ms: float = 0.0
    run_count: int = 0
    error_count: int = 0
    restart_count: int = 0
    last_error: str | None = None
    interval_seconds: float = 60.0
    consecutive_errors: int = 0
    last_error_pattern: str | None = None


class ManagedService:
    """Background service thread with crash recovery."""

    def __init__(
        self,
        name: str,
        label: str,
        runner: Callable[[], Any],
        interval_seconds: float,
        logger: logging.Logger,
        *,
        run_once: bool = False,
        on_result: Callable[[str, dict], None] | None = None,
    ):
        self.state = ServiceState(name=name, label=label, interval_seconds=interval_seconds)
        self._runner = runner
        self._logger = logger
        self._run_once = run_once
        self._on_result = on_result
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"svc-{self.state.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.state.status = "running"
            t0 = time.monotonic()
            loop_results: dict[str, str] = {}
            try:
                result = self._runner()
                if isinstance(result, dict):
                    loop_results = {k: str(v) for k, v in result.items() if k != "loops"}
                    if "loops" in result:
                        loop_results.update(result["loops"])
                self.state.run_count += 1
                self.state.status = "ok"
                self.state.last_error = None
            except Exception as exc:
                self.state.error_count += 1
                self.state.status = "error"
                self.state.last_error = str(exc)
                self.state.restart_count += 1
                self.state.consecutive_errors += 1
                self._logger.error("Service %s failed (consecutive=%d): %s", self.state.name, self.state.consecutive_errors, exc)
                self._logger.error(traceback.format_exc())
                # Auto-recovery: if same error repeats 2+ times, try to recover
                self._try_auto_recovery(exc)
            else:
                # Log recovery success after consecutive errors
                if self.state.consecutive_errors > 0:
                    self._logger.info("Service %s recovered after %d consecutive errors", self.state.name, self.state.consecutive_errors)
                self.state.consecutive_errors = 0
            finally:
                self.state.last_run = utc_now_iso()
                self.state.last_duration_ms = round((time.monotonic() - t0) * 1000, 1)
                if self._on_result and loop_results:
                    self._on_result(self.state.name, loop_results)

            if self._run_once:
                self.state.status = "stopped"
                break

            # Back off on consecutive errors to avoid spamming broken loops
            base_sleep = max(1.0, self.state.interval_seconds - self.state.last_duration_ms / 1000)
            sleep_for = base_sleep * min(1 + 0.5 * self.state.consecutive_errors, 5.0)
            if self._stop.wait(sleep_for):
                break

        self.state.status = "stopped"

    def _try_auto_recovery(self, exc: Exception) -> None:
        """Attempt automatic recovery based on error patterns."""
        err_str = str(exc)
        pattern = None

        # Pattern: KeyError signal_id — clear stale approved_signals
        if "signal_id" in err_str:
            pattern = "signal_id_keyerror"
            if self.state.consecutive_errors >= 2:
                self._recover_clear_stale_signals()

        # Pattern: stale state file — force data refresh
        elif any(kw in err_str.lower() for kw in ["stale", "no such file", "jsondecodeerror"]):
            pattern = "stale_state"
            if self.state.consecutive_errors >= 2:
                self._recover_clear_stale_state()

        # Pattern: MT5 connection — reset connection state
        elif any(kw in err_str.lower() for kw in ["not logged in", "no ipc connection", "connection"]):
            pattern = "mt5_connection"
            if self.state.consecutive_errors >= 3:
                self._recover_reset_mt5_state()

        if pattern:
            self.state.last_error_pattern = pattern
            self._logger.warning(
                "Auto-recovery triggered: pattern=%s consecutive=%d action=%s",
                pattern, self.state.consecutive_errors, pattern,
            )

    def _recover_clear_stale_signals(self) -> None:
        """Clear stale approved_signals.json so the verifier can write fresh data."""
        for name in ["approved_signals.json"]:
            stale_file = STATE_DIR / name
            try:
                if stale_file.exists():
                    stale_file.unlink(missing_ok=True)
                    self._logger.warning("Auto-recovery: deleted stale %s", stale_file)
            except Exception as exc:
                self._logger.warning("Auto-recovery: could not delete %s: %s", stale_file, exc)

    def _recover_clear_stale_state(self) -> None:
        """Clear corrupted or stale state files to force a fresh data pull."""
        for name in ["evaluated_signals.json", "candidate_signals.json"]:
            p = STATE_DIR / name
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                ts = data.get("timestamp", "")
                if ts:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()
                    if age > 300:  # older than 5 minutes
                        p.unlink(missing_ok=True)
                        self._logger.warning("Auto-recovery: deleted stale %s (age=%.0fs)", name, age)
            except Exception as exc:
                self._logger.warning("Auto-recovery: could not delete %s: %s", name, exc)

    def _recover_reset_mt5_state(self) -> None:
        """Reset MT5 connection state so the next cycle reconnects fresh."""
        for name in ["mt5_connection.json", "mt5_state.json"]:
            p = STATE_DIR / name
            try:
                if p.exists():
                    p.unlink(missing_ok=True)
                    self._logger.warning("Auto-recovery: reset %s", name)
            except Exception as exc:
                self._logger.warning("Auto-recovery: could not reset %s: %s", name, exc)


class Supervisor:
    """Orchestrates all OS services and writes system heartbeat."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("supervisor")
        self.sup_cfg = config.get("app", {}).get("supervisor", {})
        self._services: list[ManagedService] = []
        self._loop_states: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self.started_at = utc_now_iso()
        self.version = config.get("app", {}).get("version", "1.0")

    def register(self, service: ManagedService) -> None:
        self._services.append(service)

    def _record_loop_results(self, service_name: str, results: dict[str, str]) -> None:
        now = utc_now_iso()
        for loop_name, status in results.items():
            prev = self._loop_states.get(loop_name, {})
            ok = status in ("OK", "complete")
            self._loop_states[loop_name] = {
                "name": loop_name,
                "status": "ok" if ok else "error",
                "last_run": now,
                "last_result": status,
                "run_count": prev.get("run_count", 0) + 1,
                "service": service_name,
            }
        # Drop renamed/retired loops so stale bots cannot pollute supervisor.json.
        if service_name == "trading_pipeline" and results.get("pipeline") == "complete":
            from core.coordination import STALE_LOOPS

            for stale in STALE_LOOPS:
                self._loop_states.pop(stale, None)
            active = set(results.keys())
            for name in list(self._loop_states):
                if name not in active:
                    self._loop_states.pop(name, None)

    def _collect_system_metrics(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {"cpu_pct": None, "memory_mb": None, "memory_pct": None}
        if psutil is None:
            return metrics
        try:
            proc = psutil.Process()
            metrics["cpu_pct"] = round(psutil.cpu_percent(interval=0.1), 1)
            mem = proc.memory_info()
            metrics["memory_mb"] = round(mem.rss / 1024 / 1024, 1)
            metrics["memory_pct"] = round(proc.memory_percent(), 1)
        except Exception:
            pass
        return metrics

    def _owns_agent_lock(self) -> bool:
        lock = read_json_state("agent_lock.json", default={}) or {}
        lock_pid = int(lock.get("pid") or 0)
        if not lock_pid:
            return True
        return lock_pid == os.getpid()

    def write_heartbeat(self) -> dict[str, Any]:
        """Persist supervisor state for dashboard consumption."""
        if not self._owns_agent_lock():
            return read_json_state("supervisor.json", default={}) or {}
        services = []
        for svc in self._services:
            s = svc.state
            services.append({
                "name": s.name,
                "label": s.label,
                "status": s.status,
                "last_run": s.last_run,
                "last_duration_ms": s.last_duration_ms,
                "run_count": s.run_count,
                "error_count": s.error_count,
                "restart_count": s.restart_count,
                "last_error": s.last_error,
                "interval_seconds": s.interval_seconds,
            })

        overall = "healthy"
        critical = {"trading_pipeline"}
        if any(s["status"] == "error" for s in services if s["name"] in critical):
            overall = "degraded"
        elif all(s["status"] in ("stopped", "idle") for s in services):
            overall = "offline"

        payload = {
            "timestamp": utc_now_iso(),
            "started_at": self.started_at,
            "agent_pid": os.getpid(),
            "version": self.version,
            "os_name": self.config.get("app", {}).get("display_name", "MT5 Quant OS"),
            "overall_status": overall,
            "services": services,
            "loops": list(self._loop_states.values()),
            "system": self._collect_system_metrics(),
            "uptime_seconds": self._uptime_seconds(),
        }
        write_json_state("supervisor.json", payload)
        return payload

    def _uptime_seconds(self) -> int:
        try:
            started = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
            return int((datetime.now(timezone.utc) - started).total_seconds())
        except Exception:
            return 0

    def _heartbeat_loop(self) -> None:
        interval = float(self.sup_cfg.get("heartbeat_seconds", 10))
        while not self._stop.wait(interval):
            try:
                self.write_heartbeat()
            except Exception as exc:
                self.logger.error("Heartbeat write failed: %s", exc)

    def start_all(self) -> None:
        for svc in self._services:
            if svc._on_result is None and svc.state.name == "trading_pipeline":
                svc._on_result = self._record_loop_results
            elif svc._on_result is None:
                pass
            svc.start()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        self._heartbeat_thread.start()
        self.write_heartbeat()

    def stop_all(self) -> None:
        self._stop.set()
        for svc in self._services:
            svc.stop()
        self.write_heartbeat()

    def attach_pipeline_callback(self) -> None:
        for svc in self._services:
            if svc.state.name == "trading_pipeline":
                svc._on_result = self._record_loop_results
