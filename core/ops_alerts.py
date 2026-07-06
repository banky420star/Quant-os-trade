"""Operational alerts — webhook notifications for kill switch, health, execution."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger("ops_alerts")

_last_sent: dict[str, str] = {}


def _alerts_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return (config.get("ops") or {}).get("alerts") or {}


def alerts_enabled(config: dict[str, Any]) -> bool:
    cfg = _alerts_cfg(config)
    return bool(cfg.get("enabled")) and bool(str(cfg.get("webhook_url") or "").strip())


def _should_send(key: str, message: str, cooldown_sec: float) -> bool:
    global _last_sent
    if _last_sent.get(key) == message:
        return False
    _last_sent[key] = message
    if len(_last_sent) > 200:
        _last_sent.clear()
    return True


def send_alert(
    config: dict[str, Any],
    *,
    title: str,
    message: str,
    level: str = "warning",
    dedupe_key: str | None = None,
) -> bool:
    """POST a compact JSON payload to the configured webhook (Discord-compatible)."""
    if not alerts_enabled(config):
        return False
    cfg = _alerts_cfg(config)
    key = dedupe_key or title
    if not _should_send(key, message, float(cfg.get("cooldown_sec", 300))):
        return False

    url = str(cfg["webhook_url"]).strip()
    body = {
        "content": f"**[{level.upper()}] {title}**\n{message}",
        "username": cfg.get("username", "MT5 Quant OS"),
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Alert webhook failed: %s", exc)
        return False


def alert_kill_switch(config: dict[str, Any], reason: str) -> None:
    cfg = _alerts_cfg(config)
    if not cfg.get("on_kill_switch", True):
        return
    send_alert(config, title="Kill switch ON", message=reason, level="critical", dedupe_key="kill_switch")


def alert_health_degraded(config: dict[str, Any], issues: list[str]) -> None:
    cfg = _alerts_cfg(config)
    if not cfg.get("on_health_degraded", True) or not issues:
        return
    send_alert(
        config,
        title="Health degraded",
        message="; ".join(issues[:5]),
        level="warning",
        dedupe_key="health:" + "|".join(issues[:3]),
    )


def alert_execution_error(config: dict[str, Any], symbol: str, error: str) -> None:
    cfg = _alerts_cfg(config)
    if not cfg.get("on_execution_error", True):
        return
    send_alert(
        config,
        title=f"Execution failed — {symbol}",
        message=error,
        level="error",
        dedupe_key=f"exec:{symbol}:{error[:80]}",
    )