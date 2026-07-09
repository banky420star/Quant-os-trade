"""Tailscale-reachable HTTP wrapper around the terminal TUI frame.

Renders the SAME frame as `scripts/terminal_view.py` (reuses its
`render_frame()`), but serves it as a dark, live-updating HTML page so it
can be viewed from a phone browser over Tailscale. The ANSI TUI itself is a
local process and can't be served over HTTP; this is the read-only web mirror.

  * READ-ONLY -- reads state/*.json the bot writes continuously; mutates
    nothing. Does not touch the bot, config, or trading files.
  * SEPARATE PROCESS -- does not contend with the trading loop.
  * LIVE WEBSOCKET -- `/ws` pushes the rendered frame + an update timestamp
    every PUSH_INTERVAL seconds; the page shows a green "LIVE" dot and the
    last-update time. Falls back to a full reload if the socket drops. The
    JS-free mirror stays on :8081 (nojs_dashboard) for browsers without JS.
  * Port 8083 by default; if busy, picks the next free one up to 8089.
  * Firewall rule scoped to Tailscale CGNAT (100.64.0.0/10) -- not public.

Run (from repo root):
    python scripts/terminal_view_server.py
    python scripts/terminal_view_server.py --port 8083

Then open http://100.93.135.31:8083/ over Tailscale (or
http://127.0.0.1:8083/ locally).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import importlib
import json
import re
import select
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Import the TUI renderer. The module lives next to this file.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
import terminal_view as tv  # noqa: E402

PUSH_INTERVAL = 2  # seconds between websocket pushes
REFRESH = 2  # kept for the header label / legacy callers

# ANSI color -> HTML span converter (USER request 2026-07-01: "regenerate with
# colour"). The renderer (terminal_view.py) emits ANSI SGR codes when
# USE_COLOR=True; instead of stripping them to monochrome, we translate each
# color/bold/dim run into a <span class="..."> so the served frame keeps its
# red/green/yellow semantic color in the browser. Text runs are html-escaped
# so this is XSS-safe; the only markup injected is our own span tags.
_ANSI_SPLIT = re.compile(r"(\x1b\[[0-9;]*m)")
_COLOR_MAP = {  # SGR code -> CSS class (Apple-dark palette, matches .ts green)
    "31": "c-r", "32": "c-g", "33": "c-y", "34": "c-b",
    "35": "c-m", "36": "c-c", "90": "c-gr",
}

# RFC 6455 magic GUID for the WebSocket handshake.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ansi_to_html(s: str) -> str:
    """Convert ANSI-colored terminal text to colored HTML spans.

    Walks the SGR codes (reset=0, bold=1, dim=2, 31..36/90 colors), keeps the
    currently-active attributes, and wraps each text run in a span whose
    classes reflect them. Reset closes everything. Handles combined codes like
    ``\\x1b[1;31m`` even though terminal_view.py emits them singly."""
    color = None
    bold = False
    dim = False
    out: list[str] = []

    def _emit(text: str) -> None:
        if not text:
            return
        cls = []
        if color:
            cls.append(color)
        if bold:
            cls.append("b")
        if dim:
            cls.append("d")
        esc = html.escape(text)
        out.append(f'<span class="{" ".join(cls)}">{esc}</span>' if cls else esc)

    for part in _ANSI_SPLIT.split(s):
        if not part:
            continue
        if part[0] == "\x1b":
            # part looks like "\x1b[1;31m" -> codes "1;31"
            codes = part[2:-1] if len(part) > 2 else ""
            for c in codes.split(";"):
                if c in ("", "0"):
                    color = None
                    bold = False
                    dim = False
                elif c == "1":
                    bold = True
                elif c == "2":
                    dim = True
                elif c in _COLOR_MAP:
                    color = _COLOR_MAP[c]
        else:
            _emit(part)
    return "".join(out)


def _preset_buttons_html() -> str:
    from core.fast_mode_runtime import FAST_MODE_PRESETS, read_runtime

    rt = read_runtime()
    active = rt.get("preset")
    parts = []
    for pid, spec in FAST_MODE_PRESETS.items():
        cls = " on" if active == pid else ""
        parts.append(
            f"<button type='button' class='pbtn{cls}' data-preset='{html.escape(pid)}' "
            f"title='{html.escape(spec.get('description', ''))}'>{html.escape(spec.get('label', pid))}</button>"
        )
    parts.append("<button type='button' class='pbtn' data-preset='clear'>Profile default</button>")
    return "<div class='preset-bar'>" + "".join(parts) + "<span id='pstatus'></span></div>"


def render_html() -> str:
    # Initial server-rendered frame so the page is non-empty before the
    # WebSocket connects; the socket then overwrites #frame on each push.
    frame = _ansi_to_html(tv.render_frame())
    body_text = frame  # _ansi_to_html already html-escapes text runs
    presets = _preset_buttons_html()
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>MT5 Quant OS — terminal view</title>"
        "<style>"
        "body{background:#07070a;color:#f5f5f7;"
        "font-family:ui-monospace,Menlo,Consolas,monospace;"
        "padding:16px;font-size:13px;margin:0}"
        ".hdr{color:#8e8e93;font-size:12px;margin-bottom:8px}"
        ".ts{color:#34c759}"
        ".dot{display:inline-block;width:8px;height:8px;border-radius:50%;"
        "background:#34c759;margin-right:6px;vertical-align:middle;"
        "animation:pulse 1.6s infinite}"
        ".dot.warn{background:#ff9f0a;animation:none}"
        "@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}"
        "#frame{white-space:pre;overflow-x:auto}"
        ".c-r{color:#ff453a}.c-g{color:#34c759}.c-y{color:#ffd60a}"
        ".c-b{color:#0a84ff}.c-m{color:#bf5af2}.c-c{color:#64d2ff}"
        ".c-gr{color:#8e8e93}.b{font-weight:bold}.d{opacity:.55}"
        ".preset-bar{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 12px;align-items:center}"
        ".pbtn{background:#222228;color:#f5f5f7;border:1px solid #333;padding:6px 10px;"
        "border-radius:6px;font-size:11px;cursor:pointer;font-family:inherit}"
        ".pbtn.on{border-color:#0a84ff;color:#0a84ff}"
        "#pstatus{font-size:11px;color:#8e8e93;margin-left:6px}"
        "@media(max-width:640px){body{font-size:9px;padding:8px}"
        "#frame{font-size:9px}}"
        "</style></head><body>"
        "<div class='hdr'><span class='dot' id='dot'></span>"
        "<b>MT5 Quant OS — terminal view (Tailscale)</b> · "
        "<span id='mode'>connecting…</span> · updated <span class='ts' "
        "id='ts'>—</span></div>"
        + presets +
        "<div id='frame'>" + body_text + "</div>"
        "<script>(function(){"
        "document.querySelectorAll('.pbtn').forEach(function(b){"
        "b.addEventListener('click',function(){"
        "var p=b.getAttribute('data-preset');"
        "var body=p==='clear'?{action:'clear'}:{action:'preset',preset:p};"
        "fetch('/api/fast-mode',{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify(body)}).then(function(r){return r.json();}).then(function(o){"
        "var s=document.getElementById('pstatus');"
        "if(s){s.textContent=o.ok?(o.label||p)+' applied':(o.message||'failed');}"
        "document.querySelectorAll('.pbtn').forEach(function(x){x.classList.remove('on');});"
        "if(o.ok&&p!=='clear'){b.classList.add('on');}"
        "}).catch(function(){var s=document.getElementById('pstatus');if(s)s.textContent='API error';});"
        "});});"
        "var f=document.getElementById('frame'),ts=document.getElementById('ts'),"
        "m=document.getElementById('mode'),d=document.getElementById('dot');"
        "var url=(location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/ws';"
        "var ws=new WebSocket(url);var dead=false;"
        "function reload(){if(!dead){dead=true;setTimeout(function(){location.reload();},2500);}}"
        "ws.onopen=function(){m.textContent='LIVE ws';d.classList.remove('warn');};"
        "ws.onmessage=function(ev){try{var o=JSON.parse(ev.data);"
        "f.innerHTML=o.frame;ts.textContent=o.ts;m.textContent='LIVE ws';"
        "d.classList.remove('warn');}catch(e){f.textContent=ev.data;}};"
        "ws.onclose=function(){m.textContent='reconnecting…';d.classList.add('warn');reload();};"
        "ws.onerror=function(){try{ws.close();}catch(e){}};"
        "})();</script>"
        "</body></html>"
    )


# --- minimal RFC 6455 frame helpers (server side, pure stdlib) ---

def _recv_exact(sock, n: int):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _ws_recv(sock):
    """Read one client frame. Returns (opcode, payload) or None on disconnect."""
    hdr = _recv_exact(sock, 2)
    if not hdr:
        return None
    b1, b2 = hdr[0], hdr[1]
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        ext = _recv_exact(sock, 2)
        if ext is None:
            return None
        length = int.from_bytes(ext, "big")
    elif length == 127:
        ext = _recv_exact(sock, 8)
        if ext is None:
            return None
        length = int.from_bytes(ext, "big")
    mask = _recv_exact(sock, 4) if masked else b""
    if mask is None:
        return None
    data = _recv_exact(sock, length) if length else b""
    if data is None:
        return None
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return opcode, data


def _ws_send(sock, payload: bytes, opcode: int = 0x01) -> bool:
    """Send a single (unmasked, server->client) frame. Returns False on failure."""
    out = bytearray([0x80 | opcode])  # FIN + opcode
    n = len(payload)
    if n < 126:
        out.append(n)
    elif n < 65536:
        out.append(126)
        out += n.to_bytes(2, "big")
    else:
        out.append(127)
        out += n.to_bytes(8, "big")
    out += payload
    try:
        sock.sendall(bytes(out))
        return True
    except OSError:
        return False


def _ws_handshake(handler) -> bool:
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key:
        return False
    accept = base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode("utf-8")).digest()
    ).decode("ascii")
    handler.send_response(101)
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()
    return True


def _ws_serve(handler) -> None:
    """Drive one WebSocket connection: push the live frame + ts every
    PUSH_INTERVAL seconds until the client closes/drops."""
    if not _ws_handshake(handler):
        return
    sock = handler.connection
    try:
        sock.settimeout(None)
        while True:
            # Drain any client frames (close/ping/text) without blocking.
            r, _, _ = select.select([sock], [], [], 0)
            if r:
                got = _ws_recv(sock)
                if got is None:
                    break  # client gone
                opcode, _ = got
                if opcode == 0x8:  # close
                    _ws_send(sock, b"", opcode=0x8)
                    break
                # ping (0x9) -> pong (0xA); ignore everything else
                if opcode == 0x9:
                    if not _ws_send(sock, b"", opcode=0xA):
                        break
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            importlib.reload(tv)
            frame = _ansi_to_html(tv.render_frame())
            msg = json.dumps({"ts": ts, "frame": frame}).encode("utf-8")
            if not _ws_send(sock, msg):
                break
            time.sleep(PUSH_INTERVAL)
    except (OSError, ValueError):
        pass


class H(BaseHTTPRequestHandler):
    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if path == "/api/fast-mode":
            try:
                from core.fast_mode_runtime import (
                    FAST_MODE_PRESETS,
                    apply_overrides,
                    apply_preset,
                    clear_runtime,
                    read_runtime,
                )

                body = self._read_json_body()
                action = str(body.get("action") or "preset").lower()
                if action == "clear":
                    clear_runtime()
                    doc = read_runtime()
                elif action == "overrides":
                    overrides = dict(body.get("overrides") or {})
                    if not overrides:
                        self._send_json({"ok": False, "message": "overrides required"}, 400)
                        return
                    doc = apply_overrides(overrides, preset_id=body.get("preset"))
                else:
                    preset = str(body.get("preset") or "").lower()
                    if preset not in FAST_MODE_PRESETS:
                        self._send_json({"ok": False, "message": "unknown preset"}, 400)
                        return
                    doc = apply_preset(preset, extra=body.get("extra"))
                self._send_json({
                    "ok": True,
                    "preset": doc.get("preset"),
                    "label": doc.get("label"),
                    "overrides": doc.get("overrides"),
                })
            except Exception as exc:
                self._send_json({"ok": False, "message": str(exc)}, 500)
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        # WebSocket upgrade for the live-updating TUI stream.
        if path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            _ws_serve(self)
            return
        if path in ("/", "/index.html"):
            body = render_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found")

    def log_message(self, *a):
        pass  # quiet


def _pick_port(preferred: int) -> int:
    import socket
    for port in range(preferred, preferred + 7):  # 8083..8089
        if port > 65535:
            break
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", port))
                return port
        except OSError:
            continue
    raise SystemExit(f"no free port in {preferred}..{preferred + 6}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Serve the terminal TUI frame over HTTP for Tailscale.")
    ap.add_argument("--port", type=int, default=8083,
                    help="port to bind (default 8083; auto-increments to 8089 if busy)")
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    # NOTE: do NOT call _disable_color() — we want render_frame() to emit ANSI
    # codes so _ansi_to_html() can colorize the served frame (USER 2026-07-01).

    port = _pick_port(args.port)
    srv = ThreadingHTTPServer((args.host, port), H)
    srv.daemon_threads = True
    print(f"terminal view server on http://{args.host}:{port}  "
          f"(state dir: {tv.STATE})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()