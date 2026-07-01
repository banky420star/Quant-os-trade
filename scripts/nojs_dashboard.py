"""Standalone no-JavaScript dashboard -- the reliable UI when the bot's in-process
SPA dashboard stalls.

Why this exists: the live bot (start.py) serves its dashboard IN-PROCESS on :8080.
When the trading loop is busy the HTTP layer stalls, so /api/summary times out and
the SPA hangs on its loading screen (especially in older/phone browsers that also
can't run the SPA's ES6). This server fixes both problems:

  * SEPARATE PROCESS -- does not contend with the trading loop; always responsive.
  * READS STATE FILES DIRECTLY -- no HTTP round-trip to the busy bot; reads the same
    JSON files the bot writes continuously. Cheap (small files), always fresh.
  * NO JAVASCRIPT -- pure HTML + <meta http-equiv="refresh">. Renders in ANY browser,
    including old phone WebViews that choke on async/AbortController/template-literals.
  * THREADED -- concurrent requests don't queue behind each other.

Run (from repo root):
    python scripts/nojs_dashboard.py                # default port 8081
    python scripts/nojs_dashboard.py --port 8081

Then open http://localhost:8081  (or http://100.93.135.31:8081 over Tailscale).
The bot on :8080 keeps running untouched -- this only READS state files.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "state"
REFRESH = 5  # seconds


def _load(name):
    try:
        with open(STATE / name, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _num(x, fmt="{:.2f}"):
    try:
        return fmt.format(float(x))
    except Exception:
        return str(x) if x is not None else "—"


def _money(x):
    try:
        return "${:.2f}".format(float(x))
    except Exception:
        return "—"


def _pct(x):
    try:
        return "{:+.2f}%".format(float(x))
    except Exception:
        return "—"


def _cls_pos(v):
    try:
        return "ok" if float(v) >= 0 else "bad"
    except Exception:
        return ""


def render():
    acc = _load("account.json") or {}
    hlt = _load("health.json") or {}
    ks = _load("kill_switch.json") or {}
    pos = _load("paper_positions.json") or {}
    rt = _load("runtime_mode.json") or {}
    rem = _load("remote_access.json") or {}
    dg = _load("daily_growth.json") or {}
    sup = _load("supervisor.json") or {}
    rstate = _load("risk_state.json") or {}

    bal = acc.get("balance")
    eq = acc.get("equity")
    try:
        unrl = float(eq) - float(bal)
    except Exception:
        unrl = None

    positions = pos.get("positions", []) if isinstance(pos, dict) else []
    kill = bool(ks.get("kill_switch")) if isinstance(ks, dict) else False
    hstatus = hlt.get("status", "—") if isinstance(hlt, dict) else "—"

    rows = []
    for p in positions:
        pr = p.get("profit")
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(p.get('symbol','')))}</td>"
            f"<td class='{ 'bad' if str(p.get('side','')).upper()=='SELL' else 'ok'}'>"
            f"{html.escape(str(p.get('side','')))}</td>"
            f"<td>{_num(p.get('entry'))}</td>"
            f"<td>{_num(p.get('size'))}</td>"
            f"<td class='{_cls_pos(pr)}'>{_money(pr)}</td>"
            f"<td>{_num(p.get('sl'))}</td>"
            f"<td>{_num(p.get('tp1') or p.get('tp'))}</td>"
            f"<td>{html.escape(str(p.get('setup_type','')))}</td>"
            f"<td>{html.escape(str(p.get('ticket','')))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="9" class="muted">no open positions</td></tr>')

    # approved/rejected signal counts (cheap: just count list keys)
    ap = _load("approved_signals.json")
    rej = _load("rejected_signals.json")
    ap_n = len(ap) if isinstance(ap, list) else (len(ap.get("signals", [])) if isinstance(ap, dict) and "signals" in ap else "—")
    rej_n = len(rej) if isinstance(rej, list) else (len(rej.get("signals", [])) if isinstance(rej, dict) and "signals" in rej else "—")

    # supervisor loop timestamps
    sup_lines = []
    if isinstance(sup, dict):
        loops = sup.get("loops", {}) if isinstance(sup.get("loops"), dict) else {}
        for name in ("execution", "research", "health", "data"):
            lp = loops.get(name) or {}
            ts = lp.get("last_run") or lp.get("last_complete") or lp.get("timestamp")
            if ts:
                sup_lines.append(f"<span class='chip'><b>{name}</b> {html.escape(str(ts))}</span>")

    tipscale = rem.get("tailscale_ip") if isinstance(rem, dict) else None

    card = (
        "<div class='k'><b>{lbl}</b><span class='{cls}'>{val}</span></div>"
    )

    def K(lbl, val, cls=""):
        return f"<div class='k'><b>{html.escape(lbl)}</b><span class='{cls}'>{val}</span></div>"

    head = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<meta http-equiv='refresh' content='{REFRESH}'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>MT5 Quant OS — live (no-JS)</title>
<style>
*{{box-sizing:border-box}}
body{{background:#07070a;color:#f5f5f7;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:16px;font-size:14px}}
h2{{font-weight:600;margin:4px 0 2px}}
.muted{{color:#8e8e93;font-size:12px}}
a{{color:#0a84ff}}
.wrap{{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}}
.k{{background:#1c1c1e;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:10px 14px;min-width:120px}}
.k b{{color:#8e8e93;font-weight:500;font-size:11px;display:block;text-transform:uppercase;letter-spacing:.04em}}
.k span{{font-size:20px;font-weight:600}}
.ok{{color:#30d158}}.bad{{color:#ff453a}}.warn{{color:#ffd60a}}
table{{border-collapse:collapse;width:100%;margin-top:8px}}
td,th{{border:1px solid rgba(255,255,255,.08);padding:7px 9px;text-align:left;font-size:13px}}
th{{background:#1c1c1e;color:#8e8e93;text-transform:uppercase;font-size:11px;letter-spacing:.04em}}
.chip{{display:inline-block;background:#1c1c1e;border:1px solid rgba(255,255,255,.08);border-radius:999px;padding:4px 10px;margin:2px;font-size:12px}}
.chip b{{color:#8e8e93;font-weight:500}}
.section{{margin-top:16px}}
.bar{{height:6px;background:#2c2c2e;border-radius:3px;overflow:hidden;margin-top:4px}}
.bar>i{{display:block;height:100%;background:#0a84ff}}
.note{{background:#1c1c1e;border-left:3px solid #0a84ff;padding:8px 12px;border-radius:6px;margin-top:12px;font-size:12px;color:#aeaeb2}}
</style></head><body>"""

    body = f"""
<h2>MT5 Quant OS — live status</h2>
<div class='muted'>demo account <b>{acc.get('login','—')}</b> · {acc.get('server','—')} · mode=<b>{acc.get('account_mode','—')}</b>
· runtime=<b>{rt.get('label','—')}</b> · updated {time.strftime('%H:%M:%S')} (auto-refresh {REFRESH}s, no JS)</div>

<div class='wrap'>
{K('Balance', _money(bal))}
{K('Equity', _money(eq), _cls_pos(unrl) if unrl is not None else '')}
{K('Unrealized P/L', _money(unrl), _cls_pos(unrl) if unrl is not None else '')}
{K('Kill switch', 'ON' if kill else 'OFF', 'bad' if kill else 'ok')}
{K('Health', html.escape(str(hstatus)), 'ok' if hstatus=='healthy' else 'bad')}
{K('Open positions', str(len(positions)))}
{K('Approved signals', str(ap_n))}
{K('Rejected signals', str(rej_n))}
</div>

<div class='section'>
<h3 style='margin:0 0 4px'>Open positions ({len(positions)})</h3>
<table><tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Size</th><th>P/L</th><th>SL</th><th>TP</th><th>Setup</th><th>Ticket</th></tr>
{''.join(rows)}
</table>
</div>

<div class='section'>
<h3 style='margin:0 0 6px'>Supervisor loops</h3>
{''.join(sup_lines) if sup_lines else '<span class="muted">no loop timestamps</span>'}
</div>

{(_growth_card(dg) )}

<div class='note'>
<b>This is the reliable no-JS mirror.</b> It reads the bot's state files directly and refreshes every {REFRESH}s.
The full SPA is at <a href='http://localhost:8080/'>localhost:8080</a>
{(' · phone/Tailscale: <a href="http://'+str(tipscale)+':8081/">'+str(tipscale)+':8081</a>') if tipscale else ''}.
Bot is untouched — this only reads files.
</div>
"""
    return head + body + "</body></html>"


def _growth_card(dg):
    if not isinstance(dg, dict) or not dg:
        return ""
    try:
        tgt = float(dg.get("daily_target_pct", 0) or 0)
        start = float(dg.get("starting_balance") or dg.get("starting_cash") or 0)
        cur = float(dg.get("current_balance") or dg.get("balance") or 0)
        pct = ((cur - start) / start * 100.0) if start else 0.0
        bar = max(0, min(100, pct / max(tgt, 0.01) * 100)) if tgt else 0
        return f"""<div class='section'>
<h3 style='margin:0 0 6px'>Growth campaign</h3>
<div class='muted'>target {tgt:.1f}%/day · start ${start:.2f} · now ${cur:.2f} · session {_pct(pct)} of day-target</div>
<div class='bar'><i style='width:{bar:.1f}%'></i></div>
</div>"""
    except Exception:
        return ""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            body = render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.split("?")[0] == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass  # quiet


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), H)
    srv.daemon_threads = True
    print(f"no-JS dashboard on http://{args.host}:{args.port}  (state dir: {STATE})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()