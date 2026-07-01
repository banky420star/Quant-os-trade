"""Terminal (TUI) view of the whole MT5 Quant OS process running.

A live, ANSI-refreshing dashboard that mirrors the web UI but runs in the
terminal. READ-ONLY: it reads the same state/*.json files the bot writes
continuously (no HTTP round-trip, no trading logic, nothing mutated). Safe to
run alongside the bot on demo or real.

Run (from repo root, or anywhere -- it resolves state/ by __file__):
    python scripts/terminal_view.py                 # default, refresh 2s
    python scripts/terminal_view.py --interval 1    # faster
    python scripts/terminal_view.py --once          # single frame, then exit
    python scripts/terminal_view.py --no-color      # plain text

Ctrl+C to quit. Works on Windows Terminal / cmd (enables VT processing) and
any POSIX terminal.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "state"

# ---- ANSI -----------------------------------------------------------------
USE_COLOR = True


def _c(code: str) -> str:
    return code if USE_COLOR else ""


R = _c("\033[0m")
B = _c("\033[1m")
DIM = _c("\033[2m")
RED = _c("\033[31m")
GREEN = _c("\033[32m")
YELLOW = _c("\033[33m")
BLUE = _c("\033[34m")
MAGENTA = _c("\033[35m")
CYAN = _c("\033[36m")
GREY = _c("\033[90m")


def _enable_vt() -> None:
    """Enable ANSI VT processing on Windows consoles (no-op elsewhere)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


# ---- state loading --------------------------------------------------------
def _load(name: str):
    try:
        with open(STATE / name, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _fnum(x, fmt="{:.2f}") -> str:
    try:
        return fmt.format(float(x))
    except Exception:
        return "—" if x is None else str(x)


def _money(x) -> str:
    try:
        return "${:.2f}".format(float(x))
    except Exception:
        return "—"


def _signed(x) -> str:
    try:
        return "{:+.2f}".format(float(x))
    except Exception:
        return "—"


def _age_seconds(iso_ts) -> float | None:
    if not iso_ts:
        return None
    try:
        s = str(iso_ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return None


def _age_str(iso_ts) -> str:
    a = _age_seconds(iso_ts)
    if a is None:
        return "—"
    if a < 60:
        return f"{a:.0f}s"
    if a < 3600:
        return f"{a/60:.1f}m"
    return f"{a/3600:.1f}h"


def _colored_status(status: str) -> str:
    s = str(status).lower()
    if s in ("ok", "healthy", "complete", "alive", "true", "fresh"):
        return f"{GREEN}{status}{R}"
    if s in ("error", "fail", "failed", "down", "stale", "false", "paused"):
        return f"{RED}{status}{R}"
    if s in ("warn", "warning", "slow", "pending"):
        return f"{YELLOW}{status}{R}"
    return f"{GREY}{status}{R}"


def _bar(pct: float, width: int = 24) -> str:
    p = max(0.0, min(100.0, pct))
    filled = int(round(p / 100.0 * width))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


# ---- sections -------------------------------------------------------------
def _header(acc, sup, rt) -> str:
    login = acc.get("login", "—")
    server = acc.get("server", "—")
    mode = acc.get("account_mode", "—")
    mode_col = GREEN if str(mode).lower() == "demo" else RED + B
    label = rt.get("label", "—") if isinstance(rt, dict) else "—"
    up = sup.get("uptime_seconds") if isinstance(sup, dict) else None
    up_s = f"{int(up)//3600}h{(int(up)%3600)//60}m" if isinstance(up, (int, float)) else "—"
    now = datetime.now().strftime("%H:%M:%S")
    line1 = f"{B}{CYAN}MT5 QUANT OS{R} {DIM}— live terminal view{R}"
    line2 = (f"{GREY}account{R} {B}{login}{R} {GREY}·{R} {server} {GREY}·{R} "
             f"mode={mode_col}{mode}{R} {GREY}·{R} runtime={B}{label}{R} "
             f"{GREY}·{R} uptime={up_s} {GREY}·{R} {DIM}{now}{R}")
    return line1 + "\n" + line2 + "\n" + GREY + "─" * 92 + R


def _kpi_row(acc, dg, ks, hlt, sup, pos) -> str:
    bal = acc.get("balance")
    eq = acc.get("equity")
    try:
        unrl = float(eq) - float(bal)
    except Exception:
        unrl = None
    unrl_col = GREEN if (unrl is not None and unrl >= 0) else RED
    kill = bool(ks.get("kill_switch")) if isinstance(ks, dict) else False
    hstatus = hlt.get("status", "—") if isinstance(hlt, dict) else "—"
    npos = len(pos.get("positions", [])) if isinstance(pos, dict) else 0
    day_pct = dg.get("daily_pnl_pct") if isinstance(dg, dict) else None
    day_col = GREEN if (isinstance(day_pct, (int, float)) and day_pct >= 0) else RED
    sysres = sup.get("system", {}) if isinstance(sup, dict) else {}
    cpu = sysres.get("cpu_pct")
    mem = sysres.get("memory_pct")

    def k(label, val, col=""):
        return f"{GREY}{label:<11}{R}{col}{val}{R}"

    cells = [
        k("balance", _money(bal)),
        k("equity", _money(eq)),
        k("unrealized", _signed(unrl) if unrl is not None else "—", unrl_col),
        k("day P&L", f"{_signed(day_pct)}%" if day_pct is not None else "—", day_col),
        k("kill", "ON" if kill else "OFF", RED + B if kill else GREEN),
        k("health", hstatus, GREEN if str(hstatus).lower() == "healthy" else RED),
        k("open", str(npos)),
        k("cpu", f"{_fnum(cpu, '{:.0f}')}%" if cpu is not None else "—"),
        k("mem", f"{_fnum(mem, '{:.0f}')}%" if mem is not None else "—"),
    ]
    return "  ".join(cells)


def _growth_section(dg, baseline) -> str:
    if not isinstance(dg, dict) or not dg.get("enabled"):
        return f"{GREY}growth campaign: disabled{R}"
    tgt = float(dg.get("target_pct", 0) or 0)
    pct = float(dg.get("daily_pnl_pct", 0) or 0)
    start = float(dg.get("day_start_equity", 0) or 0)
    cur = float(dg.get("current_equity", 0) or 0)
    remaining = float(dg.get("remaining_pct", 0) or 0)
    hit = bool(dg.get("target_hit"))
    paused = bool(dg.get("trading_paused"))
    bar_pct = (pct / tgt * 100.0) if tgt else 0.0
    col = GREEN if pct >= 0 else RED
    head = (f"{B}growth campaign{R} {DIM}day {dg.get('day','—')}{R}  "
            f"target {B}{tgt:.0f}%/day{R}  start {_money(start)}  now {_money(cur)}  "
            f"{col}session {_signed(pct)}%{R}  remaining {YELLOW}{_signed(remaining)}%{R}")
    flag = ""
    if hit:
        flag = f"  {GREEN}{B}TARGET HIT{R}"
    if paused:
        flag += f"  {RED}{B}PAUSED{R}"
    bar = f"  {col}{_bar(bar_pct)}{R} {GREY}{bar_pct:.0f}% of day-target{R}"
    # also show vs original baseline starting_cash (the $95/$100 reference)
    sc = baseline.get("starting_cash") if isinstance(baseline, dict) else None
    extra = ""
    if sc:
        try:
            vs = (float(cur) / float(sc) - 1.0) * 100.0
            vcol = GREEN if vs >= 0 else RED
            extra = f"  {GREY}vs orig start {_money(sc)}: {vcol}{_signed(vs)}%{R}"
        except Exception:
            pass
    return head + flag + "\n" + bar + extra


def _positions_section(pos, pmgr, conf) -> str:
    positions = pos.get("positions", []) if isinstance(pos, dict) else []
    cmap = conf if isinstance(conf, dict) else {}
    pmap = pmgr.get("positions", {}) if isinstance(pmgr, dict) else {}
    header = f"{B}open positions{R} {GREY}({len(positions)}){R}"
    if not positions:
        return header + "\n" + f"  {GREY}no open positions{R}"
    colhead = (f"  {GREY}{'symbol':<10}{'side':<6}{'entry':>10}{'size':>7}{'P/L':>9}"
               f"{'SL':>10}{'TP':>10}{'conf':>6}{'setup':<16}{'BE/Tr':<7}{'ticket':<12}{R}")
    rows = [colhead]
    for p in positions:
        sym = str(p.get("symbol", ""))[:10]
        side = str(p.get("side", ""))[:5]
        side_col = RED if side.upper() == "SELL" else GREEN
        entry = _fnum(p.get("entry"))
        size = _fnum(p.get("size"))
        pr = p.get("profit")
        pr_col = GREEN if (isinstance(pr, (int, float)) and pr >= 0) else RED
        # color OUTSIDE the width padding so the column aligns under the header
        pl_plain = _signed(pr) if pr is not None else "—"
        pl = f"{pr_col}{pl_plain:>9}{R}"
        sl = _fnum(p.get("sl"))
        tp = _fnum(p.get("tp1") or p.get("tp"))
        ticket = str(p.get("ticket", ""))
        cval = cmap.get(str(ticket)) or cmap.get(ticket)
        conf_s = f"{cval:.0f}%" if isinstance(cval, (int, float)) else "—"
        setup = str(p.get("setup_type", ""))[:15]
        pm = pmap.get(str(ticket)) or pmap.get(ticket) or {}
        flags = []
        if pm.get("break_even"):
            flags.append("BE")
        if pm.get("trailing"):
            flags.append("Tr")
        betr = "/".join(flags) or "—"
        row = (f"  {sym:<10}{side_col}{side:<6}{R}{entry:>10}{size:>7}{pl}"
               f"{sl:>10}{tp:>10}{conf_s:>6}  {setup:<16}{betr:<7}{ticket:<12}")
        rows.append(row)
    return header + "\n" + "\n".join(rows)


def _signal_flow_section(approved, rejected) -> str:
    cand = (approved.get("candidate_count") if isinstance(approved, dict) else None) or \
           (rejected.get("candidate_count") if isinstance(rejected, dict) else None)
    a_n = (approved.get("count") if isinstance(approved, dict) else None) or 0
    r_n = (rejected.get("count") if isinstance(rejected, dict) else None) or 0
    rej = rejected.get("rejected", []) if isinstance(rejected, dict) else []
    head = (f"{B}signal flow (reviewer/filter){R}  "
            f"{GREY}candidates{R} {B}{cand if cand is not None else '—'}{R}  "
            f"{GREEN}approved{R} {B}{a_n}{R}  "
            f"{RED}rejected{R} {B}{r_n}{R}")
    lines = [head]
    if rej:
        lines.append(f"  {GREY}latest rejections (which gates fired):{R}")
        for s in rej[-4:]:
            sym = str(s.get("symbol", ""))
            side = str(s.get("side", ""))
            setup = str(s.get("setup_type", ""))
            conf = s.get("confidence")
            codes = s.get("failure_codes", []) or []
            reason = str(s.get("rejection_reason") or s.get("reason") or "")
            if len(reason) > 70:
                reason = reason[:67] + "..."
            codes_s = ", ".join(codes)
            lines.append(f"    {RED}{sym} {side}{R} {DIM}{setup}{R} "
                         f"{GREY}conf={conf}%{R}  {YELLOW}{codes_s}{R}")
            if reason:
                lines.append(f"      {GREY}{reason}{R}")
    else:
        lines.append(f"  {GREY}no rejections in current cycle{R}")
    return "\n".join(lines)


def _loops_section(sup) -> str:
    loops = sup.get("loops", []) if isinstance(sup, dict) else []
    if not loops:
        return f"{B}supervisor loops{R}\n  {GREY}no loop data{R}"
    lines = [f"{B}supervisor loops{R} {GREY}({len(loops)} loops, run_count {loops[0].get('run_count','—')}){R}"]
    # pack 2 per line
    for i in range(0, len(loops), 2):
        pair = []
        for lp in loops[i:i + 2]:
            name = str(lp.get("name", ""))
            st = str(lp.get("status", ""))
            res = str(lp.get("last_result", ""))
            age = _age_str(lp.get("last_run"))
            stcol = GREEN if st.lower() == "ok" else RED
            rescol = GREEN if res.lower() in ("ok", "complete") else (YELLOW if res and res.lower() not in ("ok", "complete") else GREY)
            pair.append(f"  {name:<22}{stcol}{st:<4}{R} {rescol}{res:<9}{R} {GREY}{age:>6}{R}")
        lines.append(" ".join(pair))
    return "\n".join(lines)


def _services_section(sup) -> str:
    services = sup.get("services", []) if isinstance(sup, dict) else []
    if not services:
        return ""
    lines = [f"{B}services{R}"]
    for s in services:
        name = str(s.get("label") or s.get("name", ""))
        st = str(s.get("status", ""))
        stcol = GREEN if st.lower() == "ok" else RED
        rc = s.get("run_count", "—")
        err = s.get("error_count", 0)
        errcol = GREEN if not err else RED
        dur = _fnum(s.get("last_duration_ms"), "{:.0f}")
        iv = s.get("interval_seconds", "—")
        age = _age_str(s.get("last_run"))
        lines.append(f"  {name:<20}{stcol}{st:<5}{R} runs={rc:<5} err={errcol}{err}{R}  "
                     f"{GREY}last {dur}ms / every {iv}s / {age} ago{R}")
    return "\n".join(lines)


def _market_section(mctx) -> str:
    if not isinstance(mctx, dict):
        return ""
    syms = mctx.get("market_context", {}).get("symbols", {}) if isinstance(mctx.get("market_context"), dict) else {}
    if not syms:
        return ""
    lines = [f"{B}market regime{R}"]
    for sym, d in syms.items():
        mr = d.get("market_regime", {}) if isinstance(d, dict) else {}
        primary = mr.get("primary", "—")
        bias = mr.get("bias", "—")
        tradeable = mr.get("tradeable", False)
        session = d.get("session", "—") if isinstance(d, dict) else "—"
        tcol = GREEN if tradeable else RED
        bcol = GREEN if str(bias).lower() == "bullish" else (RED if str(bias).lower() == "bearish" else GREY)
        lines.append(f"  {sym:<10} {primary:<14}{bcol}bias={bias:<9}{R} "
                     f"session={session:<11} tradeable={tcol}{'yes' if tradeable else 'no'}{R}")
    return "\n".join(lines)


def _mt5_section(hlt) -> str:
    if not isinstance(hlt, dict):
        return ""
    conn = hlt.get("connection", {}) or {}
    candles = hlt.get("candles", {}) or {}
    mt5 = hlt.get("mt5", {}) or {}
    alive = conn.get("alive")
    logged = conn.get("logged_in")
    lat = conn.get("latency_ms")
    fresh = candles.get("fresh")
    age = candles.get("age_minutes")
    sysres = hlt.get("system", {}) or {}
    disk = sysres.get("disk_free_gb")
    lines = [f"{B}MT5 connection{R}"]
    a_col = GREEN if alive else RED
    l_col = GREEN if logged else RED
    fr_col = GREEN if fresh else RED
    lines.append(f"  alive={a_col}{alive}{R} logged_in={l_col}{logged}{R} "
                 f"latency={lat}ms  candles={fr_col}{'fresh' if fresh else 'stale'}{R} "
                 f"({age}m ago)  {GREY}disk {_fnum(disk,'{:.0f}')}GB free{R}")
    procs = mt5.get("processes", []) or []
    if procs:
        ps = "  ".join(f"pid {p.get('pid')} {'alive' if p.get('alive') else 'dead'}" for p in procs)
        lines.append(f"  {GREY}terminals: {ps}{R}")
    return "\n".join(lines)


# ---- frame ----------------------------------------------------------------
def render_frame() -> str:
    acc = _load("account.json") or {}
    sup = _load("supervisor.json") or {}
    hlt = _load("health.json") or {}
    ks = _load("kill_switch.json") or {}
    dg = _load("daily_growth.json") or {}
    rt = _load("runtime_mode.json") or {}
    baseline = _load("mt5_baseline.json") or {}
    pos = _load("paper_positions.json") or {}
    pmgr = _load("position_management.json") or {}
    conf = _load("position_confidence.json") or {}
    mctx = _load("market_context.json") or {}
    approved = _load("approved_signals.json") or {}
    rejected = _load("rejected_signals.json") or {}

    parts = [
        _header(acc, sup, rt),
        _kpi_row(acc, dg, ks, hlt, sup, pos),
        "",
        _growth_section(dg, baseline),
        "",
        _positions_section(pos, pmgr, conf),
        "",
        _signal_flow_section(approved, rejected),
        "",
        _loops_section(sup),
        "",
        _services_section(sup),
        "",
        _market_section(mctx),
        "",
        _mt5_section(hlt),
        "",
        GREY + "─" * 92 + R,
        f"{GREY}read-only · reads state/*.json every refresh · bot untouched · "
        f"Ctrl+C to quit{R}",
    ]
    # drop empty sections
    return "\n".join(p for p in parts if p != "" or True)


def main() -> None:
    global STATE, USE_COLOR, R, B, DIM, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, GREY
    ap = argparse.ArgumentParser(description="Terminal view of the MT5 Quant OS process.")
    ap.add_argument("--interval", type=float, default=2.0, help="refresh seconds (default 2)")
    ap.add_argument("--once", action="store_true", help="render a single frame and exit")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    ap.add_argument("--state-dir", default=str(STATE), help="state directory (default: repo state/)")
    args = ap.parse_args()

    STATE = Path(args.state_dir)
    if args.no_color:
        USE_COLOR = False
        # recompute color consts to empty so output is plain text
        R = B = DIM = RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = GREY = ""

    _enable_vt()

    if args.once:
        sys.stdout.write("\033[H\033[J" + render_frame() + "\n")
        return

    try:
        while True:
            frame = "\033[H\033[J" + render_frame() + "\n"
            sys.stdout.write(frame)
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        sys.stdout.write("\n" + R)
        sys.stdout.flush()


if __name__ == "__main__":
    main()