#!/usr/bin/env python
"""Process watchdog — monitors the bot PID and auto-restarts on crash.

Usage:
    python scripts/watchdog.py [--bot-pid-file state/bot.pid] [--check-interval 15] [--max-restarts-per-hour 5] [--bot-args "--profile growth"]

Features:
    - Reads bot PID from state/bot.pid (updated by start.py)
    - Checks if the process is alive every --check-interval seconds
    - Auto-restarts the bot if the process dies
    - Caps restarts at --max-restarts-per-hour to prevent restart storms
    - Logs every restart to state/restart_log.jsonl with timestamp and reason
    - Writes its own PID to state/watchdog.pid for nested monitoring
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
LOGS_DIR = ROOT / "logs"
DEFAULT_PID_FILE = STATE_DIR / "bot.pid"
DEFAULT_WATCHDOG_PID_FILE = STATE_DIR / "watchdog.pid"
DEFAULT_RESTART_LOG = STATE_DIR / "restart_log.jsonl"
DEFAULT_MAX_RESTARTS_PER_HOUR = 5
DEFAULT_CHECK_INTERVAL = 15  # seconds
DEFAULT_BOT_ARGS = "--profile growth"
SLIDING_WINDOW_SECONDS = 3600  # 1 hour


def _ensure_dirs() -> None:
    for d in (STATE_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
        except Exception:
            return True  # assume alive if we can't check

    # Fallback: Windows tasklist check
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def _read_bot_pid(pid_file: Path) -> int | None:
    """Read the bot PID from the pid file."""
    if not pid_file.exists():
        return None
    try:
        data = json.loads(pid_file.read_text(encoding="utf-8"))
        return int(data.get("pid") or 0) or None
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _count_recent_restarts(restart_log: Path) -> int:
    """Count how many restarts have happened in the last SLIDING_WINDOW_SECONDS."""
    if not restart_log.exists():
        return 0
    now = time.time()
    count = 0
    try:
        with restart_log.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    ts_str = record.get("timestamp", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        age = now - ts.timestamp()
                        if 0 <= age <= SLIDING_WINDOW_SECONDS:
                            count += 1
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return 0
    return count


def _log_restart(restart_log: Path, bot_pid: int | None, reason: str) -> None:
    """Append a restart event to the JSONL restart log."""
    record = {
        "timestamp": _utc_now_iso(),
        "event": "bot_restart",
        "reason": reason,
        "previous_bot_pid": bot_pid,
    }
    try:
        with restart_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()
    except OSError:
        pass


def _find_existing_bot_processes() -> list[int]:
    """Return PIDs of any existing 'python.*start.py' processes."""
    pids: list[int] = []
    try:
        result = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
        for line in result.stdout.splitlines():
            if "start.py" in line:
                parts = line.rsplit(",", 1)
                if len(parts) == 2:
                    try:
                        pids.append(int(parts[1].strip()))
                    except ValueError:
                        pass
    except Exception:
        pass
    return pids


def _start_bot(bot_args: str, logger: logging.Logger) -> int | None:
    """Start the bot process and return its PID."""
    # Guard: don't start a second bot if one is already running.
    existing = _find_existing_bot_processes()
    if existing:
        logger.warning("Bot already running (PIDs: %s) — not starting a duplicate", existing)
        return existing[0]

    cmd = [sys.executable, "-B", str(ROOT / "start.py")] + bot_args.split()
    logger.info("Starting bot: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=(
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            ),
        )
        return proc.pid
    except Exception as exc:
        logger.error("Failed to start bot: %s", exc)
        return None


def _write_watchdog_pid(pid_file: Path) -> None:
    """Write the watchdog's own PID so it can be monitored."""
    try:
        pid_file.write_text(
            json.dumps({"pid": os.getpid(), "started_at": _utc_now_iso()}),
            encoding="utf-8",
        )
    except OSError:
        pass


def _remove_watchdog_pid(pid_file: Path) -> None:
    try:
        pid_file.unlink(missing_ok=True)
    except OSError:
        pass


def run_watchdog(
    bot_pid_file: Path = DEFAULT_PID_FILE,
    check_interval: int = DEFAULT_CHECK_INTERVAL,
    max_restarts_per_hour: int = DEFAULT_MAX_RESTARTS_PER_HOUR,
    bot_args: str = DEFAULT_BOT_ARGS,
    *,
    daemon: bool = False,
) -> None:
    """Main watchdog loop. Never returns unless daemon=False and bot stays alive."""
    _ensure_dirs()
    restart_log = STATE_DIR / "restart_log.jsonl"
    watchdog_pid_file = STATE_DIR / "watchdog.pid"
    _write_watchdog_pid(watchdog_pid_file)

    logger = logging.getLogger("watchdog")
    logger.setLevel(logging.INFO)
    log_file = LOGS_DIR / "watchdog.log"
    handler = logging.FileHandler(str(log_file), encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(handler.formatter)
    logger.addHandler(console)

    logger.info("=== Watchdog started (pid=%d, interval=%ds, max_restarts=%d/h) ===",
                os.getpid(), check_interval, max_restarts_per_hour)

    try:
        while True:
            bot_pid = _read_bot_pid(bot_pid_file)

            if bot_pid is None:
                logger.warning("No bot.pid found — starting bot")
                new_pid = _start_bot(bot_args, logger)
                if new_pid:
                    _log_restart(restart_log, None, "no_pid_file")
                    logger.info("Bot started with PID %d", new_pid)
                    # Give the bot time to write its own pid file.
                    time.sleep(check_interval)
                else:
                    logger.error("Failed to start bot — retrying in %ds", check_interval)
                    time.sleep(check_interval)
                continue

            if _is_process_alive(bot_pid):
                # Bot is alive — silent check, log only on transition.
                time.sleep(check_interval)
                continue

            # Bot is dead — restart it.
            recent = _count_recent_restarts(restart_log)
            if recent >= max_restarts_per_hour:
                logger.error(
                    "Max restarts reached (%d in the last hour). Giving up. "
                    "Check bot logs for the root cause.",
                    recent,
                )
                break

            logger.warning("Bot PID %d has died — restarting (attempt %d/%d this hour)",
                           bot_pid, recent + 1, max_restarts_per_hour)
            _log_restart(restart_log, bot_pid, "process_died")

            # Exponential backoff for repeated failures
            backoff = min(5 * (2 ** recent), 300)  # Cap at 5 minutes
            if recent > 0:
                logger.info("Backing off %ds before restart", backoff)
                time.sleep(backoff)

            new_pid = _start_bot(bot_args, logger)
            if new_pid:
                logger.info("Bot restarted with PID %d", new_pid)
            else:
                logger.error("Failed to restart bot")

            time.sleep(check_interval)

    except KeyboardInterrupt:
        logger.info("Watchdog stopped by user")
    finally:
        _remove_watchdog_pid(watchdog_pid_file)
        logger.info("=== Watchdog stopped ===")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Process watchdog for MT5 Quant OS bot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/watchdog.py
  python scripts/watchdog.py --check-interval 30 --max-restarts-per-hour 3
  python scripts/watchdog.py --bot-args "--profile live"
        """,
    )
    parser.add_argument(
        "--bot-pid-file",
        default=str(DEFAULT_PID_FILE),
        help=f"Path to bot PID file (default: {DEFAULT_PID_FILE})",
    )
    parser.add_argument(
        "--check-interval",
        type=int,
        default=DEFAULT_CHECK_INTERVAL,
        help=f"Seconds between health checks (default: {DEFAULT_CHECK_INTERVAL})",
    )
    parser.add_argument(
        "--max-restarts-per-hour",
        type=int,
        default=DEFAULT_MAX_RESTARTS_PER_HOUR,
        help=f"Max restarts per hour before giving up (default: {DEFAULT_MAX_RESTARTS_PER_HOUR})",
    )
    parser.add_argument(
        "--bot-args",
        default=DEFAULT_BOT_ARGS,
        help=f"Arguments to pass to start.py (default: '{DEFAULT_BOT_ARGS}')",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run silently (no stdout logging beyond startup)",
    )
    args = parser.parse_args()

    run_watchdog(
        bot_pid_file=Path(args.bot_pid_file),
        check_interval=args.check_interval,
        max_restarts_per_hour=args.max_restarts_per_hour,
        bot_args=args.bot_args,
        daemon=args.daemon,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
