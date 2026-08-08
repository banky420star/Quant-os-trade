"""Supervisor — manages trading services with auto-restart and health monitoring."""

from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from core.utils import read_json_state, utc_now_iso, write_json_state

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore


# Phase 0 safety: services that can mutate the broker. When any of these
# fail 3+ consecutive times, the supervisor triggers an emergency halt.
CRITICAL_SERVICES: set[str] = {"trading_pipeline", "fast_mode"}
MAX_CONSECUTIVE_FAILURES = 3


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
    consecutive_failures: int = 0


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
                self.state.consecutive_failures = 0
            except Exception as exc:
                self.state.error_count += 1
                self.state.status = "error"
                self.state.last_error = str(exc)
                self.state.restart_count += 1
                self.state.consecutive_failures += 1
                self._logger.error("Service %s failed: %s", self.state.name, exc)
                self._logger.error(traceback.format_exc())
                # Phase 0 safety: critical service failure cascade → emergency halt.
                if (
                    self.state.name in CRITICAL_SERVICES
                    and self.state.consecutive_failures >= MAX_CONSECUTIVE_FAILURES
                ):
                    self._logger.critical(
                        "EMERGENCY HALT: %s failed %d consecutive times. "
                        "Shutting down to prevent unsafe operation.",
                        self.state.name,
                        self.state.consecutive_failures,
                    )
                    os._exit(1)
            finally:
                self.state.last_run = utc_now_iso()
                self.state.last_duration_ms = round((time.monotonic() - t0) * 1000, 1)
                if self._on_result and loop_results:
                    self._on_result(self.state.name, loop_results)

            if self._run_once:
                self.state.status = "stopped"
                break

            sleep_for = max(1.0, self.state.interval_seconds - self.state.last_duration_ms / 1000)
            if self._stop.wait(sleep_for):
                break

        self.state.status = "stopped"


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
            # Loops owned by dedicated services (not the pipeline) must not be
            # dropped by the pipeline's own-loop cleanup — e.g. m1_structure_loop
            # runs as its own supervisor service on a fast cadence.
            EXTERNAL_SERVICE_LOOPS = {"m1_structure_loop"}
            for name in list(self._loop_states):
                if name not in active and name not in EXTERNAL_SERVICE_LOOPS:
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
