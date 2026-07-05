"""Tailscale / phone remote access helpers (opt-in only)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


def tailscale_enabled(config: dict[str, Any] | None = None) -> bool:
    """Tailscale is OFF by default — it spams popups when probed every bot start."""
    env = os.environ.get("MT5_TAILSCALE_ENABLED", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    if config:
        ra = (config.get("app") or {}).get("remote_access") or {}
        return bool(ra.get("enabled"))
    return False


def tailscale_ipv4(config: dict[str, Any] | None = None) -> str | None:
    """Return this machine's Tailscale IPv4, if connected and explicitly enabled."""
    if not tailscale_enabled(config):
        return None
    candidates = [
        Path(r"C:\Program Files\Tailscale\tailscale.exe"),
        Path(r"C:\Program Files (x86)\Tailscale\tailscale.exe"),
    ]
    exe = next((p for p in candidates if p.exists()), None)
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [str(exe), "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
        ip = (proc.stdout or "").strip().splitlines()[0].strip() if proc.returncode == 0 else ""
        return ip or None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


def remote_access_info(
    dashboard_port: int = 8080,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """URLs for phone monitoring — only when app.remote_access.enabled is true."""
    if not tailscale_enabled(config):
        return {"tailscale_connected": False, "enabled": False}
    ip = tailscale_ipv4(config)
    if not ip:
        return {"tailscale_connected": False, "enabled": True}
    return {
        "enabled": True,
        "tailscale_connected": True,
        "tailscale_ip": ip,
        "dashboard_url": f"http://{ip}:{dashboard_port}",
        "rdp_target": ip,
        "phone_steps": [
            "Install Tailscale on iPhone and sign in (same account as this server).",
            f"Open Safari: http://{ip}:{dashboard_port}",
            f"For MT5 terminal: Microsoft Remote Desktop → PC name {ip}",
        ],
    }