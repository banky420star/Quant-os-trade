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
        return str(x) if x is not None else "-"


def _money(x):
    try:
        return "${:.2f}".format(float(x))
    except Exception:
        return "-"


def _pct(x):
    try:
        return "{:+.2f}%".format(float(x))
    except Exception:
        return "-"


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
    bg = _load("blue_guardian.json") or {}
    sup = _load("supervisor.json") or {}
    rstate = _load("risk_state.json") or {}
    cult = _load("forward_test_ledger.json") or {}
    veto = _load("symbol_policy_live.json") or {}
    tlog = _load("trade_log.json") or {}
    sltp = _load("symbol_sltp_live.json") or {}
    betr = _load("symbol_be_trail_live.json") or {}
    rr = _load("research_report.json") or {}
    rv = _load("research_validation.json") or {}

    bal = acc.get("balance")
    eq = acc.get("equity")
    try:
        unrl = float(eq) - float(bal)
    except Exception:
        unrl = None

    positions = pos.get("positions", []) if isinstance(pos, dict) else []
    kill = bool(ks.get("kill_switch")) if isinstance(ks, dict) else False
    hstatus = hlt.get("status", "-") if isinstance(hlt, dict) else "-"

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
    ap_n = len(ap) if isinstance(ap, list) else (len(ap.get("signals", [])) if isinstance(ap, dict) and "signals" in ap else "-")
    rej_n = len(rej) if isinstance(rej, list) else (len(rej.get("signals", [])) if isinstance(rej, dict) and "signals" in rej else "-")

    # supervisor loop timestamps
    sup_lines = []
    if isinstance(sup, dict):
        raw_loops = sup.get("loops", [])
        loops = []
        if isinstance(raw_loops, list):
            loops = [lp for lp in raw_loops if isinstance(lp, dict)]
        elif isinstance(raw_loops, dict):
            for name, lp in raw_loops.items():
                if isinstance(lp, dict):
                    item = dict(lp)
                    item.setdefault("name", name)
                    loops.append(item)
        for lp in loops:
            name = str(lp.get("name", "")).strip() or "loop"
            ts = lp.get("last_run") or lp.get("last_complete") or lp.get("timestamp")
            status = str(lp.get("status", "")).strip()
            if ts or status:
                bits = [f"<b>{html.escape(name)}</b>"]
                if status:
                    bits.append(html.escape(status))
                if ts:
                    bits.append(html.escape(str(ts)))
                sup_lines.append(f"<span class='chip'>{' '.join(bits)}</span>")

    tipscale = rem.get("tailscale_ip") if isinstance(rem, dict) else None

    card = (
        "<div class='k'><b>{lbl}</b><span class='{cls}'>{val}</span></div>"
    )

    def K(lbl, val, cls=""):
        return f"<div class='k'><b>{html.escape(lbl)}</b><span class='{cls}'>{val}</span></div>"

    head = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<meta http-equiv='refresh' content='{REFRESH}'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>MT5 Quant OS - live (no-JS)</title>
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
.swipe{{overflow-x:auto;-webkit-overflow-scrolling:touch;width:100%}}
.swipe>table{{min-width:100%}}
@media(max-width:640px){{body{{font-size:12px;margin:10px}}td,th{{font-size:11px;padding:4px 5px}}.k{{min-width:90px}}.k span{{font-size:16px}}}}
</style></head><body>"""

    body = f"""
<h2>MT5 Quant OS - live status</h2>
<div class='muted'>account <b>{acc.get('login','-')}</b> - {acc.get('server','-')} - mode=<b>{acc.get('account_mode','-')}</b>
- runtime=<b>{rt.get('label','-')}</b> - updated {time.strftime('%H:%M:%S')} (auto-refresh {REFRESH}s, no JS)</div>

<div class='wrap'>
{K('Balance', _money(bal))}
{K('Equity', _money(eq), _cls_pos(unrl) if unrl is not None else '')}
{K('Unrealized P/L', _money(unrl), _cls_pos(unrl) if unrl is not None else '')}
{K('Kill switch', 'ON' if kill else 'OFF', 'bad' if kill else 'ok')}
{K('Health', html.escape(str(hstatus)), 'ok' if hstatus=='healthy' else 'bad')}
{K('Open positions', str(len(positions)))}
{K('BG day P/L', _money(bg.get('daily_pnl')) if isinstance(bg, dict) and bg.get('enabled') else '-', _cls_pos(bg.get('daily_pnl')) if isinstance(bg, dict) else '')}
{K('BG target', '+6% ($300)' if isinstance(bg, dict) and bg.get('enabled') else '-')}
{K('BG paused', 'YES' if isinstance(bg, dict) and bg.get('trading_paused') else 'no', 'bad' if isinstance(bg, dict) and bg.get('trading_paused') else 'ok')}
{K('Approved signals', str(ap_n))}
{K('Rejected signals', str(rej_n))}
</div>

<div class='section'>
<h3 style='margin:0 0 4px'>Open positions ({len(positions)})</h3>
<div class='swipe'><table><tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Size</th><th>P/L</th><th>SL</th><th>TP</th><th>Setup</th><th>Ticket</th></tr>
{''.join(rows)}
</table></div>
</div>

<div class='section'>
<h3 style='margin:0 0 6px'>Supervisor loops</h3>
{''.join(sup_lines) if sup_lines else '<span class="muted">no loop timestamps</span>'}
</div>

{(_growth_card(dg) )}

{(_research_card(rr, rv) )}

{(_culturing_card(cult, veto) )}

{(_sltp_card(sltp) )}

{(_betrail_card(betr) )}

{(_trade_log_card(tlog) )}

<div class='note'>
<b>This is the reliable no-JS mirror.</b> It reads the bot's state files directly and refreshes every {REFRESH}s.
The full SPA is at <a href='http://localhost:8080/'>localhost:8080</a>
{(' - phone/Tailscale: <a href="http://'+str(tipscale)+':8081/">'+str(tipscale)+':8081</a>') if tipscale else ''}.
Bot is untouched - this only reads files.
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
<div class='muted'>target {tgt:.1f}%/day - start ${start:.2f} - now ${cur:.2f} - session {_pct(pct)} of day-target</div>
<div class='bar'><i style='width:{bar:.1f}%'></i></div>
</div>"""
    except Exception:
        return ""


def _research_card(rr, rv):
    if not isinstance(rr, dict) or not rr:
        return ""
    try:
        status = html.escape(str(rr.get("status", "-")))
        reason = html.escape(str(rr.get("status_reason") or "-"))
        summary = rr.get("summary", {}) if isinstance(rr.get("summary"), dict) else {}
        edge_records = rr.get("edge_records", "-")
        ctx_cells = summary.get("context_cells", "-")
        setup_cells = summary.get("setup_cells", "-")
        pattern_count = summary.get("pattern_count", len(rr.get("patterns", [])) if isinstance(rr.get("patterns"), list) else "-")
        val_status = html.escape(str(rv.get("status", "-"))) if isinstance(rv, dict) else "-"
        proposal = html.escape(str(rv.get("proposal") or "-")) if isinstance(rv, dict) else "-"
        return f"""<div class='section'>
<h3 style='margin:0 0 6px'>Research engine</h3>
<div class='muted'>status <b>{status}</b> - {reason} - edge records <b>{edge_records}</b> - context cells <b>{ctx_cells}</b> - setup cells <b>{setup_cells}</b> - patterns <b>{pattern_count}</b> - validation <b>{val_status}</b> - proposal <b>{proposal}</b></div>
</div>"""
    except Exception:
        return ""


def _culturing_card(ledger, policy):
    """No-JS mirror of the culturing ledger + data-driven veto (forward_test_loop)."""
    if not isinstance(ledger, dict) or not ledger:
        return ""
    try:
        cells = ledger.get("cells", {}) if isinstance(ledger.get("cells"), dict) else {}
        symbols = policy.get("symbols", {}) if isinstance(policy, dict) and isinstance(policy.get("symbols"), dict) else {}
        total = int(ledger.get("total_cells", 0) or 0)
        vetoed = int(ledger.get("total_vetoed", 0) or 0)
        cfg = ledger.get("config", {}) or {}
        min_n = cfg.get("min_n", "-")
        vwr = cfg.get("veto_win_rate_pct", "-")
        if not cells:
            return (f"<div class='section'><h3 style='margin:0 0 6px'>Culturing ledger - data-driven veto</h3>"
                    f"<div class='muted'>no cells yet (min_n={min_n}, veto when wr&lt;{vwr}% &amp; netR&lt;0)</div></div>")
        rows = []
        for sym, sym_cells in cells.items():
            v_cells = (symbols.get(sym, {}) or {}).get("vetoed_cells", []) or []
            v_set = set(v_cells)
            entries = []
            for cell, st in sym_cells.items():
                if not isinstance(st, dict):
                    continue
                try:
                    entries.append((float(st.get("expectancy_net_r", 0)), cell, st))
                except (TypeError, ValueError):
                    continue
            entries.sort(key=lambda r: r[0])
            worst = entries[0] if entries else None
            n_cells = len(sym_cells)
            v_n = len(v_set)
            wc = "-"
            if worst:
                wnet, wcell, wst = worst
                wc = html.escape(wcell if len(wcell) <= 44 else wcell[:41] + "...")
                wc += (f" - n={wst.get('n',0)} wr={int(float(wst.get('win_rate_pct',0) or 0))}% "
                       f"netR={_pct(wnet*100)} avgL={_money(wst.get('avg_loss_usd'))} "
                       f"maxL={_money(wst.get('max_loss_usd'))} br25={wst.get('breach_25_count',0)} "
                       f"[{wst.get('verdict','')}]")
            vlist = "".join(f"<li class='bad'>œ* {html.escape(c if len(c)<=60 else c[:57]+'...')}</li>" for c in sorted(v_set)[:5])
            rows.append(
                f"<tr><td><b>{html.escape(str(sym))}</b><br><span class='muted'>cells={n_cells} vetoed={v_n}</span></td>"
                f"<td class='muted'>{wc}</td>"
                f"<td><ul style='margin:0;padding-left:18px'>{vlist}</ul></td></tr>"
            )
        return f"""<div class='section'>
<h3 style='margin:0 0 6px'>Culturing ledger - data-driven veto</h3>
<div class='muted'>cells <b>{total}</b> - vetoed <b style='color:#ff453a'>{vetoed}</b> - min_n={min_n} - veto when wr&lt;{vwr}% &amp; netR&lt;0</div>
<table><tr><th>Symbol</th><th>Worst cell (by net R)</th><th>Vetoed cells</th></tr>
{''.join(rows)}
</table></div>"""
    except Exception:
        return ""


def _sltp_card(sltp):
    """No-JS mirror of the per-symbol SL/TP calibration (state/symbol_sltp_live.json).

    Shows the data-driven auto-tune status per symbol: the in-sample best
    (k_sl, RR, expectancy) and whether it is APPLIED (trusted) or just
    recorded (thin data / ci95<=0 -> seeded values stay active).
    """
    if not isinstance(sltp, dict) or not sltp.get("symbols"):
        return ("<div class='section'><h3 style='margin:0 0 6px'>SL/TP calibration - per symbol</h3>"
                "<div class='muted'>no data-driven calibration yet (seeds from config active; "
                "run scripts/calibrate_sltp.py)</div></div>")
    try:
        rows = []
        for sym, s in sltp.get("symbols", {}).items():
            if not isinstance(s, dict):
                continue
            trusted = bool(s.get("trusted"))
            n = s.get("n", "-")
            exp = s.get("expectancy_r")
            seed_exp = s.get("seed_expectancy_r")
            ci = s.get("ci95") or [None, None]
            exp_s = "-" if exp is None else f"{float(exp):+.3f}R"
            seed_s = "-" if seed_exp is None else f"{float(seed_exp):+.3f}R"
            ci_s = "-" if ci[0] is None else f"[{float(ci[0]):+.3f},{float(ci[1]):+.3f}]"
            applied = "<b class='ok'>APPLIED</b>" if trusted else f"<span class='muted'>recorded ({html.escape(str(s.get('reason','')))})</span>"
            rows.append(
                f"<tr><td><b>{html.escape(str(sym))}</b></td><td class='muted'>{n}</td>"
                f"<td>{s.get('sl_atr_mult','-')}</td><td>{s.get('tp1_rr','-')}/{s.get('tp2_rr','-')}</td>"
                f"<td>{exp_s}</td><td class='muted'>seed {seed_s}</td>"
                f"<td class='muted'>{ci_s}</td><td>{applied}</td></tr>"
            )
        return f"""<div class='section'>
<h3 style='margin:0 0 6px'>SL/TP calibration - per symbol (data-driven auto-tune)</h3>
<div class='muted'>seeded values from config active unless a row is <b class='ok'>APPLIED</b>; auto-tune only applies when n&gt;=50 AND beats seed AND ci95 lo&gt;0</div>
<div class='swipe'><table style='font-size:11px'><tr><th>Symbol</th><th>n</th><th>sl_atr</th><th>tp1/tp2 RR</th><th>best exp</th><th>seed exp</th><th>ci95</th><th>status</th></tr>
{''.join(rows)}
</table></div></div>"""
    except Exception:
        return ""


def _betrail_card(betr):
    """No-JS mirror of the per-symbol break-even/trailing calibration."""
    if not isinstance(betr, dict) or not betr.get("symbols"):
        return ("<div class='section'><h3 style='margin:0 0 6px'>Break-even / trailing calibration - per symbol</h3>"
                "<div class='muted'>no data-driven calibration yet (seeds from config active; "
                "run scripts/calibrate_be_trail.py)</div></div>")
    try:
        rows = []
        for sym, s in betr.get("symbols", {}).items():
            if not isinstance(s, dict):
                continue
            trusted = bool(s.get("trusted"))
            be = s.get("break_even") or {}
            tr = s.get("trailing") or {}
            exp = s.get("expectancy_r"); seed_exp = s.get("seed_expectancy_r")
            ci = s.get("ci95") or [None, None]
            exp_s = "-" if exp is None else f"{float(exp):+.3f}R"
            seed_s = "-" if seed_exp is None else f"{float(seed_exp):+.3f}R"
            ci_s = "-" if ci[0] is None else f"[{float(ci[0]):+.3f},{float(ci[1]):+.3f}]"
            applied = "<b class='ok'>APPLIED</b>" if trusted else f"<span class='muted'>recorded ({html.escape(str(s.get('reason','')))})</span>"
            rows.append(
                f"<tr><td><b>{html.escape(str(sym))}</b></td><td class='muted'>{s.get('n','-')}</td>"
                f"<td>{be.get('trigger_atr_mult','-')}/{be.get('lock_profit_atr_mult','-')}</td>"
                f"<td>{tr.get('activation_atr_mult','-')}/{tr.get('trail_atr_mult','-')}</td>"
                f"<td>{exp_s}</td><td class='muted'>seed {seed_s}</td>"
                f"<td class='muted'>{ci_s}</td><td>{applied}</td></tr>"
            )
        return f"""<div class='section'>
<h3 style='margin:0 0 6px'>Break-even / trailing calibration - per symbol</h3>
<div class='muted'>seeds active unless <b class='ok'>APPLIED</b>; auto-tune applies only when n&gt;=50 AND beats seed AND ci95 lo&gt;0 (path-unknown model -> conservative)</div>
<div class='swipe'><table style='font-size:11px'><tr><th>Symbol</th><th>n</th><th>BE trig/lock</th><th>trail act/dist</th><th>best exp</th><th>seed exp</th><th>ci95</th><th>status</th></tr>
{''.join(rows)}
</table></div></div>"""
    except Exception:
        return ""


def _trade_log_card(tlog):
    """No-JS mirror of the comprehensive per-trade log (state/trade_log.json).

    The user's "whole works" record: open/close times, win/loss, setup,
    drawdown (MAE), run-up (MFE), R-multiple, regime/session/bias. Shows the
    summary + the most recent 25 closed trades.
    """
    if not isinstance(tlog, dict) or not tlog:
        return ""
    try:
        total = int(tlog.get("total", 0) or 0)
        if not total:
            return "<div class='section'><h3 style='margin:0 0 6px'>Trade log</h3><div class='muted'>no closed trades yet</div></div>"
        wins = int(tlog.get("wins", 0) or 0)
        losses = int(tlog.get("losses", 0) or 0)
        wr = tlog.get("win_rate_pct", "-")
        pnl = tlog.get("total_pnl", 0)
        avg_r = tlog.get("avg_R")
        r_s = "-" if avg_r is None else f"{float(avg_r):+.3f}R"
        ks = tlog.get("kelly") or {}
        ks_sized = int(ks.get("sized_trades", 0) or 0)
        ks_fb = int(ks.get("fallback_trades", 0) or 0)
        trades = tlog.get("trades", []) if isinstance(tlog.get("trades"), list) else []
        rows = []
        for t in trades[:25]:
            sym = html.escape(str(t.get("symbol") or "-"))
            side = html.escape(str(t.get("side") or "-"))
            setup = html.escape(str(t.get("setup") or "-"))
            res = str(t.get("result") or "-")
            res_cls = "ok" if res == "win" else "bad"
            pnl_v = t.get("pnl")
            try:
                pnl_s = f"${float(pnl_v):+.2f}"
            except Exception:
                pnl_s = "-"
            r = t.get("r_multiple")
            r_s2 = "-" if r is None else f"{float(r):+.2f}R"
            mae = t.get("mae_R")
            mae_s = "-" if mae is None else f"{float(mae):.2f}R"
            mfe = t.get("mfe_R")
            mfe_s = "-" if mfe is None else f"{float(mfe):.2f}R"
            opened = html.escape(str(t.get("opened_at") or "-")[5:16].replace("T", " "))
            closed = html.escape(str(t.get("closed_at") or "-")[5:16].replace("T", " "))
            hold = html.escape(str(t.get("hold_human") or "-"))
            conf = t.get("confidence") or "-"
            # Kelly verdict for this trade's cell: fraction (risk-%) + reason.
            k = t.get("kelly") if isinstance(t.get("kelly"), dict) else None
            if k:
                kcls = "ok" if not k.get("gated") else "muted"
                kfrac = k.get("fraction")
                kfrac_s = "-" if kfrac is None else f"{float(kfrac):.2f}%"
                kreason = html.escape(str(k.get("reason") or ""))
                k_s = f"<span class='{kcls}'>{kfrac_s}</span><br><span class='muted' style='font-size:10px'>{kreason}</span>"
            else:
                k_s = "<span class='muted'>-</span>"
            rows.append(
                f"<tr><td class='muted'>{opened}</td><td class='muted'>{closed}</td>"
                f"<td>{hold}</td><td><b>{sym}</b></td><td>{side}</td><td>{setup}</td>"
                f"<td class='muted'>{html.escape(str(t.get('regime_primary') or '-'))}</td>"
                f"<td class='muted'>{html.escape(str(t.get('session') or '-'))}</td>"
                f"<td>{conf}</td><td class='{res_cls}'>{res}</td>"
                f"<td class='{_cls_pos(pnl_v)}'>{pnl_s}</td><td class='{_cls_pos(r)}'>{r_s2}</td>"
                f"<td class='bad'>{mae_s}</td><td class='ok'>{mfe_s}</td>"
                f"<td>{k_s}</td></tr>"
            )
        return f"""<div class='section'>
<h3 style='margin:0 0 6px'>Trade log - every closed trade (open/close, win/loss, setup, drawdown, R, Kelly)</h3>
<div class='muted'>trades <b>{total}</b> - wins <b class='ok'>{wins}</b> - losses <b class='bad'>{losses}</b> - win% <b>{wr}</b> - net PnL <b class='{_cls_pos(pnl)}'>${float(pnl):+.2f}</b> - avg R <b class='{_cls_pos(avg_r)}'>{r_s}</b> - Kelly sized-up <b class='ok'>{ks_sized}</b> / fallback <b class='muted'>{ks_fb}</b> - opened_at known {tlog.get('opened_at_known','-')} - drawdown known {tlog.get('drawdown_known','-')}</div>
<div class='swipe'><table style='font-size:11px'><tr><th>Opened</th><th>Closed</th><th>Hold</th><th>Symbol</th><th>Side</th><th>Setup</th><th>Regime</th><th>Session</th><th>Conf</th><th>Result</th><th>PnL</th><th>R</th><th>MAE(R)</th><th>MFE(R)</th><th>Kelly</th></tr>
{''.join(rows)}
</table></div>
<div class='muted' style='margin-top:4px'>showing most recent 25 of {total} trades - full log with all fields (entry/exit/SL/TP/size/tags/exit_reason) on the SPA Trade Log tab</div>
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


