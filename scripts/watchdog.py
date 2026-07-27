#!/usr/bin/env python3
"""Process Watchdog — monitors the MT5 Quant OS bot and restarts it if it dies.

Usage:
    python scripts/watchdog.py [--profile growth] [--check-interval 30] [--max-restarts 50]

The watchdog:
1. Reads the PID from state/agent_lock.json
2. Checks if the process is alive every CHECK_INTERVAL seconds
3. If the process is dead, restarts it using start.py --profile <profile>
4. Tracks restart count and backs off after MAX_RESTARTS to prevent infinite loops
5. Writes state/watchdog.json for dashboard consumption
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "mt5_quant_agent" / "state"

# Also check project root state dir (the bot imports from root core/)
STATE_DIR_ALT = ROOT / "state"


def read_json(path: Path, default: dict | None = None) -> dict:
    if default is None:
        default = {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def find_bot_pid() -> int | None:
    """Find the bot PID from agent_lock.json."""
    for d in [STATE_DIR, STATE_DIR_ALT]:
        lock = read_json(d / "agent_lock.json")
        pid = lock.get("pid")
        if pid:
            return int(pid)
    # Fallback: scan running python processes for start.py
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "start.py" in cmdline and "--profile" in cmdline:
                    return proc.info["pid"]
            except Exception:
                continue
    except ImportError:
        pass
    return None


def is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive.

    Uses psutil when available (most reliable on Windows).
    Falls back to os.kill(pid, 0) which can raise PermissionError on Windows.
    """
    try:
        import psutil
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return True  # is_running() or AccessDenied (process exists but we lack perm)
    except psutil.NoSuchProcess:
        return False
    except ImportError:
        pass
    except Exception:
        pass
    except Exception:
        pass
    # Fallback
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def start_bot(profile: str) -> int | None:
    """Start the bot and return its PID."""
    cmd = [sys.executable, "-B", "start.py", "--profile", profile]
    log_path = ROOT / ".freebuff" / "watchdog_restart.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(log_path, "a", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=str(ROOT),
                creationflags=subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            return proc.pid
    except Exception as exc:
        print(f"[WATCHDOG] Failed to start bot: {exc}")
        return None


def run_watchdog(profile: str, check_interval: int, max_restarts: int) -> None:
    """Main watchdog loop."""
    restart_count = 0
    last_restart = None
    consecutive_failures = 0
    watchdog_state_path = STATE_DIR / "watchdog.json"
    watchdog_state_path_alt = STATE_DIR_ALT / "watchdog.json"

    print(f"[WATCHDOG] Starting — profile={profile}, interval={check_interval}s, max_restarts={max_restarts}")

    while True:
        pid = find_bot_pid()
        alive = pid is not None and is_process_alive(pid)

        # Write watchdog state
        state = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bot_pid": pid,
            "bot_alive": alive,
            "restart_count": restart_count,
            "max_restarts": max_restarts,
            "last_restart": last_restart,
            "consecutive_failures": consecutive_failures,
            "profile": profile,
            "status": "watching" if alive else "bot_down",
        }
        write_json(watchdog_state_path, state)
        try:
            write_json(watchdog_state_path_alt, state)
        except Exception:
            pass

        if alive:
            consecutive_failures = 0
            print(f"[WATCHDOG] Bot alive (PID={pid}) — watching")
        else:
            consecutive_failures += 1
            print(f"[WATCHDOG] Bot is DOWN (consecutive={consecutive_failures})")

            if restart_count >= max_restarts:
                print(f"[WATCHDOG] Max restarts ({max_restarts}) reached — NOT restarting. Manual intervention required.")
                state["status"] = "max_restarts_reached"
                write_json(watchdog_state_path, state)
                break

            # Exponential backoff: 5s, 10s, 20s, 40s, 80s, max 120s
            backoff = min(5 * (2 ** min(consecutive_failures - 1, 5)), 120)
            print(f"[WATCHDOG] Waiting {backoff}s before restart (attempt {restart_count + 1}/{max_restarts})")
            time.sleep(backoff)

            # Start the bot
            new_pid = start_bot(profile)
            if new_pid:
                restart_count += 1
                last_restart = datetime.now(timezone.utc).isoformat()
                print(f"[WATCHDOG] Bot restarted — new PID={new_pid}")
                # Wait and check if it survived the first 15 seconds
                time.sleep(15)
                if not is_process_alive(new_pid):
                    consecutive_failures += 1
                    print(f"[WATCHDOG] Bot died within 15s of restart — backing off harder")
            else:
                print("[WATCHDOG] Failed to start bot — will retry")

        time.sleep(check_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="MT5 Quant OS Process Watchdog")
    parser.add_argument("--profile", default="growth", help="Bot profile (default: growth)")
    parser.add_argument("--check-interval", type=int, default=30, help="Seconds between checks (default: 30)")
    parser.add_argument("--max-restarts", type=int, default=50, help="Max restarts before giving up (default: 50)")
    args = parser.parse_args()

    run_watchdog(args.profile, args.check_interval, args.max_restarts)


if __name__ == "__main__":
    main()
