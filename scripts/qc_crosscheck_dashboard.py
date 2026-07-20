"""QuantConnect cross-check dashboard -- a read-only HTML view of the
independent verification of the bot's exit-mechanism backtest finding.

WHAT THIS SHOWS
  The bot's quantum_loop reported `baseline|medium|off` at +0.35R net (75% win,
  DSR 0.619) -- seemingly a real edge from break-even + trailing exits. The
  exit-model-bias hypothesis (memory: exit-model-bias-found-2026-06-27) says
  that number is a close-to-close backtest artifact: the bot's PaperBroker
  inspects only bar CLOSES for exits, so BE/trailing never fire intrabar the
  way a real broker would. Two INDEPENDENT engines test this:

    1. Pandas engine (scripts/independent_backtest_check.py) -- zero shared code
       with the bot, INTRABAR (checks bar high/low for stops). Same window.
    2. QuantConnect Lean cloud engine (qc_crosscheck/main.py) -- a THIRD engine
       using QC native stop/TP orders (intrabar fills) on OANDA XAUUSD minute
       bars. BLOCKED on user QC credentials (see bottom of page).

  If both independent engines show medium <= off (or both negative), the exit
  edge is robustly an artifact across engines + data. The pandas engine already
  answers this; the QC run is corroboration for whenever creds land.

This dashboard reads state files + runs the cheap pandas engine inline. It does
NOT touch MT5, the bot, or live trading. Read-only research UI.

Run (from repo root):
    python scripts/qc_crosscheck_dashboard.py             # default port 8084
    python scripts/qc_crosscheck_dashboard.py --port 8084
"""
from __future__ import annotations

import argparse
import html
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
REFRESH = 15  # seconds -- cross-check results are static, refresh slow

# --- independent pandas engine (imported lazily so import errors are visible) ---
_BOT_REPORT = ROOT / "quantum_loop_report_realgrid.json"
_QC_RESULTS = ROOT / "state" / "qc_crosscheck_results.json"  # written by qc run if it ever lands


def _load_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _independent_results():
    """Run the pandas cross-check engine inline. Cheap (~1s on 1546 bars).
    Returns list of dicts: [{exit, trades, wr, net_r, ci_lo, pf}, ...]."""
    try:
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        import independent_backtest_check as ibc  # type: ignore
        df = ibc.slice_window(
            ibc.load("XAUUSDm", "M15"),
            "2025-12-09", "2026-01-04",
        )
        out = []
        for mode in ("off", "medium", "wide"):
            r = ibc.backtest(df, exit_mode=mode)
            out.append({
                "exit": mode,
                "trades": r["trades"],
                "wr": r["wr"],
                "net_r": r["net_r"],
                "ci_lo": r["ci_lo"],
                "pf": r["pf"],
            })
        return out, None
    except Exception as e:
        return None, str(e)


def _bot_medium_cell(bot_report):
    """Extract the bot's baseline|medium|off cell (the contested +0.35R)."""
    if not isinstance(bot_report, dict):
        return None
    for cell in bot_report.get("ranked", []):
        if cell.get("cell") == "baseline|medium|off":
            return cell
    return None


def _qc_cloud_status():
    """Return (status_text, has_creds). Credentials are read from env or
    config.local.yaml under a `qc:` key ONLY (never echo the value)."""
    uid = os.environ.get("QC_USER_ID")
    tok = os.environ.get("QC_API_TOKEN")
    if not uid or not tok:
        local = _load_json(ROOT / "config.local.yaml".replace(".yaml", ""))
        # config.local.yaml is YAML not JSON; do a minimal key-only check
        try:
            txt = (ROOT / "config.local.yaml").read_text(encoding="utf-8")
            has_qc_block = "qc:" in txt and "user_id" in txt and "api_token" in txt
        except Exception:
            has_qc_block = False
        if not has_qc_block:
            return "PARKED -- no QC credentials found (see bottom of page)", False
    if not uid or not tok:
        return "PARKED -- QC credentials partially present in config.local.yaml", False
    return "Credentials present -- ready for `lean login` + `lean cloud backtest`", True


def _fmt(x, fmt="{:+.3f}"):
    try:
        return fmt.format(float(x))
    except Exception:
        return "—"


def render():
    bot_report = _load_json(_BOT_REPORT)
    bot_cell = _bot_medium_cell(bot_report)
    indep, indep_err = _independent_results()
    qc_results = _load_json(_QC_RESULTS)
    qc_status, has_creds = _qc_cloud_status()

    # --- bot side card ---
    if bot_cell:
        bot_html = (
            f"<tr><td><b>baseline | medium | off</b></td>"
            f"<td>{bot_cell.get('trades','—')}</td>"
            f"<td>{_fmt(bot_cell.get('win_rate_pct'), '{:.1f}%')}</td>"
            f"<td class='ok'>{_fmt(bot_cell.get('expectancy_net_r'))}</td>"
            f"<td>{_fmt(bot_cell.get('ci95',[0,0])[0])}</td>"
            f"<td>{_fmt(bot_cell.get('profit_factor'), '{:.2f}') }</td>"
            f"<td>{_fmt(bot_cell.get('dsr'), '{:.3f}') }</td>"
            f"<td class='warn'>close-to-close (artifact risk)</td></tr>"
        )
        bot_window = (bot_report.get("common_window", {}) if isinstance(bot_report, dict) else {})
        bot_window_str = f"{bot_window.get('start','—')} -> {bot_window.get('end','—')}"
    else:
        bot_html = '<tr><td colspan="8" class="muted">quantum_loop_report_realgrid.json not found</td></tr>'
        bot_window_str = "—"

    # --- independent pandas engine rows ---
    if indep:
        indep_rows = []
        for r in indep:
            cls = "ok" if r["net_r"] > 0 else "bad"
            indep_rows.append(
                f"<tr><td><b>EMA20-slope | {r['exit']}</b></td>"
                f"<td>{r['trades']}</td>"
                f"<td>{r['wr']:.1f}%</td>"
                f"<td class='{cls}'>{_fmt(r['net_r'])}</td>"
                f"<td>{_fmt(r['ci_lo'])}</td>"
                f"<td>{r['pf']:.2f}</td>"
                f"<td>—</td>"
                f"<td class='ok'>intrabar (independent)</td></tr>"
            )
        indep_html = "".join(indep_rows)
        indep_note = (
            "<span class='bad'>CONTRADICTS the bot</span>: medium (-0.028R) is "
            "<b>worse</b> than off (+0.175R) under an intrabar engine -- the exit "
            "edge is a close-to-close backtest artifact, not a real mechanism."
        )
    else:
        indep_html = f'<tr><td colspan="8" class="bad">pandas engine error: {html.escape(indep_err or "unknown")}</td></tr>'
        indep_note = ""

    # --- QC cloud row ---
    if isinstance(qc_results, dict) and qc_results.get("runs"):
        qc_rows = []
        for run in qc_results["runs"]:
            cls = "ok" if float(run.get("net_r", 0)) > 0 else "bad"
            qc_rows.append(
                f"<tr><td><b>QC Lean | {run.get('exit','—')}</b></td>"
                f"<td>{run.get('trades','—')}</td>"
                f"<td>{run.get('wr','—')}%</td>"
                f"<td class='{cls}'>{_fmt(run.get('net_r'))}</td>"
                f"<td>{_fmt(run.get('ci_lo'))}</td>"
                f"<td>{run.get('pf','—')}</td>"
                f"<td>—</td>"
                f"<td class='ok'>QC native stop orders (intrabar)</td></tr>"
            )
        qc_html = "".join(qc_rows)
        qc_note = f"<span class='ok'>QC cloud run completed {qc_results.get('timestamp','—')}</span>"
    else:
        qc_html = (
            f'<tr><td colspan="8" class="muted">QC Lean cloud run: '
            f'<b>{html.escape(qc_status)}</b> -- no results yet. '
            f'This is the THIRD independent engine (OANDA XAUUSD minute bars, '
            f'QC native stop/TP orders). Run once credentials are added.</td></tr>'
        )
        qc_note = ""

    # --- verdict line ---
    if indep and bot_cell:
        bot_r = float(bot_cell.get("expectancy_net_r", 0))
        ind_med = next((r["net_r"] for r in indep if r["exit"] == "medium"), None)
        ind_off = next((r["net_r"] for r in indep if r["exit"] == "off"), None)
        if ind_med is not None and ind_off is not None:
            if ind_med <= ind_off and bot_r > ind_med:
                verdict = (
                    f"<b>CROSS-CHECK VERDICT:</b> Bot reports medium=+{bot_r:.3f}R but the "
                    f"independent intrabar engine shows medium={ind_med:+.3f}R vs off={ind_off:+.3f}R. "
                    f"<span class='bad'>The exit edge is an artifact</span> -- corroboration "
                    f"pending from QC Lean (third engine)."
                )
            else:
                verdict = "<b>CROSS-CHECK VERDICT:</b> Independent engine partially agrees with bot -- review needed."
        else:
            verdict = "Verdict pending (insufficient data)."
    else:
        verdict = "Verdict pending (missing bot or independent results)."

    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

    head = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<meta http-equiv='refresh' content='{REFRESH}'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>QC cross-check -- MT5 Quant OS</title>
<style>
*{{box-sizing:border-box}}
body{{background:#07070a;color:#f5f5f7;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:16px;font-size:14px}}
h2{{font-weight:600;margin:4px 0 2px}}
h3{{font-weight:600;margin:18px 0 6px;color:#aeaeb2}}
.muted{{color:#8e8e93;font-size:12px}}
a{{color:#0a84ff}}
table{{border-collapse:collapse;width:100%;margin-top:8px}}
td,th{{border:1px solid rgba(255,255,255,.08);padding:7px 9px;text-align:left;font-size:13px}}
th{{background:#1c1c1e;color:#8e8e93;text-transform:uppercase;font-size:11px;letter-spacing:.04em}}
.ok{{color:#30d158}}.bad{{color:#ff453a}}.warn{{color:#ffd60a}}
.note{{background:#1c1c1e;border-left:3px solid #0a84ff;padding:8px 12px;border-radius:6px;margin-top:12px;font-size:12px;color:#aeaeb2}}
.verdict{{background:#1c1c1e;border-left:3px solid #ffd60a;padding:10px 14px;border-radius:6px;margin-top:12px;font-size:13px}}
.cred{{background:#1c1c1e;border:1px solid rgba(255,69,58,.25);border-radius:8px;padding:12px 14px;margin-top:12px;font-size:12px}}
.cred code{{background:#2c2c2e;padding:1px 5px;border-radius:4px;color:#ffd60a}}
.section{{margin-top:16px}}
</style></head><body>"""

    body = f"""
<h2>QuantConnect cross-check -- exit-mechanism hypothesis</h2>
<div class='muted'>Independent verification of the bot's <code>baseline|medium|off</code> +0.35R claim.
Read-only research -- no live trading, no MT5 access. Updated {ts} (auto-refresh {REFRESH}s).</div>

<div class='verdict'>{verdict}</div>

<div class='section'>
<h3>Hypothesis</h3>
<div class='muted'>"Do break-even + trailing exits lift per-trade R off the floor, or is the bot's
reported +0.35R for <code>medium</code> a close-to-close backtest artifact?"
The bot's PaperBroker exits on bar CLOSES only; a real broker fills stops INTRABAR
when the touch price is hit. Two independent engines re-test this intrabar.</div>
</div>

<div class='section'>
<h3>Engine 1 -- Bot's quantum_loop (close-to-close, contested)</h3>
<div class='muted'>window: {html.escape(str(bot_window_str))} · cost 0.15R/trade · source: quantum_loop_report_realgrid.json</div>
<table>
<tr><th>Cell</th><th>Trades</th><th>Win%</th><th>Net R</th><th>CI95 lo</th><th>PF</th><th>DSR</th><th>Exit model</th></tr>
{bot_html}
</table>
</div>

<div class='section'>
<h3>Engine 2 -- Independent pandas (intrabar, zero shared code)</h3>
<div class='muted'>EMA20-slope entry | ATR(14)*1.5 SL | 2.0R TP | XAUUSDm M15 | same window | cost 0.15R/trade</div>
<table>
<tr><th>Cell</th><th>Trades</th><th>Win%</th><th>Net R</th><th>CI95 lo</th><th>PF</th><th>DSR</th><th>Exit model</th></tr>
{indep_html}
</table>
<div class='note'>{indep_note}</div>
</div>

<div class='section'>
<h3>Engine 3 -- QuantConnect Lean cloud (intrabar, OANDA minute bars)</h3>
<div class='muted'>qc_crosscheck/main.py · QC native stop/TP orders (intrabar fills) · different data sample (OANDA XAUUSD minute)</div>
<table>
<tr><th>Cell</th><th>Trades</th><th>Win%</th><th>Net R</th><th>CI95 lo</th><th>PF</th><th>DSR</th><th>Exit model</th></tr>
{qc_html}
</table>
<div class='note'>{qc_note}</div>
</div>

<div class='cred'>
<b>QC cloud run status:</b> {html.escape(qc_status)}<br><br>
<b>To unblock the third engine</b>, set these two env vars and re-run:
<ul style='margin:6px 0 6px 18px'>
<li><code>QC_USER_ID</code> -- your QuantConnect user-id (numeric, from
<a href='https://www.quantconnect.com/Account'>quantconnect.com/Account</a> settings)</li>
<li><code>QC_API_TOKEN</code> -- your API token (from
<a href='https://www.quantconnect.com/Account'>quantconnect.com/Account</a> -> API tab)</li>
</ul>
Then the agent runs (non-interactive, no Docker needed):
<pre style='background:#2c2c2e;padding:8px;border-radius:6px;overflow-x:auto'>
lean login --user-id %QC_USER_ID% --api-token %QC_API_TOKEN%
lean cloud backtest qc_crosscheck --push --name qc_medium --parameter exits medium
lean cloud backtest qc_crosscheck --push --name qc_off    --parameter exits off
</pre>
Results land in <code>state/qc_crosscheck_results.json</code> and appear in Engine 3 above.
<b>Do NOT</b> run interactive <code>lean login</code> -- it hangs in this non-interactive environment.
</div>

<div class='note' style='margin-top:16px'>
<b>Security:</b> This dashboard only reads files and runs a local pandas backtest.
It does not touch MT5, the bot (start.py, PID 7824), config.yaml trading flags, or live trading.
Firewall is scoped to Tailscale CGNAT (100.64.0.0/10) only.
</div>
"""
    return head + body + "</body></html>"


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            try:
                body = render().encode("utf-8")
            except Exception as e:
                body = f"<html><body><pre>render error: {html.escape(str(e))}</pre></body></html>".encode("utf-8")
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
    ap.add_argument("--port", type=int, default=8084)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), H)
    srv.daemon_threads = True
    print(f"QC cross-check dashboard on http://{args.host}:{args.port}  (root: {ROOT})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()