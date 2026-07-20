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

# Canonical pipeline order (core/pipeline.py) — used to sort supervisor loop rows.
PIPELINE_LOOP_ORDER = [
    "pipeline",
    "data_loop",
    "feature_loop",
    "market_context_loop",
    "risk_loop",
    "signal_loop",
    "verifier_loop",
    "execution_loop",
    "blue_guardian_loop",
    "position_manager_loop",
    "memory_loop",
    "adaptation_loop",
    "trade_log_loop",
    "health_loop",
]

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
def _header(acc, sup, rt, bg=None, arena=None) -> str:
    login = acc.get("login", "—")
    server = acc.get("server", "—")
    mode = acc.get("account_mode", "—")
    mode_col = GREEN if str(mode).lower() == "demo" else RED + B
    label = rt.get("label", "—") if isinstance(rt, dict) else "—"
    if isinstance(arena, dict) and arena.get("campaign_id"):
        cid = str(arena.get("campaign_id", ""))
        short = cid.replace("full-tilt-", "ft-") if cid.startswith("full-tilt-") else cid
        syms = arena.get("symbols") or []
        sym_s = f"{len(syms)}sym" if syms else ""
        label = f"{label} · {MAGENTA}arena {short}{R} {GREY}{sym_s}{R}"
    elif isinstance(bg, dict) and bg.get("enabled"):
        label = f"{label} · {CYAN}blue guardian $5k{R}"
    up = sup.get("uptime_seconds") if isinstance(sup, dict) else None
    up_s = f"{int(up)//3600}h{(int(up)%3600)//60}m" if isinstance(up, (int, float)) else "—"
    now = datetime.now().strftime("%H:%M:%S")
    line1 = f"{B}{CYAN}MT5 QUANT OS{R} {DIM}— live terminal view{R}"
    line2 = (f"{GREY}account{R} {B}{login}{R} {GREY}·{R} {server} {GREY}·{R} "
             f"mode={mode_col}{mode}{R} {GREY}·{R} runtime={B}{label}{R} "
             f"{GREY}·{R} uptime={up_s} {GREY}·{R} {DIM}{now}{R}")
    return line1 + "\n" + line2 + "\n" + GREY + "─" * 92 + R


def _kpi_row(acc, dg, ks, hlt, sup, pos, bg=None, rt=None) -> str:
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
    arena_on = isinstance(rt, dict) and rt.get("arena_active")
    if isinstance(bg, dict) and bg.get("enabled") and not arena_on:
        day_pct = bg.get("daily_pnl_pct")
    else:
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


def _blue_guardian_section(bg, pos, rt=None) -> str:
    if isinstance(rt, dict) and rt.get("arena_active"):
        return ""
    if not isinstance(bg, dict) or not bg.get("enabled"):
        return ""
    positions = pos.get("positions", []) if isinstance(pos, dict) else []
    floating = round(sum(float(p.get("profit", 0) or 0) for p in positions), 2)
    fcol = GREEN if floating >= -35 else (YELLOW if floating >= -45 else RED)
    tgt = float(bg.get("daily_profit_target_usd", 300) or 300)
    day_pnl = float(bg.get("daily_pnl", 0) or 0)
    dcol = GREEN if day_pnl >= 0 else RED
    paused = bool(bg.get("trading_paused"))
    bar_pct = min(100.0, max(0.0, day_pnl / tgt * 100.0)) if tgt else 0.0
    head = (
        f"{B}blue guardian{R} {DIM}instant $5k{R}  "
        f"day {dcol}{_signed(day_pnl)}{R}/{_money(tgt)} ({bg.get('daily_profit_target_pct', 6):.0f}%)  "
        f"float {fcol}{_signed(floating)}{R}  "
        f"open {len(positions)}/{bg.get('max_total_open_positions', '—')}"
    )
    flags = ""
    if paused:
        flags = f"  {RED}{B}PAUSED{R} {bg.get('pause_reason', '')}"
    shield = float(bg.get("guardian_shield_usd", -50) or -50)
    shield_dist = round(floating - shield, 2)
    bar = f"  {dcol}{_bar(bar_pct)}{R} {GREY}{bar_pct:.0f}% of +6% target{R}  shield buffer {_signed(shield_dist)}"
    return head + flags + "\n" + bar


def _growth_section(dg, baseline, bg=None) -> str:
    if isinstance(bg, dict) and bg.get("enabled"):
        return ""
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
    spread_src = approved.get("spread_source") if isinstance(approved, dict) else None
    spread_age = _age_str(approved.get("timestamp")) if isinstance(approved, dict) else "—"
    src_col = GREEN if spread_src == "mt5" else (YELLOW if spread_src else GREY)
    head = (f"{B}signal flow (verifier){R}  "
            f"{GREY}candidates{R} {B}{cand if cand is not None else '—'}{R}  "
            f"{GREEN}approved{R} {B}{a_n}{R}  "
            f"{RED}rejected{R} {B}{r_n}{R}  "
            f"{GREY}spreads{R} {src_col}{spread_src or '—'}{R} {GREY}({spread_age} ago){R}")
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


def _strategy_arena_section(arena, cand=None) -> str:
    """Live strategy competition — all setups fire, winners earn points."""
    ar = arena if isinstance(arena, dict) else {}
    if not ar.get("campaign_id"):
        return ""
    cid = ar.get("campaign_id", "—")
    syms = ar.get("symbols") or []
    totals = ar.get("totals") or {}
    trig = int(totals.get("triggers", 0) or 0)
    closed = int(totals.get("trades_closed", 0) or 0)
    pts = float(totals.get("points", 0) or 0)
    pts_col = GREEN if pts >= 0 else RED
    head = (
        f"{B}strategy arena{R} {MAGENTA}{cid}{R}  "
        f"{GREY}symbols{R} {B}{', '.join(syms) if syms else '—'}{R}  "
        f"{GREY}triggers{R} {B}{trig}{R}  "
        f"{GREY}closed{R} {B}{closed}{R}  "
        f"{GREY}points{R} {pts_col}{B}{_signed(pts)}{R}"
    )
    lines = [head]
    lb = ar.get("leaderboard") or {}
    rows = []
    for setup, row in lb.items():
        if not isinstance(row, dict):
            continue
        rows.append(row)
    catalog = ar.get("setup_catalog") or {}
    catalog_by = {}
    if isinstance(catalog, dict):
        for entry in catalog.get("setups") or []:
            if isinstance(entry, dict) and entry.get("setup_type"):
                catalog_by[entry["setup_type"]] = entry
    setup_types = ar.get("setup_types") or list(lb.keys())
    merged_rows = []
    seen = set()
    for setup in setup_types:
        row = dict(lb.get(setup) or {"setup_type": setup, "points": 0, "trades": 0, "triggers": 0})
        row["setup_type"] = setup
        cat = catalog_by.get(setup, {})
        row["trigger_summary"] = cat.get("trigger_summary") or ""
        merged_rows.append(row)
        seen.add(setup)
    for row in rows:
        st = row.get("setup_type")
        if st not in seen:
            merged_rows.append(row)
    merged_rows.sort(key=lambda r: (-float(r.get("points", 0)), -int(r.get("triggers", 0))))
    if merged_rows:
        lines.append(f"  {GREY}all setups + triggers (8 compete; zero rows still listed):{R}")
        for i, row in enumerate(merged_rows):
            setup = str(row.get("setup_type", "—"))[:20]
            p = float(row.get("points", 0) or 0)
            pcol = GREEN if p >= 0 else RED
            trades = int(row.get("trades", 0) or 0)
            trig_n = int(row.get("triggers", 0) or 0)
            wins = int(row.get("wins", 0) or 0)
            wr = (100.0 * wins / trades) if trades else 0.0
            exp_r = row.get("expectancy_r")
            exp_s = "—" if exp_r is None else f"{_signed(exp_r)}R"
            crown = f"{YELLOW}★{R} " if i == 0 and (p != 0 or trades) else "  "
            trig_hint = str(row.get("trigger_summary") or "")[:42]
            lines.append(
                f"  {crown}{setup:<20} {pcol}{_signed(p):>7} pts{R}  "
                f"{GREY}t={trades} trig={trig_n} wr={wr:.0f}% exp={exp_s}{R}"
            )
            if trig_hint:
                lines.append(f"      {GREY}trigger:{R} {trig_hint}")
    by_sym = ar.get("by_symbol") or {}
    if by_sym:
        lines.append(f"  {GREY}per-symbol leaders:{R}")
        for sym in sorted(by_sym.keys()):
            setups = by_sym.get(sym) or {}
            if not isinstance(setups, dict):
                continue
            leader = None
            for row in setups.values():
                if not isinstance(row, dict):
                    continue
                if leader is None or float(row.get("points", 0)) > float(leader.get("points", 0)):
                    leader = row
            if leader and (leader.get("trades") or leader.get("triggers")):
                setup = str(leader.get("setup_type", "—"))[:16]
                p = float(leader.get("points", 0) or 0)
                pcol = GREEN if p >= 0 else RED
                lines.append(
                    f"    {B}{sym:<10}{R} {YELLOW}{setup:<16}{R} {pcol}{_signed(p)} pts{R}  "
                    f"{GREY}t={leader.get('trades', 0)} trig={leader.get('triggers', 0)}{R}"
                )
            else:
                lines.append(f"    {sym:<10} {GREY}no trades yet{R}")
    snap = (cand or {}).get("strategy_arena") if isinstance(cand, dict) else None
    n_trig = (cand or {}).get("arena_triggers_recorded") if isinstance(cand, dict) else None
    if n_trig:
        lines.append(f"  {GREY}last signal cycle:{R} {B}{n_trig}{R} arena triggers recorded")
    elif snap and snap.get("global_top"):
        top = snap["global_top"]
        lines.append(
            f"  {GREY}live leader:{R} {YELLOW}{top.get('setup_type', '—')}{R} "
            f"{GREEN}{_signed(top.get('points', 0))} pts{R}"
        )
    insights = ar.get("insights") or {}
    if isinstance(insights, dict) and (
        insights.get("best_setup_per_session")
        or insights.get("best_setup_per_symbol")
        or insights.get("top_winning_conditions")
    ):
        lines.append(f"  {GREY}session + symbol insights (what worked where):{R}")
        for row in (insights.get("best_setup_per_session") or [])[:3]:
            sess = str(row.get("session", "—")).replace("_", " ")
            setup = str(row.get("setup_type", "—"))[:16]
            p = float(row.get("points", 0) or 0)
            pcol = GREEN if p >= 0 else RED
            lines.append(
                f"    {GREY}session{R} {B}{sess:<18}{R} → {YELLOW}{setup:<16}{R} "
                f"{pcol}{_signed(p)} pts{R} {GREY}wr={row.get('win_rate_pct', 0):.0f}% "
                f"t={row.get('trades', 0)}{R}"
            )
        for row in (insights.get("best_setup_per_symbol") or [])[:3]:
            sym = str(row.get("symbol", "—"))
            setup = str(row.get("setup_type", "—"))[:16]
            p = float(row.get("points", 0) or 0)
            pcol = GREEN if p >= 0 else RED
            lines.append(
                f"    {GREY}symbol{R} {B}{sym:<10}{R} → {YELLOW}{setup:<16}{R} "
                f"{pcol}{_signed(p)} pts{R} {GREY}wr={row.get('win_rate_pct', 0):.0f}% "
                f"pnl={_signed(row.get('net_pnl', 0))}{R}"
            )
        for row in (insights.get("top_winning_conditions") or [])[:2]:
            cond = (
                f"{row.get('symbol')} {row.get('setup_type')} "
                f"{row.get('session')} {row.get('regime')} "
                f"move={row.get('move_type')} trend={row.get('m5_trend')}"
            )[:58]
            lines.append(
                f"    {GREY}condition{R} {cond}  "
                f"{GREEN}{_signed(row.get('points', 0))} pts{R} "
                f"{GREY}wr={row.get('win_rate_pct', 0):.0f}%{R}"
            )
            hint = str(row.get("trigger_summary") or "")[:50]
            if hint:
                lines.append(f"      {GREY}cause:{R} {hint}")
    return "\n".join(lines)


def _rankings_section(rankings) -> str:
    """Per-symbol strategy rankings (signal_loop -> strategy_rankings.json)."""
    data = rankings if isinstance(rankings, dict) else {}
    by_sym = data.get("rankings", {}) if isinstance(data.get("rankings"), dict) else {}
    if not by_sym:
        return ""
    age = _age_str(data.get("timestamp"))
    head = (f"{B}strategy rankings (per symbol){R}  "
            f"{GREY}updated {age} ago{R}")
    lines = [head]
    for sym in sorted(by_sym.keys()):
        rows = by_sym.get(sym) or []
        if not rows:
            lines.append(f"  {sym:<10} {GREY}no rankings yet{R}")
            continue
        top = rows[0] if isinstance(rows[0], dict) else {}
        setup = str(top.get("setup_type", "—"))
        wr = top.get("win_rate_pct", top.get("score", 0))
        total = top.get("total", 0)
        insuf = top.get("insufficient_data")
        if insuf or not total:
            tag = f"{YELLOW}building own history{R}"
        else:
            tag = f"{GREEN}{wr:.1f}%{R} {GREY}n={total}{R}"
        lines.append(f"  {sym:<10} #{1} {setup:<18} {tag}")
        if len(rows) > 1 and isinstance(rows[1], dict):
            second = rows[1]
            s2 = str(second.get("setup_type", ""))
            wr2 = second.get("win_rate_pct", second.get("score", 0))
            n2 = second.get("total", 0)
            if s2:
                lines.append(f"  {'':10} #{2} {DIM}{s2:<18} {wr2:.1f}% n={n2}{R}")
    return "\n".join(lines)


def _culturing_section(ledger, policy) -> str:
    """Data-driven per-symbol culturing ledger + veto (forward_test_loop).

    Reads state/forward_test_ledger.json (per-cell stats) + symbol_policy_live.json
    (vetoed cells). Shows total cells, vetoed count, and per-symbol worst cell +
    vetoed cell keys. This is the UI mirror of the verifier's data_driven_veto
    gate so the user can see *which* cells the live forward-test pruned.
    """
    led = ledger if isinstance(ledger, dict) else {}
    pol = policy if isinstance(policy, dict) else {}
    if not led and not pol:
        return ""
    total = led.get("total_cells", 0)
    vetoed = led.get("total_vetoed", 0)
    cfg = led.get("config", {}) or {}
    min_n = cfg.get("min_n", pol.get("min_n", "—"))
    vwr = cfg.get("veto_win_rate_pct", pol.get("veto_win_rate_pct", "—"))
    cells_map = led.get("cells", {}) if isinstance(led.get("cells"), dict) else {}
    head = (f"{B}culturing ledger (data-driven veto){R}  "
            f"{GREY}cells{R} {B}{total}{R}  "
            f"{RED}vetoed{R} {B}{vetoed}{R}  "
            f"{GREY}min_n={min_n} veto_wr<{vwr}%{R}")
    lines = [head]
    syms = pol.get("symbols", {}) if isinstance(pol.get("symbols"), dict) else {}
    if not cells_map and not syms:
        lines.append(f"  {GREY}no cells yet — adaptation_loop hasn't recorded a cycle{R}")
        return "\n".join(lines)
    all_symbols = sorted(set(cells_map.keys()) | set(syms.keys()))
    for sym in all_symbols:
        cells = cells_map.get(sym, {})
        sym_v = set((syms.get(sym, {}) or {}).get("vetoed_cells", []) or [])
        # worst cell by realized net expectancy
        worst = None
        for cell, st in cells.items():
            if not isinstance(st, dict):
                continue
            try:
                net = float(st.get("expectancy_net_r", 0))
            except (TypeError, ValueError):
                continue
            if worst is None or net < worst[1]:
                worst = (cell, net, st)
        n_cells = len(cells)
        v_n = len(sym_v)
        tag = f"{RED}vetoed {v_n}{R}" if v_n else f"{GREEN}vetoed 0{R}"
        line = f"  {sym:<10} {GREY}cells={n_cells}{R} {tag}"
        if worst:
            wcell, wnet, wst = worst
            wc = wcell if len(wcell) <= 40 else wcell[:37] + "..."
            n = wst.get("n", 0)
            try:
                wr = float(wst.get("win_rate_pct", 0))
            except (TypeError, ValueError):
                wr = 0.0
            vlabel = str(wst.get("verdict", ""))
            vcol = RED if vlabel == "vetoed" else GREY
            avg_l = wst.get("avg_loss_usd")
            max_l = wst.get("max_loss_usd")
            br25 = wst.get("breach_25_count", 0)
            loss_s = ""
            if avg_l is not None:
                loss_s = f" avgL={_money(avg_l)} maxL={_money(max_l)} br25={br25}"
            line += (f"  {GREY}worst{R} {DIM}{wc}{R} "
                     f"{GREY}n={n} wr={wr:.0f}% netR={_signed(wnet)}{loss_s} {vcol}{vlabel}{R}")
        lines.append(line)
        for c in sorted(sym_v)[:3]:
            cc = c if len(c) <= 52 else c[:49] + "..."
            lines.append(f"      {RED}{cc}{R}")
    return "\n".join(lines)


def _trade_time_label(trade: dict) -> str:
    raw = trade.get("closed_at") or trade.get("opened_at") or ""
    if not raw:
        return "—         "
    return str(raw)[5:16].replace("T", " ")


def _summarize_trades_inline(trades: list[dict]) -> dict:
    wins = sum(1 for t in trades if t.get("result") == "win" or (t.get("pnl") or 0) > 0)
    losses = sum(1 for t in trades if t.get("result") == "loss" or (t.get("pnl") or 0) < 0)
    total = len(trades)
    pnl = sum(float(t.get("pnl") or 0) for t in trades)
    rs = [float(t["r_multiple"]) for t in trades if t.get("r_multiple") is not None]
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100.0 * wins / max(wins + losses, 1), 1) if total else 0.0,
        "total_pnl": round(pnl, 2),
        "avg_R": round(sum(rs) / len(rs), 3) if rs else None,
    }


def _trade_log_section(tlog) -> str:
    """Comprehensive per-trade log (state/trade_log.json).

    Session stats (since mt5_baseline.set_at) drive the summary when a session
    anchor exists — even when total is 0 after a fresh reset.
    """
    tl = tlog if isinstance(tlog, dict) else {}
    if not tl:
        return ""
    session = tl.get("session") if isinstance(tl.get("session"), dict) else {}
    use_session = bool(session.get("since"))
    trades = list(tl.get("session_trades") if use_session else tl.get("trades") or [])
    trades = [t for t in trades if not t.get("archive_polluted")]
    trades.sort(key=lambda r: str(r.get("closed_at") or ""), reverse=True)
    stats = _summarize_trades_inline(trades) if use_session else tl
    total = int(stats.get("total", 0) or 0)
    since_raw = str(session.get("since") or "")
    since_short = since_raw[11:16] if len(since_raw) >= 16 else ""
    if use_session and not total:
        scope = (
            f"{GREY}session acct {session.get('login', '—')}"
            f"{f' since {since_short} UTC' if since_short else ''}{R}"
        )
        return f"{B}trade log{R} {scope}  {GREY}no closed trades this session{R}"
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    wr = stats.get("win_rate_pct", 0)
    pnl = stats.get("total_pnl", 0)
    avg_r = stats.get("avg_R")
    pnl_col = GREEN if (pnl or 0) >= 0 else RED
    r_col = GREEN if (avg_r or 0) >= 0 else RED
    r_str = "—" if avg_r is None else f"{_signed(avg_r)}R"
    ks = tl.get("kelly") or {}
    ks_sized = int(ks.get("sized_trades", 0) or 0)
    ks_fb = int(ks.get("fallback_trades", 0) or 0)
    since_note = f" since {since_short} UTC" if use_session and since_short else ""
    scope = (
        f"{GREY}session acct {session.get('login', '—')}{since_note}{R}"
        if use_session else f"{GREY}all-time{R}"
    )
    head = (f"{B}trade log{R} {scope}  "
            f"{GREY}trades{R} {B}{total}{R}  "
            f"{GREEN}wins{R} {B}{wins}{R}  "
            f"{RED}losses{R} {B}{losses}{R}  "
            f"{GREY}win%{R} {B}{wr}{R}  "
            f"{GREY}netPnL{R} {pnl_col}{B}${float(pnl):.2f}{R}  "
            f"{GREY}avgR{R} {r_col}{B}{r_str}{R}  "
            f"{GREY}kelly{R} {GREEN}sized {ks_sized}{R}/{GREY}fb {ks_fb}{R}"
            + (
                ""
                if use_session
                else f"  {GREY}opened={tl.get('opened_at_known', '—')} dd={tl.get('drawdown_known', '—')}{R}"
            )
    )
    lines = [head]
    org = tl.get("session_organized") if use_session else tl.get("organized")
    org = org if isinstance(org, dict) else {}
    per_sym = org.get("per_symbol") if isinstance(org.get("per_symbol"), dict) else {}
    by_sym = org.get("by_symbol") if isinstance(org.get("by_symbol"), dict) else {}
    if not trades:
        return head
    lines.append(f"  {GREY}per-symbol (each treated individually):{R}")
    sym_order = list(org.get("symbols") or []) or sorted(per_sym.keys() or by_sym.keys())
    for sym in sym_order[:13]:
        ps = per_sym.get(sym) if isinstance(per_sym.get(sym), dict) else {}
        sm = ps.get("summary") or by_sym.get(sym) or {}
        n = int(sm.get("n") or 0)
        if not n:
            continue
        wr = sm.get("win_rate_pct", 0)
        spnl = float(sm.get("total_pnl") or 0)
        spnl_c = GREEN if spnl >= 0 else RED
        latest = None
        for t in trades:
            if t.get("symbol") == sym:
                latest = t
                break
        lat_s = ""
        if latest:
            res = str(latest.get("result") or "—")
            rc = GREEN if res == "win" else RED
            lat_s = (
                f"  {GREY}latest{R} {rc}{res}{R} "
                f"{spnl_c}${float(latest.get('pnl') or 0):+.2f}{R} "
                f"{DIM}{str(latest.get('setup') or '—')[:12]}{R}"
            )
        lines.append(
            f"  {B}{str(sym)[:10]:<10}{R} "
            f"{GREY}{n}t {wr}% ${spnl:+.2f}{R}{lat_s}"
        )
    lines.append(f"  {GREY}recent closed (all symbols):{R}")
    for t in trades[:8]:
        sym = str(t.get("symbol") or "—")[:9]
        side = str(t.get("side") or "—")
        side_col = GREEN if side == "BUY" else RED
        setup = str(t.get("setup") or t.get("setup_type") or "—")[:14]
        if setup == "unknown":
            setup = str(t.get("exit_reason") or "mt5")[:14]
        res = str(t.get("result") or "—")
        res_col = GREEN if res == "win" else RED
        pnl_v = t.get("pnl")
        try:
            pnl_f = float(pnl_v)
            pnl_s = f"{pnl_f:+.2f}$"
            pnl_c = GREEN if pnl_f >= 0 else RED
        except (TypeError, ValueError):
            pnl_s = "—"
            pnl_c = GREY
        r = t.get("r_multiple")
        r_s = "—" if r is None else f"{_signed(r)}R"
        r_c = GREEN if (r or 0) >= 0 else RED
        mae = t.get("mae_R")
        mae_s = "—" if mae is None else f"{mae:.2f}R"
        mfe = t.get("mfe_R")
        mfe_s = "—" if mfe is None else f"{mfe:.2f}R"
        hold = str(t.get("hold_human") or "—")
        conf = t.get("confidence") if t.get("confidence") is not None else "—"
        score = t.get("trade_score_total")
        score_s = "—" if score is None else f"{float(score):.0f}"
        regime = str(t.get("regime_primary") or "—")[:10]
        sess = str(t.get("session") or "—")[:12]
        when = _trade_time_label(t)
        k = t.get("kelly") if isinstance(t.get("kelly"), dict) else None
        if k:
            kfrac = k.get("fraction")
            kfrac_s = "—" if kfrac is None else f"{float(kfrac):.1f}%"
            k_col = GREEN if not k.get("gated") else GREY
            k_s = f"{k_col}{kfrac_s:<5}{R}"
        else:
            k_s = f"{GREY}—    {R}"
        lines.append(
            f"  {GREY}{when}{R} {side_col}{side:<4}{R} {B}{sym:<9}{R} "
            f"{DIM}{setup:<14}{R} {GREY}{regime:<10} {sess:<12}{R} "
            f"{GREY}c{conf} s{score_s}{R} "
            f"{res_col}{res:<4}{R} {pnl_c}{pnl_s:<8}{R} {r_c}{r_s:<7}{R} "
            f"{RED}dd{mae_s:<7}{R} {GREEN}run{mfe_s:<7}{R} {GREY}{hold:<8}{R} {GREY}k{k_s}"
        )
    return "\n".join(lines)


def _fresh(iso_ts, max_age: float = 120.0) -> bool:
    age = _age_seconds(iso_ts)
    return age is not None and age <= max_age


def _live_loop_override(
    name: str,
    *,
    approved: dict | None,
    broker: dict | None,
    candles: dict | None,
    acc: dict | None,
    features: dict | None = None,
    health: dict | None = None,
    tlog: dict | None = None,
    mctx: dict | None = None,
    rejected: dict | None = None,
) -> tuple[str, str] | None:
    """Correct stale supervisor errors using fresher state files."""
    if name == "data_loop" and isinstance(broker, dict) and isinstance(candles, dict):
        resolved = broker.get("resolved") or {}
        if len(resolved) >= 10 and _fresh(candles.get("timestamp"), 600):
            return ("ok", f"OK {len(resolved)} syms")
    if name == "feature_loop" and isinstance(features, dict) and _fresh(features.get("timestamp"), 180):
        n = len(features.get("symbols") or {})
        return ("ok", f"OK {n} features")
    if name == "market_context_loop" and isinstance(mctx, dict) and _fresh(mctx.get("timestamp"), 180):
        return ("ok", "OK context")
    if name == "verifier_loop" and isinstance(approved, dict) and _fresh(approved.get("timestamp"), 180):
        src = approved.get("spread_source", "mt5")
        spreads = approved.get("spread_data") or {}
        n = sum(1 for v in spreads.values() if float(v or 0) > 0)
        appr = int(approved.get("count", 0) or 0)
        rej = int((rejected or {}).get("count", 0) or 0) if isinstance(rejected, dict) else 0
        return ("ok", f"OK {appr}a/{rej}r ({n} spreads)")
    if name == "health_loop" and isinstance(health, dict) and _fresh(health.get("timestamp"), 180):
        status = str(health.get("status") or "healthy")
        return ("ok" if status == "healthy" else "warn", status)
    if name == "trade_log_loop" and isinstance(tlog, dict) and _fresh(tlog.get("updated_at"), 600):
        return ("ok", f"OK {tlog.get('total', 0)} trades")
    return None


def _short_loop_result(res: str, width: int = 28) -> str:
    text = str(res or "")
    if text.upper().startswith("FAILED:"):
        text = text[7:].strip()
    if len(text) > width:
        return text[: width - 3] + "..."
    return text


def _loops_section(
    sup,
    *,
    approved: dict | None = None,
    broker: dict | None = None,
    candles: dict | None = None,
    acc: dict | None = None,
    features: dict | None = None,
    health: dict | None = None,
    tlog: dict | None = None,
    mctx: dict | None = None,
    rejected: dict | None = None,
) -> str:
    raw_loops = sup.get("loops", []) if isinstance(sup, dict) else []
    loops = []
    if isinstance(raw_loops, list):
        loops = [lp for lp in raw_loops if isinstance(lp, dict)]
    elif isinstance(raw_loops, dict):
        for name, lp in raw_loops.items():
            if isinstance(lp, dict):
                item = dict(lp)
                item.setdefault("name", name)
                loops.append(item)
    if not loops:
        return f"{B}supervisor loops{R}\n  {GREY}no loop data — waiting for first cycle{R}"
    order_idx = {n: i for i, n in enumerate(PIPELINE_LOOP_ORDER)}
    loops = sorted(loops, key=lambda lp: order_idx.get(str(lp.get("name", "")), 999))

    resolved_rows: list[tuple[str, str, str, str]] = []
    ok_n = err_n = 0
    raw_errors: list[str] = []
    for lp in loops:
        name = str(lp.get("name", ""))
        st = str(lp.get("status", ""))
        res = str(lp.get("last_result", ""))
        if res.upper().startswith("FAILED:"):
            raw_errors.append(res[7:].strip())
        override = _live_loop_override(
            name,
            approved=approved,
            broker=broker,
            candles=candles,
            acc=acc,
            features=features,
            health=health,
            tlog=tlog,
            mctx=mctx,
            rejected=rejected,
        )
        if override:
            st, res = override
        if st.lower() in ("ok", "warn"):
            ok_n += 1
        else:
            err_n += 1
        age = _age_str(lp.get("last_run"))
        resolved_rows.append((name, st, _short_loop_result(res), age))

    run_count = loops[0].get("run_count", "—")
    live_ok = sum(1 for _, st, _, _ in resolved_rows if st.lower() == "ok")
    summary_col = GREEN if err_n == 0 else (YELLOW if live_ok >= len(resolved_rows) // 2 else RED)
    lines = [
        f"{B}supervisor loops{R} {summary_col}{live_ok}/{len(resolved_rows)} OK{R}"
        f"{GREY} · run #{run_count}{R}",
    ]
    unique_errors = sorted(set(raw_errors))
    if unique_errors and len(unique_errors) == 1 and err_n >= 3:
        lines.append(f"  {RED}root cause{R} {unique_errors[0]}"
                     f"{GREY} — restart start.py after config/code fix{R}")
    elif err_n:
        bad = [n for n, st, _, _ in resolved_rows if st.lower() not in ("ok", "warn")]
        if bad:
            lines.append(f"  {YELLOW}supervisor flagged: {', '.join(bad[:6])}"
                         f"{'…' if len(bad) > 6 else ''}{R}")
    for i in range(0, len(resolved_rows), 2):
        pair = []
        for name, st, res, age in resolved_rows[i:i + 2]:
            st_l = st.lower()
            stcol = GREEN if st_l == "ok" else (YELLOW if st_l == "warn" else RED)
            rescol = GREEN if res.upper().startswith("OK") else (
                YELLOW if st_l == "warn" else GREY
            )
            pair.append(f"  {name:<22}{stcol}{st:<4}{R} {rescol}{res:<28}{R} {GREY}{age:>6}{R}")
        lines.append(" ".join(pair))
    return "\n".join(lines)


def _fast_mode_section(runtime, decisions, sup) -> str:
    """Fast scalper runtime status (presets controlled via dashboard / web TUI)."""
    rt = runtime if isinstance(runtime, dict) else {}
    dec = decisions if isinstance(decisions, dict) else {}
    overrides = rt.get("overrides") or {}
    if not overrides and not rt.get("preset"):
        preset = "(profile yaml)"
        label = ""
    else:
        preset = rt.get("preset") or "custom"
        label = rt.get("label") or ""
    live = bool(overrides.get("live_enabled"))
    enabled = overrides.get("enabled", True)
    mode_col = YELLOW if live else CYAN
    mode_txt = "LIVE" if live else ("observe" if enabled else "off")
    syms = overrides.get("symbols") or []
    tick = overrides.get("tick_interval_ms")
    svc = next(
        (s for s in (sup.get("services") or []) if s.get("name") == "fast_mode"),
        {},
    ) if isinstance(sup, dict) else {}
    svc_st = str(svc.get("status", "—"))
    svc_col = GREEN if svc_st.lower() in ("ok", "healthy") else YELLOW
    last_dec = (dec.get("decisions") or [None])[-1] if dec.get("decisions") else None
    act = last_dec.get("action", "—") if isinstance(last_dec, dict) else "—"
    lines = [
        f"{B}fast scalper{R}  preset={B}{preset}{R}"
        + (f" ({label})" if label else ""),
        f"  mode={mode_col}{mode_txt}{R}  tick={tick or '—'}ms  "
        f"symbols={','.join(syms) if syms else '—'}  "
        f"service={svc_col}{svc_st}{R}  last={act}",
        f"  {DIM}presets: dashboard :8080 or terminal web :8083 buttons{R}",
    ]
    return "\n".join(lines)


def _services_section(sup) -> str:
    services = sup.get("services", []) if isinstance(sup, dict) else []
    if not services:
        return ""
    lines = [f"{B}services{R}"]
    for s in services:
        name = str(s.get("label") or s.get("name", ""))
        st = str(s.get("status", ""))
        st_l = st.lower()
        if st_l in ("ok", "healthy", "complete"):
            stcol = GREEN
        elif st_l in ("running", "pending"):
            stcol = YELLOW
        else:
            stcol = RED
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
    bg = _load("blue_guardian.json") or {}
    rt = _load("runtime_mode.json") or {}
    baseline = _load("mt5_baseline.json") or {}
    pos = _load("paper_positions.json") or {}
    pmgr = _load("position_management.json") or {}
    conf = _load("position_confidence.json") or {}
    mctx = _load("market_context.json") or {}
    approved = _load("approved_signals.json") or {}
    rejected = _load("rejected_signals.json") or {}
    broker = _load("broker_symbols.json") or {}
    candles = _load("latest_candles.json") or {}
    cult = _load("forward_test_ledger.json") or {}
    veto = _load("symbol_policy_live.json") or {}
    rankings = _load("strategy_rankings.json") or {}
    arena = _load("strategy_arena.json") or {}
    cand = _load("candidate_signals.json") or {}
    tlog = _load("trade_log.json") or {}
    features = _load("features.json") or {}
    fm_rt = _load("fast_mode_runtime.json") or {}
    fm_dec = _load("fast_mode_decisions.json") or {}

    # Visual dividers between every section so the whole TUI reads as separated
    # blocks (USER request 2026-07-01: "tui needs to be in split in a divider
    # to make it viisable as a whole"). Empty sections are dropped so we never
    # emit a dangling rule; the header + KPI row are joined as one title block
    # (no rule between them) so the title stays a coherent unit.
    _DIV = GREY + "─" * 92 + R
    arena_active = bool(isinstance(arena, dict) and arena.get("campaign_id"))
    title_bits = [_header(acc, sup, rt, bg, arena), _kpi_row(acc, dg, ks, hlt, sup, pos, bg, rt)]
    title = "\n".join(b for b in title_bits if b and b.strip())
    body = [
        _blue_guardian_section(bg, pos, rt),
        _growth_section(dg, baseline, bg),
        _strategy_arena_section(arena, cand) if arena_active else "",
        _positions_section(pos, pmgr, conf),
        _signal_flow_section(approved, rejected),
        _rankings_section(rankings) if not arena_active else "",
        _culturing_section(cult, veto),
        _trade_log_section(tlog),
        _loops_section(
            sup,
            approved=approved,
            broker=broker,
            candles=candles,
            acc=acc,
            features=features,
            health=hlt,
            tlog=tlog,
            mctx=mctx,
            rejected=rejected,
        ),
        _fast_mode_section(fm_rt, fm_dec, sup),
        _services_section(sup),
        _market_section(mctx),
        _mt5_section(hlt),
        f"{GREY}reads state/*.json every refresh · fast presets on dashboard/TUI web · "
        f"Ctrl+C to quit{R}",
    ]
    blocks = [title] + [s for s in body if s and s.strip()]
    blocks = [b for b in blocks if b and b.strip()]
    if not blocks:
        return ""
    out = [blocks[0]]
    for cur in blocks[1:]:
        out.append(_DIV)
        out.append(cur)
    out.append(_DIV)
    return "\n".join(out)


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
