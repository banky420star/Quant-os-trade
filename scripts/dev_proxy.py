"""Reverse-proxy for the Freebuff preview tab.

Serves the dashboard/ folder as static files AND forwards /api/* traffic to
the bot's API server on a configurable port. SSE-safe (streams upstream
chunks downstream). One process, no external deps.

Usage:
    python scripts/dev_proxy.py                # default upstream :8080
    python scripts/dev_proxy.py --upstream :8081
    python scripts/dev_proxy.py --port 8088
    python scripts/dev_proxy.py --directory dashboard

Stops cleanly on Ctrl+C / SIGTERM.
"""
from __future__ import annotations

import argparse
import contextlib
import http.client
import http.server
import json
import socketserver
import sys
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DASH = ROOT / "dashboard"
DEFAULT_UPSTREAM = "127.0.0.1:8080"
DEFAULT_PORT = 8088

# ---- /api/render_metrics local storage ----
# Append-only JSONL of every render-marker beacon fired by the dashboard.
# Read on GET to surface per-tab last_render_ms + recent activity in the
# dashboard tile. Background-driven path so no upstream round-trip cost.
RENDER_METRICS_PATH = ROOT / "state" / "render_metrics.jsonl"
RENDER_METRICS_MAX_ROWS = 500  # trim the file every N appends
_render_metrics_lock = threading.Lock()


class _ProxyHandler(http.server.SimpleHTTPRequestHandler):
    """Serve directory; forward /api/* to upstream bot; SSE-safe streaming."""

    def __init__(self, *args, upstream=None, **kwargs):
        # Store upstream on the instance, not the class — mutating the class
        # attribute races across the thread pool when the same handler class
        # is reused for different upstreams in one process.
        self.upstream = upstream or ("127.0.0.1", 8080)
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *fargs):  # quieter log
        sys.stderr.write("dev_proxy " + fmt % fargs + "\n")

    def do_GET(self):
        if self.path == "/api/render_metrics":
            self._handle_render_metrics_get()
        elif self.path.startswith("/api/"):
            self._proxy()
        else:
            super().do_GET()

    # Beacon-friendly POST. Special-cases /api/render_metrics locally — no
    # upstream round-trip, treated as a UI-only metric so the dashboard tile
    # never degrades if the bot is down. All other /api/* POSTs still proxy.
    def do_POST(self):
        if self.path == "/api/render_metrics":
            self._handle_render_metrics_post()
        elif self.path.startswith("/api/"):
            self._proxy_post()
        else:
            self.send_error(405, "Method Not Allowed")

    def _handle_render_metrics_get(self) -> None:
        rows = _tail_render_metrics(50)
        per_tab: dict[str, dict] = {}
        for r in rows:
            t = r.get("tab")
            ms = r.get("last_render_ms")
            ts = r.get("ts")
            if not t or ms is None:
                continue
            d = per_tab.setdefault(t, {"last_render_ms": ms, "last_ts": ts, "count": 0})
            if ts and (d["last_ts"] is None or ts > d["last_ts"]):
                d["last_render_ms"] = ms
                d["last_ts"] = ts
            d["count"] += 1
        body = json.dumps({
            "events": rows,
            "per_tab": per_tab,
            "count": len(rows),
        }).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_render_metrics_post(self) -> None:
        try:
            length_str = self.headers.get("Content-Length") or "0"
            n = int(length_str)
        except (TypeError, ValueError):
            n = 0
        # sendBeacon sends a Blob with Content-Length ≤ ~64 KB. Cap defensively.
        if n <= 0 or n > 65536:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.send_error(204)
            return
        try:
            raw = self.rfile.read(n)
        except Exception:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.send_error(204)
            return
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = None
        events: list[dict] = []
        if isinstance(body, dict):
            events = [body]
        elif isinstance(body, list):
            events = [e for e in body if isinstance(e, dict)]
        valid: list[dict] = []
        for e in events:
            tab = str(e.get("tab") or "").strip()
            ms = e.get("last_render_ms")
            try:
                ms_int = int(round(float(ms)))
            except (TypeError, ValueError):
                ms_int = None
            ts = e.get("ts")
            try:
                ts_int = int(ts) if ts is not None else int(time.time() * 1000)
            except (TypeError, ValueError):
                ts_int = int(time.time() * 1000)
            if not tab or ms_int is None:
                continue
            if not (0 <= ms_int <= 600_000):  # 10 min cap covers slow first-paint + CI hangs
                continue
            valid.append({"tab": tab, "last_render_ms": ms_int, "ts": ts_int})
        if valid:
            _append_render_metrics(valid)
        # Always answer 204 — sendBeacon ignores response but the browser still
        # likes a definite end-of-message. Quietly complete the round-trip.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.send_response(204)
            self.end_headers()

    def _proxy_post(self) -> None:
        """Forward POST body + headers to upstream bot (binary-safe)."""
        host, port = self.upstream
        skip = {"transfer-encoding", "connection", "keep-alive",
                "proxy-authenticate", "proxy-authorization", "te",
                "trailers", "upgrade", "host", "content-length"}
        fwd_headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in skip}
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        try:
            conn = http.client.HTTPConnection(host, port, timeout=60)
            conn.request("POST", self.path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in skip:
                    self.send_header(k, v)
            self.send_header("Content-Length", resp.getheader("Content-Length") or "0")
            self.end_headers()
            remaining = resp.read(8192)
            while remaining:
                try:
                    self.wfile.write(remaining)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining = resp.read(8192)
            conn.close()
        except Exception as e:
            sys.stderr.write(f"dev_proxy upstream POST error: {e}\n")
            with contextlib.suppress(Exception):
                self.send_error(502, f"upstream {host}:{port} unreachable: {e}")

    def _proxy(self):
        host, port = self.upstream
        # Strip hop-by-hop and length headers — http.client will recompute
        skip = {"transfer-encoding", "connection", "keep-alive",
                "proxy-authenticate", "proxy-authorization", "te",
                "trailers", "upgrade", "host", "content-length"}
        fwd_headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in skip}
        try:
            conn = http.client.HTTPConnection(host, port, timeout=60)
            conn.request("GET", self.path, headers=fwd_headers)
            resp = conn.getresponse()
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in skip:
                    self.send_header(k, v)
            # SSE: never set Content-Length so the chunked stream flushes
            if resp.getheader("Content-Length") and "text/event-stream" not in (
                resp.getheader("Content-Type", "")):
                self.send_header("Content-Length", resp.getheader("Content-Length"))
            self.end_headers()
            # Stream upstream body to client chunk-by-chunk
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
            conn.close()
        except Exception as e:
            sys.stderr.write(f"dev_proxy upstream error: {e}\n")
            try:
                self.send_error(502, f"upstream {host}:{port} unreachable: {e}")
            except Exception:
                pass


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _append_render_metrics(events: list[dict]) -> None:
    """Append one or more events to the JSONL file; trim when it gets large.

    Thread-safe (the dev_proxy serves concurrent SSE + JSON fetches via a
    ThreadingMixIn server). API callers fire-and-forget so failures must
    swallow quietly — never raise to the request handler. Trimming happens
    OUTSIDE this method's lock to avoid a non-reentrant ``threading.Lock``
    deadlock (the public trim helper also acquires the same lock).
    """
    needs_trim = False
    try:
        needs_trim = RENDER_METRICS_PATH.stat().st_size > 256 * 1024
    except Exception:
        needs_trim = False
    with _render_metrics_lock:
        try:
            RENDER_METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(RENDER_METRICS_PATH, "a", encoding="utf-8") as fh:
                for e in events:
                    fh.write(json.dumps(e, separators=(",", ":")) + "\n")
                    fh.flush()  # explicit flush for cross-thread visibility
        except Exception as exc:
            sys.stderr.write(f"dev_proxy render_metrics append failed: {exc}\n")
            return
    if needs_trim:
        _trim_render_metrics()


def _trim_render_metrics() -> None:
    """Keep the most recent RENDER_METRICS_MAX_ROWS rows; drop the rest."""
    with _render_metrics_lock:
        try:
            with open(RENDER_METRICS_PATH, encoding="utf-8") as fh:
                tail = deque(fh, maxlen=RENDER_METRICS_MAX_ROWS)
            with open(RENDER_METRICS_PATH, "w", encoding="utf-8") as fh:
                fh.writelines(tail)
        except Exception as exc:
            sys.stderr.write(f"dev_proxy render_metrics trim failed: {exc}\n")


def _tail_render_metrics(n: int) -> list[dict]:
    """Return the last ``n`` decoded rows; tolerate parse errors per line."""
    out: list[dict] = []
    with _render_metrics_lock:
        try:
            with open(RENDER_METRICS_PATH, encoding="utf-8") as fh:
                tail = deque(fh, maxlen=n)
        except FileNotFoundError:
            return []
        except Exception as exc:
            sys.stderr.write(f"dev_proxy render_metrics read failed: {exc}\n")
            return []
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def parse_upstream(spec: str) -> tuple[str, int]:
    if ":" not in spec:
        raise argparse.ArgumentTypeError(f"--upstream must be host:port, got {spec!r}")
    host, _, port = spec.partition(":")
    return host, int(port)


def main() -> int:
    ap = argparse.ArgumentParser(description="Dev proxy for Freebuff preview tab")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"port to listen on (default {DEFAULT_PORT})")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="bind address (default 127.0.0.1)")
    ap.add_argument("--upstream", type=parse_upstream, default=parse_upstream(DEFAULT_UPSTREAM),
                    help=f"bot host:port to forward /api/* to (default {DEFAULT_UPSTREAM})")
    ap.add_argument("--directory", default=str(DEFAULT_DASH),
                    help=f"directory of static files to serve (default {DEFAULT_DASH})")
    args = ap.parse_args()

    directory = Path(args.directory).resolve()
    if not directory.is_dir():
        print(f"ERROR: directory does not exist: {directory}", file=sys.stderr)
        return 2

    handler = lambda *a, **kw: _ProxyHandler(*a, directory=str(directory),
                                             upstream=args.upstream, **kw)
    srv = _ThreadingServer((args.bind, args.port), handler)
    print(f"dev_proxy listening on http://{args.bind}:{args.port}/  "
          f"serving {directory}/  api->http://{args.upstream[0]}:{args.upstream[1]}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("dev_proxy shutting down", flush=True)
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
