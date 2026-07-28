"""MT5 Quant OS — health monitor.

Polls the bot's /api/health + the dashboard mirror every 5 seconds, writes
PASS/FAIL/WARN lines to logs/health.log, and serves a tiny HTTP endpoint at
http://127.0.0.1:<serve-port>/api/health so the launcher (and the dashboard)
can check liveness cheaply.

Exit on first FAIL of any component after 3 consecutive bad polls (sustained
down = 15s+). Use --no-fail-exit for environments where the bot is expected
to go up and down.

Usage:
  python scripts/health_monitor.py                   # default ports
  python scripts/health_monitor.py --bot-port 8080 --dash-port 8088 --serve-port 8090
  python scripts/health_monitor.py --interval 2      # poll every 2s instead of 5
  python scripts/health_monitor.py --no-fail-exit   # never exit on bad health
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "mt5_quant_agent" / "logs" / "health.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_BOT_PORT = 8080
DEFAULT_DASH_PORT = 8088
DEFAULT_SERVE_PORT = 8090


class HealthState:
    """Thread-safe shared state polled by the HTTP endpoint."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bot: dict = {"status": "unknown", "detail": "", "ts": 0}
        self._dash: dict = {"status": "unknown", "detail": "", "ts": 0}

    def update_bot(self, status: str, detail: str) -> None:
        with self._lock:
            self._bot = {"status": status, "detail": detail, "ts": time.time()}

    def update_dash(self, status: str, detail: str) -> None:
        with self._lock:
            self._dash = {"status": status, "detail": detail, "ts": time.time()}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "bot": dict(self._bot),
                "dash": dict(self._dash),
                "now": time.time(),
            }


def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", errors="replace").decode("ascii") + "\n")


def _http_get(url: str, timeout: float = 1.5) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "health-monitor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(512).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def _log(line: str) -> None:
    """Append a timestamped line to logs/health.log AND print it."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    msg = f"{ts} {line}"
    _safe_print(msg)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def _poll_loop(state: HealthState, bot_port: int, dash_port: int,
               interval: float, fail_exit: bool, exit_evt: threading.Event) -> None:
    """Background poll loop — runs until exit_evt is set or hard fail."""
    consecutive_bad: dict[str, int] = {"bot": 0, "dash": 0}

    while not exit_evt.is_set():
        # Bot
        s, body = _http_get(f"http://127.0.0.1:{bot_port}/api/health", timeout=1.5)
        if s == 200:
            state.update_bot("UP", body[:120])
            consecutive_bad["bot"] = 0
            _log(f"PASS  bot   http://127.0.0.1:{bot_port}/api/health  HTTP {s}")
        else:
            consecutive_bad["bot"] += 1
            state.update_bot("DOWN", body[:120])
            _log(f"FAIL  bot   http://127.0.0.1:{bot_port}/api/health  HTTP {s}  "
                 f"(consecutive: {consecutive_bad['bot']})")
            if fail_exit and consecutive_bad["bot"] >= 3:
                _log("ERROR health monitor: bot DOWN 15s+ sustained, exiting")
                exit_evt.set()
                return

        # Dashboard mirror (also gate fail_exit symmetrically; operator depends on it
        # being reachable for the Freebuff preview tab)
        s, body = _http_get(f"http://127.0.0.1:{dash_port}/", timeout=1.5)
        if s == 200:
            state.update_dash("UP", body[:120])
            consecutive_bad["dash"] = 0
            _log(f"PASS  dash  http://127.0.0.1:{dash_port}/  HTTP {s}")
        else:
            consecutive_bad["dash"] += 1
            state.update_dash("DOWN", body[:120])
            _log(f"FAIL  dash  http://127.0.0.1:{dash_port}/  HTTP {s}  "
                 f"(consecutive: {consecutive_bad['dash']})")
            if fail_exit and consecutive_bad["dash"] >= 3:
                _log("ERROR health monitor: dashboard DOWN 15s+ sustained, exiting")
                exit_evt.set()
                return

        # Sleep with early-exit awareness
        for _ in range(int(interval * 4)):
            if exit_evt.is_set():
                break
            time.sleep(0.25)


class _HealthHandler(BaseHTTPRequestHandler):
    """Serves /api/health and /api/state on the configured serve-port."""

    state_ref: HealthState = None  # set by main()
    started_at: float = 0

    def log_message(self, fmt, *args):  # noqa: A003
        # Quiet the default stderr logging — we have our own _log pipeline.
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        snap = self.state_ref.snapshot() if self.state_ref else {}
        if self.path in ("/", "/api/health"):
            bot_status = snap.get("bot", {}).get("status", "unknown")
            dash_status = snap.get("dash", {}).get("status", "unknown")
            # UP only when the BOT (the source of truth) is up. The dashboard
            # mirror is optional — it's a static-file server, not a service.
            # OR-logic here would mask a dead bot behind a live mirror.
            if bot_status == "UP":
                overall = "UP"
            elif bot_status == "DOWN":
                overall = "DOWN"
            else:
                overall = "STARTING"
            self._send(200, {
                "overall": overall,
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "bot": snap.get("bot", {}),
                "dash": snap.get("dash", {}),
            })
        elif self.path == "/api/state":
            self._send(200, snap)
        else:
            self._send(404, {"error": "not found"})


def main() -> int:
    p = argparse.ArgumentParser(description="MT5 Quant OS - health monitor")
    p.add_argument("--bot-port",   type=int, default=DEFAULT_BOT_PORT)
    p.add_argument("--dash-port",  type=int, default=DEFAULT_DASH_PORT)
    p.add_argument("--serve-port", type=int, default=DEFAULT_SERVE_PORT)
    p.add_argument("--interval",    type=float, default=5.0)
    p.add_argument("--no-fail-exit", dest="fail_exit", action="store_false")
    args = p.parse_args()

    state = HealthState()
    exit_evt = threading.Event()

    # HTTP server
    _HealthHandler.state_ref = state
    _HealthHandler.started_at = time.time()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.serve_port), _HealthHandler)
    http_thread = threading.Thread(target=httpd.serve_forever,
                                   name="health-http", daemon=True)
    http_thread.start()
    _log(f"health monitor listening on :{args.serve_port} (poll={args.interval}s)")

    # Polling thread
    poll_thread = threading.Thread(
        target=_poll_loop,
        args=(state, args.bot_port, args.dash_port,
              args.interval, args.fail_exit, exit_evt),
        name="health-poll",
        daemon=True,
    )
    poll_thread.start()

    try:
        while not exit_evt.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        _log("health monitor: KeyboardInterrupt - shutting down")
        exit_evt.set()

    httpd.shutdown()
    _log("health monitor stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
