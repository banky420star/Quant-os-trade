"""Detached Windows launcher for the MT5 Quant OS dashboard server.

Usage:
    python dashboard/start_server.py [--port 8082] [--log .freebuff/dashboard.log]

Why this wrapper is needed:
- ``start /B python dashboard/server.py`` on Windows keeps the parent cmd.exe
  waiting because the child inherits the console.
- ``nohup``/``&`` under bash-on-Windows can also hang for the same reason.
- This script uses ``subprocess.DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``
  and redirects stdin/stdout/stderr so the parent shell returns immediately
  and the server keeps running after the shell exits.
- After launch it polls ``/api/health`` until the server is ready (or a timeout
  is reached), writes the PID to ``.freebuff/dashboard.pid`` automatically,
  and reports success/failure — no manual PID tracking required.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_PORT = 8082
DEFAULT_HEALTH_TIMEOUT = 15.0
DEFAULT_HEALTH_INTERVAL = 0.5


def _is_healthy(port: int, timeout: float = 2.0) -> dict | None:
    """Return the parsed /api/health JSON if the dashboard is healthy, else None."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=timeout
        ) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "ok":
                return data
            return None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def _write_pid(pid: int) -> None:
    """Persist the child PID for orchestrators and preview registration."""
    pid_path = ROOT / ".freebuff" / "dashboard.pid"
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(pid), encoding="utf-8")
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Detached dashboard server launcher")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port (default 8082)")
    parser.add_argument("--host", default=None, help="bind host (default 0.0.0.0)")
    parser.add_argument(
        "--log",
        default=str(ROOT / ".freebuff" / "dashboard.log"),
        help="path to log file (default .freebuff/dashboard.log)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_HEALTH_TIMEOUT,
        help=f"seconds to wait for /api/health (default {DEFAULT_HEALTH_TIMEOUT})",
    )
    args = parser.parse_args()

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # If a healthy dashboard is already running on this port, reuse it.
    existing = _is_healthy(args.port)
    if existing:
        existing_pid = existing.get("pid")
        if existing_pid:
            _write_pid(existing_pid)
        print(
            f"Dashboard already healthy on port {args.port} "
            f"(PID {existing_pid or 'unknown'})."
        )
        return

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")

    cmd = [sys.executable, str(ROOT / "dashboard" / "server.py")]
    if args.host:
        cmd.extend(["--host", args.host])
    cmd.extend(["--port", str(args.port)])

    # Open log in append mode and hand it to the child as stdout/stderr.
    # stdin is DEVNULL so the child cannot hold the parent console.
    # Open the log file without `with` so the handle stays alive while the
    # child inherits it.  The parent exits immediately after launch, at
    # which point the handle is closed; the child keeps its duplicated copy.
    log_file = open(log_path, "a", encoding="utf-8")
    popen_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "cwd": str(ROOT),
        "env": env,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    popen = subprocess.Popen(cmd, **popen_kwargs)

    # Wait until the health endpoint responds or we time out.  This eliminates
    # the race where callers (or preview registration) hit the server before it
    # has finished binding/importing.
    deadline = time.time() + args.timeout
    healthy = False
    last_error: Optional[str] = None
    while time.time() < deadline:
        if popen.poll() is not None:
            last_error = f"server exited early (code {popen.returncode})"
            break
        if _is_healthy(args.port, timeout=min(DEFAULT_HEALTH_INTERVAL, 1.0)):
            healthy = True
            break
        time.sleep(DEFAULT_HEALTH_INTERVAL)

    if not healthy:
        try:
            log_file.close()
        except Exception:
            pass
        # Best-effort cleanup of a half-started child.  Give it a brief grace
        # period for a clean shutdown, then force-kill if still alive.
        try:
            popen.terminate()
        except Exception:
            pass
        try:
            popen.wait(timeout=2.0)
        except Exception:
            try:
                popen.kill()
            except Exception:
                pass
        print(
            f"Dashboard server failed to become healthy on port {args.port}. "
            f"{last_error or f'timeout after {args.timeout}s'}"
        )
        print(f"See log: {log_path}")
        sys.exit(1)

    # Health confirmed — persist PID, close parent's log handle, and exit.
    try:
        log_file.close()
    except Exception:
        pass
    _write_pid(popen.pid)

    print(
        f"Dashboard server started (PID {popen.pid}) on port {args.port}. "
        f"Health check passed. Log: {log_path}"
    )


if __name__ == "__main__":
    main()
