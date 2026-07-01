"""Tailscale / phone remote access helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def tailscale_ipv4() -> str | None:
    """Return this machine's Tailscale IPv4, if connected."""
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
        )
        ip = (proc.stdout or "").strip().splitlines()[0].strip() if proc.returncode == 0 else ""
        return ip or None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


def remote_access_info(dashboard_port: int = 8080) -> dict[str, Any]:
    """URLs for phone monitoring (Tailscale must be connected on both devices)."""
    ip = tailscale_ipv4()
    if not ip:
        return {"tailscale_connected": False}
    return {
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