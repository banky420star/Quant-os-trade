#!/usr/bin/env python3
"""Single launcher - trading bot + all dashboard frontends.

Starts in one command:
  - MT5 Quant OS agent (start.py)           -> :8080 main dashboard
  - No-JS dashboard (nojs_dashboard.py)     -> :8081 reliable UI
  - Terminal web view (terminal_view_server)  -> :8083 phone-friendly TUI

Usage (from repo root):
    python scripts/launch_all.py
    python scripts/launch_all.py --profile 30
    python scripts/launch_all.py --no-browser

Stop everything:
    scripts\\kill_agent.bat
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import ensure_dirs, utc_now_iso, write_json_state

LOG_DIR = ROOT / "logs"
KILL_MARKERS = (
    "start.py",
    "nojs_dashboard.py",
    "terminal_view_server.py",
    "run_all.py",
)

DEFAULT_URLS = {
    "main_dashboard": "http://127.0.0.1:8080",
    "nojs_dashboard": "http://127.0.0.1:8081",
    "terminal_web": "http://127.0.0.1:8083",
}


def _kill_windows() -> int:
    killed = 0
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $m = @('start.py','nojs_dashboard.py',"
        "'terminal_view_server.py','run_all.py'); "
        "$cmd = $_.CommandLine; ($m | Where-Object { $cmd -like \"*$_*\" }) } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=ROOT,
        )
        for line in (out.stdout or "").splitlines():
            if line.strip().isdigit():
                killed += 1
    except Exception:
        pass
    return killed


def _kill_posix() -> int:
    killed = 0
    try:
        out = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=10)
        for line in (out.stdout or "").splitlines():
            if not any(marker in line for marker in KILL_MARKERS):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            pid = parts[1]
            if pid.isdigit():
                subprocess.run(["kill", "-9", pid], check=False)
                killed += 1
    except Exception:
        pass
    return killed


def kill_quant_processes() -> int:
    if sys.platform == "win32":
        return _kill_windows()
    return _kill_posix()


def _spawn_detached(name: str, args: list[str], log_name: str) -> dict[str, Any]:
    ensure_dirs()
    log_path = LOG_DIR / log_name
    log_handle = log_path.open("a", encoding="utf-8")
    log_handle.write(f"\n--- {name} started {utc_now_iso()} ---\n")
    log_handle.flush()

    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")

    popen_kwargs: dict[str, Any] = {
        "cwd": ROOT,
        "env": env,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }
    if sys.platform == "win32":
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | create_no_window
        )
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(args, **popen_kwargs)
    return {"name": name, "pid": proc.pid, "log": str(log_path), "args": args}


def _tailscale_hint() -> str | None:
    try:
        from core.utils import load_config
        from core.remote_access import remote_access_info, tailscale_enabled
        config = load_config()
        if not tailscale_enabled(config):
            return None
        info = remote_access_info(8080, config)
        if info.get("tailscale_connected") and info.get("tailscale_ip"):
            return info["tailscale_ip"]
    except Exception:
        pass
    return None


def launch_all(
    *,
    profile: str | None = None,
    open_browser: bool = False,
    wait_seconds: float = 4.0,
) -> dict[str, Any]:
    killed = kill_quant_processes()
    if killed:
        time.sleep(2)

    bot_cmd = [sys.executable, str(ROOT / "start.py")]
    if profile:
        bot_cmd.extend(["--profile", profile])

    services = [
        _spawn_detached("bot", bot_cmd, "launch_bot.log"),
        _spawn_detached(
            "nojs_dashboard",
            [sys.executable, str(ROOT / "scripts" / "nojs_dashboard.py")],
            "launch_nojs.log",
        ),
        _spawn_detached(
            "terminal_web",
            [sys.executable, str(ROOT / "scripts" / "terminal_view_server.py")],
            "launch_terminal_web.log",
        ),
    ]

    time.sleep(wait_seconds)

    urls = dict(DEFAULT_URLS)
    ts_ip = _tailscale_hint()
    if ts_ip:
        urls["main_dashboard_remote"] = f"http://{ts_ip}:8080"
        urls["nojs_dashboard_remote"] = f"http://{ts_ip}:8081"
        urls["terminal_web_remote"] = f"http://{ts_ip}:8083"

    if open_browser:
        webbrowser.open(urls["nojs_dashboard"])

    manifest = {
        "started_at": utc_now_iso(),
        "killed_previous": killed,
        "profile": profile or "auto",
        "services": services,
        "urls": urls,
        "terminal_tui": "python scripts/terminal_view.py",
        "stop_command": "scripts\\kill_agent.bat",
    }
    write_json_state("launch_all.json", manifest)
    return manifest


def _print_summary(manifest: dict[str, Any]) -> None:
    urls = manifest.get("urls") or {}
    print()
    print("  MT5 Quant OS - full stack launched")
    print("  " + "-" * 44)
    for svc in manifest.get("services") or []:
        print(f"  {svc['name']:<18} PID {svc['pid']}  log: {svc['log']}")
    print("  " + "-" * 44)
    print("  Views (open in browser):")
    print(f"    Main dashboard   {urls.get('main_dashboard')}")
    print(f"    No-JS dashboard  {urls.get('nojs_dashboard')}  <- recommended")
    print(f"    Terminal web     {urls.get('terminal_web')}")
    if urls.get("nojs_dashboard_remote"):
        print("  Remote (Tailscale):")
        print(f"    Main dashboard   {urls.get('main_dashboard_remote')}")
        print(f"    No-JS dashboard  {urls.get('nojs_dashboard_remote')}")
        print(f"    Terminal web     {urls.get('terminal_web_remote')}")
    print("  " + "-" * 44)
    print(f"  Terminal TUI (this window): {manifest.get('terminal_tui')}")
    print(f"  Stop everything:          {manifest.get('stop_command')}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch bot + all dashboard views")
    parser.add_argument(
        "--profile",
        default=None,
        help="Force profile (30, 100, growth, live). Omit for auto-detect from MT5.",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open the no-JS dashboard in the browser after launch",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=4.0,
        help="Seconds to wait after spawn before opening browser (default 4)",
    )
    args = parser.parse_args()

    os.chdir(ROOT)
    manifest = launch_all(
        profile=args.profile,
        open_browser=args.open_browser,
        wait_seconds=args.wait,
    )
    _print_summary(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())