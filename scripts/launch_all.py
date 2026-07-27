"""MT5 Quant OS — full-stack launcher.

Replaces the broken `LAUNCH.bat` (which referenced a missing script of the
same name). Spawns:
  - bot (start.py --profile <profile>)           -> :8080 main dashboard
  - dashboard mirror (http.server on dashboard/) -> :8088 Freebuff preview
  - TUI (terminal_view.py)                       -> separate console window
  - health monitor (health_monitor.py)           -> :8090

Each child is detached and logs to `logs/<component>.log`. Idempotent:
existing services on each port are detected and left alone.

Usage:
  python scripts/launch_all.py                 # default growth profile
  python scripts/launch_all.py --profile 30   # $30 demo
  python scripts/launch_all.py --profile 100  # $100 real
  python scripts/launch_all.py --profile 30-real
  python scripts/launch_all.py --no-tui       # skip the TUI status board
  python scripts/launch_all.py --browser      # open browser to mirror
  python scripts/launch_all.py --wait         # print status every 2s after launch
  python scripts/launch_all.py --status       # only print status, no spawn
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
LOGS = ROOT / "mt5_quant_agent" / "logs"
LOGS.mkdir(parents=True, exist_ok=True)

BOT_PORT = 8080
DASH_MIRROR_PORT = 8088
HEALTH_PORT = 8090

PROFILES = ("30", "100", "growth", "30-real", "30-c2", "30-c3", "auto")


def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", errors="replace").decode("ascii") + "\n")


def _is_port_open(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _http_get(url: str, timeout: float = 1.5) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "launcher/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(200).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def _spawn(label: str, cmd: list[str], log_name: str | None = None) -> subprocess.Popen:
    """Spawn a detached child process; logs to logs/<label>.log."""
    log_path = LOGS / (log_name or f"{label}.log")
    log_file = open(log_path, "ab", buffering=0)
    creationflags = 0
    if sys.platform == "win32":
        # DETACHED so the child outlives the launcher console window
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
    _safe_print(f"  spawn[{label:<11}] pid={proc.pid:<6} log={log_path.name}")
    return proc


def _check(label: str, host: str, port: int, path: str = "/api/health") -> str:
    if not _is_port_open(host, port):
        return f"  {label:<14} DOWN   ({host}:{port})"
    status, _ = _http_get(f"http://{host}:{port}{path}", timeout=1.5)
    if status == 200:
        return f"  {label:<14} UP     ({host}:{port})"
    return f"  {label:<14} ERR    ({host}:{port} -> HTTP {status})"


def _start_bot(profile: str) -> bool:
    """Launch `start.py --profile <profile>` if port 8080 is free."""
    if _is_port_open("127.0.0.1", BOT_PORT):
        _safe_print(f"  bot already listening on :{BOT_PORT} - leaving it alone")
        return True

    _safe_print(f"  launching bot: profile={profile}")
    proc = _spawn("bot", [sys.executable, "start.py", "--profile", profile], "bot.log")
    for _ in range(20):
        time.sleep(0.5)
        if _is_port_open("127.0.0.1", BOT_PORT):
            _safe_print(f"  bot online pid={proc.pid} :{BOT_PORT}")
            return True
    _safe_print(f"  WARNING bot did not bind :{BOT_PORT} within 10s - see logs/bot.log")
    return False


def _start_dashboard_mirror() -> bool:
    """Serve the static dashboard on port 8088 for the Freebuff Preview tab.

    The bot's own /api/state (port 8080) is what the dashboard fetches for
    live data, so the URL inside index.html stays pointed at the bot
    regardless of where the mirror runs.
    """
    if _is_port_open("127.0.0.1", DASH_MIRROR_PORT):
        _safe_print(f"  dashboard mirror already listening on :{DASH_MIRROR_PORT}")
        return True

    _safe_print(f"  launching dashboard mirror on :{DASH_MIRROR_PORT}")
    proc = _spawn(
        "dashmirror",
        [
            sys.executable, "-m", "http.server",
            str(DASH_MIRROR_PORT), "--bind", "127.0.0.1",
        ],
        "dashmirror.log",
    )
    for _ in range(20):
        time.sleep(0.25)
        if _is_port_open("127.0.0.1", DASH_MIRROR_PORT):
            _safe_print(f"  dashboard mirror online pid={proc.pid} :{DASH_MIRROR_PORT}")
            return True
    _safe_print(f"  WARNING dashboard mirror did not bind :{DASH_MIRROR_PORT}")
    return False


def _start_health_monitor() -> bool:
    if _is_port_open("127.0.0.1", HEALTH_PORT):
        _safe_print(f"  health monitor already listening on :{HEALTH_PORT}")
        return True
    _safe_print(f"  launching health monitor on :{HEALTH_PORT}")
    cmd = [
        sys.executable, str(SCRIPTS / "health_monitor.py"),
        "--bot-port", str(BOT_PORT),
        "--dash-port", str(DASH_MIRROR_PORT),
        "--serve-port", str(HEALTH_PORT),
    ]
    proc = _spawn("health", cmd, "health.log")
    for _ in range(20):
        time.sleep(0.25)
        if _is_port_open("127.0.0.1", HEALTH_PORT):
            _safe_print(f"  health monitor online pid={proc.pid} :{HEALTH_PORT}")
            return True
    _safe_print(f"  WARNING health monitor did not bind :{HEALTH_PORT}")
    return False


def _start_tui() -> bool:
    """Open terminal_view.py in a new console window."""
    tui_script = SCRIPTS / "terminal_view.py"
    if not tui_script.exists():
        _safe_print("  TUI script missing (scripts/terminal_view.py) - skipping")
        return False

    if sys.platform == "win32":
        _safe_print("  opening TUI in new window")
        subprocess.Popen(
            [
                "cmd", "/c", "start", "MT5 Quant OS - TUI",
                sys.executable, str(tui_script),
                "--bot-url", f"http://127.0.0.1:{BOT_PORT}",
            ],
            creationflags=0x00000008,  # DETACHED
            close_fds=True,
        )
    else:
        subprocess.Popen(
            [sys.executable, str(tui_script),
             "--bot-url", f"http://127.0.0.1:{BOT_PORT}"],
        )
    return True


# --------------------------------------------------------------------- commands

def cmd_status(_args) -> int:
    _safe_print("\n  MT5 Quant OS - component health")
    _safe_print("  " + "-" * 50)
    print(_check("bot",         "127.0.0.1", BOT_PORT, "/api/health"))
    print(_check("bot/api",     "127.0.0.1", BOT_PORT, "/api/state"))
    print(_check("dash-mirror", "127.0.0.1", DASH_MIRROR_PORT, "/"))
    print(_check("health",      "127.0.0.1", HEALTH_PORT, "/api/health"))
    _safe_print("")
    return 0


def cmd_launch(args) -> int:
    profile = args.profile if args.profile in PROFILES else "growth"
    banner = (
        "\n  +-------------------------------------------------------+\n"
        "  |   MT5 QUANT OS - FULL LAUNCH                          |\n"
        f"  |   profile={profile:<9}  freebuff=127.0.0.1:{DASH_MIRROR_PORT}      |\n"
        "  +-------------------------------------------------------+\n"
    )
    _safe_print(banner)

    t0 = time.monotonic()
    bot_ok    = _start_bot(profile)
    mirror_ok = _start_dashboard_mirror()
    health_ok = _start_health_monitor()
    tui_ok    = _start_tui() if args.tui else False
    elapsed   = time.monotonic() - t0

    if args.browser and mirror_ok:
        webbrowser.open(f"http://127.0.0.1:{DASH_MIRROR_PORT}/")

    _safe_print("")
    _safe_print(f"  launch took {elapsed:.1f}s")
    _safe_print("  " + "-" * 50)
    print(_check("bot",         "127.0.0.1", BOT_PORT, "/api/health"))
    print(_check("dash-mirror", "127.0.0.1", DASH_MIRROR_PORT, "/"))
    print(_check("health",      "127.0.0.1", HEALTH_PORT, "/api/health"))
    _safe_print(f"  tui          {'OK (separate window)' if tui_ok else 'skipped' if not args.tui else 'FAILED'}")
    _safe_print("")
    _safe_print("  Tips:")
    _safe_print(f"    python scripts/launch_all.py --status   # re-check health")
    _safe_print(f"    python scripts/launch_all.py --wait     # tail-bot mode (Ctrl+C to exit)")
    _safe_print(f"    scripts\\kill_agent.bat                 # stop everything")
    _safe_print("")

    if args.wait:
        _safe_print("  --wait mode: status every 2s (Ctrl+C to exit)")
        try:
            while True:
                time.sleep(2.0)
                cmd_status(args)
        except KeyboardInterrupt:
            _safe_print("\n  exiting wait mode")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="MT5 Quant OS - full-stack launcher")
    p.add_argument("--profile", default="growth",
                   help="Bot profile (30, 100, growth, 30-real, etc.)")
    p.add_argument("--no-tui", dest="tui", action="store_false",
                   help="Skip the TUI status board window")
    p.add_argument("--browser", action="store_true",
                   help="Open browser to mirror dashboard after launch")
    p.add_argument("--wait", action="store_true",
                   help="After launch, sleep and periodically re-print status")
    p.add_argument("--status", action="store_true",
                   help="Just print status of all components, no spawn")
    args = p.parse_args()

    if args.status:
        return cmd_status(args)
    return cmd_launch(args)


if __name__ == "__main__":
    sys.exit(main())
