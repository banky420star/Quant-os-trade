"""Heartbeat Loop — writes state/heartbeat.json with service status, uptime, and memory.

Supervisor-managed (Tier 2). Mirrors what the Supervisor's internal heartbeat does
but surfaces as a visible loop tile on the dashboard. Runs on a configurable
interval (default 15s) via the ManagedService thread.

Output: state/heartbeat.json
  {
    "timestamp": "2026-07-29T12:00:00Z",
    "started_at": "2026-07-29T11:00:00Z",
    "status": "ok",
    "run_count": 42,
    "uptime_seconds": 3600,
    "memory_mb": 85.2,
    "memory_pct": 2.1,
    "cpu_pct": 3.5,
    "interval_seconds": 15,
  }
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datetime import datetime, timezone
from typing import Any

from core.utils import (
    ensure_dirs,
    load_config,
    read_json_state,
    setup_logger,
    utc_now_iso,
    write_json_state,
)

_STARTED_AT: str | None = None  # set on first run (survives across cycles)

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore


def _collect_metrics() -> dict[str, Any]:
    """Collect system metrics without importing outside the loop."""
    metrics: dict[str, Any] = {
        "memory_mb": None,
        "memory_pct": None,
        "cpu_pct": None,
    }
    if psutil is None:
        return metrics
    try:
        proc = psutil.Process()
        mem = proc.memory_info()
        metrics["memory_mb"] = round(mem.rss / 1024 / 1024, 1)
        metrics["memory_pct"] = round(proc.memory_percent(), 1)
        metrics["cpu_pct"] = round(psutil.cpu_percent(interval=0.1), 1)
    except Exception:
        pass
    return metrics


def _uptime_seconds() -> int:
    """Return seconds since this loop first ran."""
    global _STARTED_AT
    if _STARTED_AT is None:
        return 0
    try:
        started = datetime.fromisoformat(_STARTED_AT.replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - started).total_seconds())
    except Exception:
        return 0


def run(config: dict | None = None) -> dict:
    """Write state/heartbeat.json with service status, uptime, and memory."""
    global _STARTED_AT

    ensure_dirs()
    cfg = config or load_config()
    logger = setup_logger("heartbeat_loop", "heartbeat_loop.log")
    logger.info("=== Heartbeat Loop starting ===")

    hb_cfg = (cfg.get("heartbeat_loop") or {})
    if not hb_cfg.get("enabled", True):
        logger.info("Heartbeat loop disabled")
        return {"status": "disabled"}

    interval = float(hb_cfg.get("loop_interval_seconds", 15))

    # Track uptime from first cycle
    if _STARTED_AT is None:
        _STARTED_AT = utc_now_iso()

    # Read previous run count from the last heartbeat (if any)
    previous = read_json_state("heartbeat.json", default=None)
    run_count = 1
    if isinstance(previous, dict):
        run_count = int(previous.get("run_count", 0)) + 1

    metrics = _collect_metrics()
    uptime = _uptime_seconds()
    hb = {
        "timestamp": utc_now_iso(),
        "started_at": _STARTED_AT,
        "status": "ok",
        "run_count": run_count,
        "uptime_seconds": uptime,
        "interval_seconds": interval,
    }
    hb.update(metrics)

    write_json_state("heartbeat.json", hb)

    logger.info(
        "Heartbeat: run=%d uptime=%ds mem=%.1fMB",
        run_count, uptime,
        metrics.get("memory_mb") or 0,
    )
    logger.info("=== Heartbeat Loop complete ===")
    return hb


if __name__ == "__main__":
    run()
