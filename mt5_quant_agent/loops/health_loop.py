"""Health Loop — run health checks every cycle, write state/health.json."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.health_monitor import HealthMonitor
from core.mt5_connection_manager import MT5ConnectionManager
from core.utils import ensure_dirs, load_config, setup_logger


def run(connect: bool = False) -> dict:
    ensure_dirs()
    config = load_config()
    logger = setup_logger("health_loop", "health_loop.log")
    logger.info("=== Health Loop starting ===")

    connection = None
    if connect:
        connection = MT5ConnectionManager(config, logger)
        try:
            connection.connect()
        except ConnectionError as exc:
            logger.warning("Health connect failed (offline checks only): %s", exc)

    monitor = HealthMonitor(config, connection, logger=logger)
    health = monitor.write_health()
    if health.get("status") != "healthy" and health.get("issues"):
        from core.ops_alerts import alert_health_degraded

        alert_health_degraded(config, list(health.get("issues") or []))
    logger.info("Health status: %s issues=%s", health["status"], health["issues"])
    logger.info("=== Health Loop complete ===")

    if connection:
        connection.disconnect()
    return health


if __name__ == "__main__":
    run(connect=True)