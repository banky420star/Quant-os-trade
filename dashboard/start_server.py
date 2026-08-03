"""Detached launcher for the stdlib dashboard server.

The launcher starts dashboard/server.py with stdin detached, redirects output to a
log file, waits for the HTTP endpoint to answer, and writes the child PID. It is
safe to invoke from a Windows batch shell without inheriting the console.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent


def _healthy(host: str, port: int) -> bool:
    try:
        with urlopen(f"http://{host}:{port}/api/health", timeout=1.5) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the dashboard detached")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--log", default=".freebuff/preview-dashboard.log")
    parser.add_argument("--pid-file", default=".freebuff/dashboard.pid")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    log_path = ROOT / args.log
    pid_path = ROOT / args.pid_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    log_handle = log_path.open("a", encoding="utf-8")
    log_handle.write(f"\n--- dashboard launch {time.strftime('%Y-%m-%dT%H:%M:%S')} ---\n")
    log_handle.flush()

    cmd = [
        sys.executable,
        str(ROOT / "dashboard" / "server.py"),
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    popen_kwargs: dict[str, object] = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "env": {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True

    child = subprocess.Popen(cmd, **popen_kwargs)
    pid_path.write_text(json.dumps({"pid": child.pid, "port": args.port}), encoding="utf-8")
    log_handle.close()

    deadline = time.monotonic() + max(1.0, args.timeout)
    while time.monotonic() < deadline:
        if child.poll() is not None:
            print(f"Dashboard exited early (code={child.returncode}); see {log_path}", file=sys.stderr)
            return 1
        if _healthy(args.host, args.port):
            print(f"Dashboard ready at http://{args.host}:{args.port}/ (pid {child.pid})")
            return 0
        time.sleep(0.25)

    print(f"Dashboard did not answer within {args.timeout:g}s; see {log_path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
