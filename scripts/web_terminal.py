"""Tailscale-reachable web terminal that drives a Claude Code session from a phone.

Serves an xterm.js terminal in the browser; on WebSocket connect (with a valid
token) it spawns `claude -r <session-id>` inside a real Windows PTY (pywinpty)
and bridges PTY <-> WebSocket so the phone gets the full Claude TUI (colors,
cursor, the lot) and can type prompts. This lets the user continue THIS chat
from their iPhone over Tailscale, since Claude Code's first-class Remote Control
(`/rc`) is disabled when the endpoint is a gateway/proxy (this setup routes
through ANTHROPIC_BASE_URL=http://127.0.0.1:11434 -> glm-5.2:cloud) instead of
api.anthropic.com + claude.ai OAuth.

SECURITY:
  * Bound to 0.0.0.0:<port> so it is reachable over Tailscale, BUT a firewall
    rule scoped to the Tailscale CGNAT range (100.64.0.0/10) must gate the port
    -- never expose it publicly. See the launcher / docs.
  * Auth: a random token (secrets.token_urlsafe). The HTML page is only served
    when the request carries ?token=<token>; the WebSocket upgrade also requires
    it. Without the token you get 401. Treat the token like a password -- it
    grants a shell running `claude` as the server user.
  * One active PTY connection at a time (per-connection spawn of `claude -r`).
    A second connection while one is live is rejected with a message, to avoid
    two Claude processes on the same session ID corrupting the transcript.

CONCURRENT-SESSION WARNING: do not drive the same session from the console AND
the phone at the same time. Exit the console Claude first, then connect from the
phone (which resumes the session by ID). The phone and console are alternate
windows into one conversation, not simultaneous ones.

Run (from repo root, inherits the env so the spawned `claude` sees
ANTHROPIC_BASE_URL etc.):
    python scripts/web_terminal.py --port 8085 --session <session-id>
    python scripts/web_terminal.py --port 8085 --session <id> --token <tok>

Then on the phone over Tailscale:
    http://100.93.135.31:8085/?token=<token>
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import secrets
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"

# RFC 6455 magic GUID for the WebSocket handshake.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# One active PTY at a time. Guarded by a lock; the holder spawns `claude -r`.
_active_lock = threading.Lock()
_active_holder: "PtySession | None" = None


def _load_or_create_token(path: Path) -> str:
    """Persist a random token so reconnects/relaunches use the same one."""
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            tok = data.get("token")
            if tok:
                return tok
    except Exception:
        pass
    tok = secrets.token_urlsafe(18)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"token": tok, "created": time.strftime("%Y-%m-%d %H:%M:%S")}),
                    encoding="utf-8")
    return tok


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
    """Send one (unmasked, server->client) frame. Returns False on failure."""
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


class PtySession:
    """A winpty PTY running `claude -r <session-id>`, bridged to one WS client."""

    def __init__(self, cmd_argv: list[str], cwd: str, env: dict[str, str] | None,
                 cols: int = 90, rows: int = 30):
        import shutil
        import winpty  # local import so the module loads even if missing
        # pywinpty 3.x. Force the WinPTY backend: on this Server 2022 + pywinpty
        # 3.0.5 combo the default ConPTY backend drops the spawned command-line
        # args (the child re-execs the exe as a script). WinPTY passes args
        # correctly (verified: full ANSI output captured).
        # spawn(appname, cmdline=..., cwd=..., env=...): appname should be a
        # resolved full path; cmdline is a single command-line string. env=None
        # -> inherit the parent process env (so `claude` sees ANTHROPIC_BASE_URL
        # etc.); passing a dict raises TypeError in this version.
        self.pty = winpty.PTY(cols, rows, backend=winpty.Backend.WinPTY)
        self.cmd_argv = cmd_argv
        self._closed = False
        appname = shutil.which(cmd_argv[0]) or cmd_argv[0]
        cmdline = " ".join([appname] + cmd_argv[1:])
        self.pty.spawn(appname, cmdline=cmdline, cwd=cwd, env=None)

    def read(self) -> bytes | None:
        try:
            data = self.pty.read(4096, blocking=False)
        except Exception:
            return None
        if not data:
            return None
        return data if isinstance(data, bytes) else data.encode("utf-8", "replace")

    def write(self, data: bytes) -> None:
        try:
            s = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else data
            self.pty.write(s)
        except Exception:
            pass

    def set_size(self, cols: int, rows: int) -> None:
        try:
            self.pty.set_size(cols, rows)
        except Exception:
            pass

    def isalive(self) -> bool:
        try:
            return bool(self.pty.isalive())
        except Exception:
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # pywinpty's PTY has no .close(); cleanup is via __del__ (the upstream
        # tests do `del pty`). cancel_io stops pending reads first.
        try:
            self.pty.cancel_io()
        except Exception:
            pass
        try:
            del self.pty
        except Exception:
            pass


def _ws_serve(handler, token: str, cmd_argv: list[str], cwd: str, env: dict[str, str]) -> None:
    """Drive one WebSocket connection: spawn claude in a PTY and bridge."""
    global _active_holder
    # Auth: require ?token= on the WS upgrade URL.
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(handler.path).query)
    if (q.get("token", [""])[0] != token):
        handler.send_response(401)
        handler.send_header("Content-Type", "text/plain")
        handler.end_headers()
        handler.wfile.write(b"unauthorized")
        return
    if not _ws_handshake(handler):
        return

    # Only one active PTY at a time.
    if not _active_lock.acquire(blocking=False):
        _ws_send(handler.connection,
                 b"\r\n[busy] another terminal session is already active. "
                 b"Close it first, then reconnect.\r\n")
        try:
            handler.connection.close()
        except Exception:
            pass
        return
    sess = PtySession(cmd_argv, cwd, env)
    _active_holder = sess
    sock = handler.connection
    try:
        sock.settimeout(None)
        # Reader thread: PTY -> WS (binary frames preserve ANSI/UTF-8 bytes).
        stop = threading.Event()

        def pump_pty():
            while not stop.is_set() and sess.isalive():
                data = sess.read()
                if data:
                    if not _ws_send(sock, data, opcode=0x02):
                        break
                else:
                    time.sleep(0.02)
            stop.set()

        reader = threading.Thread(target=pump_pty, daemon=True)
        reader.start()

        # Main loop: WS -> PTY. Binary = keystrokes; text = control JSON (resize).
        while not stop.is_set():
            r, _, _ = select.select([sock], [], [], 0.5)
            if not r:
                if not sess.isalive():
                    break
                continue
            got = _ws_recv(sock)
            if got is None:
                break
            opcode, data = got
            if opcode == 0x8:  # close
                _ws_send(sock, b"", opcode=0x8)
                break
            if opcode == 0x9:  # ping -> pong
                if not _ws_send(sock, b"", opcode=0xA):
                    break
                continue
            if opcode == 0x1:  # text = control message
                try:
                    msg = json.loads(data.decode("utf-8", "replace"))
                    if "cols" in msg and "rows" in msg:
                        sess.set_size(int(msg["cols"]), int(msg["rows"]))
                except Exception:
                    pass
                continue
            if opcode in (0x2, 0x0):  # binary = raw keystrokes
                sess.write(data)
        stop.set()
    except (OSError, ValueError):
        pass
    finally:
        try:
            sess.close()
        except Exception:
            pass
        _active_holder = None
        _active_lock.release()
        try:
            sock.close()
        except Exception:
            pass


def _page_html(token: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no'>"
        "<title>Claude Code — remote terminal</title>"
        "<link rel='stylesheet' href='https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css'>"
        "<style>"
        "html,body{margin:0;height:100%;background:#07070a;overflow:hidden}"
        "#t{height:100%;width:100%}"
        ".bar{position:fixed;top:0;left:0;right:0;background:#1c1c1e;color:#8e8e93;"
        "font:11px system-ui;padding:4px 8px;z-index:10;display:flex;gap:8px;align-items:center}"
        ".bar b{color:#34c759}.dot{width:7px;height:7px;border-radius:50%;background:#34c759}"
        "</style></head><body>"
        "<div class='bar'><span class='dot'></span><b>Claude Code</b> "
        "<span id='st'>connecting…</span></div>"
        "<div id='t' style='padding-top:22px'></div>"
        "<script src='https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js'></script>"
        "<script src='https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js'></script>"
        "<script>(function(){"
        "var term=new Terminal({cursorBlink:true,fontFamily:'ui-monospace,Menlo,Consolas,monospace',"
        "fontSize:12,scrollback:2000,convertEol:false});"
        "var fit=new FitAddon.FitAddon();term.loadAddon(fit);term.open(document.getElementById('t'));"
        "try{fit.fit();}catch(e){}"
        "var url=(location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/ws?token='+"
        "encodeURIComponent('" + token + "');"
        "var ws=new WebSocket(url);ws.binaryType='arraybuffer';"
        "var st=document.getElementById('st');"
        "ws.onopen=function(){st.textContent='LIVE';st.style.color='#34c759';sendSize();};"
        "ws.onmessage=function(ev){"
        "if(ev.data instanceof ArrayBuffer){term.write(new Uint8Array(ev.data));}"
        "else{term.write(ev.data);}};"
        "ws.onclose=function(){st.textContent='disconnected';st.style.color='#ff453a';};"
        "ws.onerror=function(){try{ws.close();}catch(e){}};"
        "function sendSize(){if(ws.readyState===1){var d=fit.proposeDimensions();"
        "ws.send(JSON.stringify({cols:d.cols,rows:d.rows}));}}"
        "term.onData(function(d){if(ws.readyState===1){ws.send(new TextEncoder().encode(d));}});"
        "term.onResize(function(){sendSize();});"
        "window.addEventListener('resize',function(){try{fit.fit();}catch(e){}sendSize();});"
        "window.addEventListener('focus',function(){try{fit.fit();}catch(e){}});"
        "})();</script>"
        "</body></html>"
    )


class H(BaseHTTPRequestHandler):
    token = ""
    cmd_argv: list[str] = []
    cwd = str(ROOT)
    env: dict[str, str] = {}

    def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        from urllib.parse import urlparse, parse_qs
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        # Auth gate: every path requires ?token=
        if query.get("token", [""])[0] != self.token:
            self._send(b"unauthorized - append ?token=<token>\n", "text/plain", 401)
            return
        if path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            _ws_serve(self, self.token, self.cmd_argv, self.cwd, self.env)
            return
        if path in ("/", "/index.html"):
            self._send(_page_html(self.token).encode("utf-8"), "text/html; charset=utf-8")
            return
        self._send(b"not found\n", "text/plain", 404)

    def log_message(self, format: str, *args) -> None:
        try:
            line = "%s %s\n" % (time.strftime("%H:%M:%S"), (format % args))
            with (STATE / "web_terminal_access.log").open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        except Exception:
            pass


def _pick_port(preferred: int) -> int:
    for port in range(preferred, preferred + 7):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", port))
                return port
        except OSError:
            continue
    raise SystemExit(f"no free port in {preferred}..{preferred + 6}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Web terminal -> Claude Code over Tailscale.")
    ap.add_argument("--port", type=int, default=8085)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--session", required=True,
                    help="Claude Code session ID to resume (`claude -r <id>`).")
    ap.add_argument("--token", default=None,
                    help="auth token; if omitted, a random one is generated and persisted.")
    ap.add_argument("--cwd", default=str(ROOT),
                    help="working directory for the spawned claude process.")
    args = ap.parse_args()

    token = args.token or _load_or_create_token(STATE / "web_terminal_token.json")
    # Spawn `claude -r <session-id>` so the phone continues THIS conversation.
    cmd_argv = ["claude", "-r", args.session]
    env = dict(os.environ)  # inherit ANTHROPIC_BASE_URL etc. so claude works

    H.token = token
    H.cmd_argv = cmd_argv
    H.cwd = args.cwd
    H.env = env

    port = _pick_port(args.port)
    srv = ThreadingHTTPServer((args.host, port), H)
    srv.daemon_threads = True
    print(f"web terminal on http://{args.host}:{port}/?token={token}", flush=True)
    print(f"  spawns: {' '.join(cmd_argv)}  (cwd: {args.cwd})", flush=True)
    print("  REMINDER: do not run the console Claude + phone on the same session at once.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()