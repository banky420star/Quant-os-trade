"""Babysit monitor — runs periodic checks on the bot for N hours.

Usage:
    python scripts/babysit_monitor.py                  # run for 6 hours
    python scripts/babysit_monitor.py --hours 2        # run for 2 hours
    python scripts/babysit_monitor.py --once           # single snapshot

Writes babysit_report.md + babysit_improvement_notes.md with every cycle.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from core.utils import read_json_state
except ImportError:
    STATE_DIR = ROOT / "state"

    def read_json_state(name: str, *, default: dict | list | None = None) -> dict | list:
        fp = STATE_DIR / name
        if not fp.exists():
            return default or {}
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            return default or {}


def slurp(name):
    d = read_json_state(name, default={})
    return d if isinstance(d, dict) else {}


def fmt(val, decimals=2):
    if val is None:
        return "?"
    try:
        return round(float(val), decimals)
    except (TypeError, ValueError):
        return val


def timestamp():
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


def take_snapshot() -> dict:
    """Collect all relevant bot state into a single dict."""
    snapshot: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ts_pretty": timestamp(),
    }

    # Account
    acct = slurp("account.json")
    snapshot["balance"] = fmt(acct.get("balance"))
    snapshot["equity"] = fmt(acct.get("equity"))
    snapshot["login"] = acct.get("login", "?")

    # Positions
    pos = slurp("paper_positions.json")
    positions = pos.get("positions", [])
    snapshot["open_positions"] = len(positions)
    snapshot["positions_detail"] = []
    for p in positions:
        snapshot["positions_detail"].append({
            "symbol": p.get("symbol", "?"),
            "side": p.get("side", "?"),
            "profit": fmt(p.get("profit"), 2),
            "entry": fmt(p.get("entry"), 4),
            "sl": fmt(p.get("sl"), 4),
            "tp1": fmt(p.get("tp1"), 4),
            "ticket": p.get("ticket", "?"),
        })

    # Trade log
    tl = slurp("trade_log.json")
    trades = tl if isinstance(tl, list) else tl.get("trades", [])
    snapshot["trade_count"] = len(trades)
    recent = trades[-30:]
    pnls = [float(t.get("pnl", 0) or 0) for t in recent]
    snapshot["recent_wins"] = sum(1 for p in pnls if p > 0)
    snapshot["recent_losses"] = sum(1 for p in pnls if p < 0)
    snapshot["recent_net_pnl"] = fmt(sum(pnls))

    # Exit reason distribution
    exit_reasons: dict[str, int] = {}
    for t in trades:
        er = t.get("exit_reason", "unknown") or "unknown"
        exit_reasons[er] = exit_reasons.get(er, 0) + 1
    snapshot["exit_reasons"] = exit_reasons

    # All-time stats
    if trades:
        all_pnls = [float(t.get("pnl", 0) or 0) for t in trades]
        wins = [p for p in all_pnls if p > 0]
        losses = [p for p in all_pnls if p < 0]
        snapshot["total_wr"] = fmt(100 * len(wins) / max(len(trades), 1))
        snapshot["total_net_pnl"] = fmt(sum(all_pnls))
        snapshot["total_payoff"] = fmt(
            abs((sum(wins) / max(len(wins), 1)) / (sum(losses) / max(len(losses), 1)))
        ) if losses else 0
        snapshot["avg_win"] = fmt(sum(wins) / max(len(wins), 1)) if wins else 0
        snapshot["avg_loss"] = fmt(sum(losses) / max(len(losses), 1)) if losses else 0
    else:
        snapshot["total_wr"] = 0
        snapshot["total_net_pnl"] = 0
        snapshot["total_payoff"] = 0
        snapshot["avg_win"] = 0
        snapshot["avg_loss"] = 0

    # Fast scalper
    rt = slurp("fast_mode_runtime.json")
    snapshot["fast_preset"] = rt.get("preset", "?")
    snapshot["fast_label"] = rt.get("label", "?")
    snapshot["fast_interval_ms"] = rt.get("overrides", {}).get("tick_interval_ms", "?")

    guard = slurp("fast_mode_guard.json")
    snapshot["fast_guard_mode"] = guard.get("mode", "?")
    snapshot["fast_guard_open"] = guard.get("open_positions", 0)

    # Fast decisions
    decisions = slurp("fast_mode_decisions.json")
    decs = decisions.get("decisions") or decisions.get("entries") or []
    snapshot["fast_decisions_24h"] = len(decs)
    if decs:
        last_5 = decs[-5:]
        snapshot["fast_last_decisions"] = [
            {
                "symbol": d.get("symbol", "?"),
                "action": d.get("action", d.get("decision", "?")),
                "ts": str(d.get("timestamp", d.get("ts", "?")))[:19],
            }
            for d in last_5
        ]

    # Fast signal cache
    cache = slurp("fast_signal_cache.json")
    if isinstance(cache, dict):
        cache_symbols = cache.get("symbols", {})
        if isinstance(cache_symbols, dict):
            snapshot["fast_cached_symbols"] = list(cache_symbols.keys())
        else:
            snapshot["fast_cached_symbols"] = str(cache_symbols)[:60]

    # Learning
    ls = slurp("learning_state.json")
    snapshot["learning_reviewed"] = ls.get("reviewed_count", "?")
    snapshot["learning_wr"] = ls.get("rolling_win_rate_pct", "?")
    snapshot["learning_exp"] = ls.get("rolling_expectancy_r", "?")
    snapshot["learning_mistakes"] = ls.get("mistake_counts", {})

    # Proposals
    try:
        from core.learning_logger import read_jsonl
        proposals = read_jsonl("config_proposals", limit=10)
        snapshot["proposals_count"] = len(proposals)
        active = [p for p in proposals if not p.get("rejected")]
        rejected = [p for p in proposals if p.get("rejected")]
        snapshot["active_proposals"] = len(active)
        snapshot["rejected_proposals"] = len(rejected)
        snapshot["proposals_detail"] = [
            {
                "symbol": p.get("symbol", "?"),
                "kind": p.get("kind", p.get("reason", "?")),
                "rejected": bool(p.get("rejected")),
                "ts": str(p.get("timestamp", p.get("ts", "?")))[:19],
            }
            for p in proposals[-5:]
        ]
    except Exception:
        snapshot["proposals_count"] = "?"

    # Profit quality summary
    try:
        import urllib.request
        r = urllib.request.urlopen("http://127.0.0.1:8080/api/profit_quality", timeout=5)
        pq = json.loads(r.read())
        snapshot["profit_quality"] = {
            "n_total": pq.get("n_total"),
            "meter": pq.get("meter", {}),
            "bleed_top": pq.get("bleed_top", [])[:3],
        }
    except Exception as e:
        snapshot["profit_quality"] = {"error": str(e)[:80]}

    return snapshot


def format_snapshot(snap: dict, prev: dict | None = None) -> list[str]:
    """Format one snapshot as report lines."""
    lines: list[str] = []
    lines.append(f"### {snap['ts_pretty']}")
    lines.append("")

    # Delta line
    delta_str = ""
    if prev:
        b_delta = float(snap.get("balance", 0) or 0) - float(prev.get("balance", 0) or 0)
        if abs(b_delta) > 0.01:
            delta_str += f" Balance Delta {fmt(b_delta)}"

        p_delta = float(snap.get("recent_net_pnl", 0) or 0) - float(prev.get("recent_net_pnl", 0) or 0)
        if abs(p_delta) > 0.01:
            delta_str += f" | Recent PnL Delta {fmt(p_delta)}"

        if snap.get("trade_count") != prev.get("trade_count"):
            new_trades = (snap.get("trade_count") or 0) - (prev.get("trade_count") or 0)
            delta_str += f" | +{new_trades} trades"

    if delta_str:
        lines.append(f"Changes: {delta_str}\n")

    # Main table
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Balance / Equity | {snap.get('balance', '?')} / {snap.get('equity', '?')} |")
    lines.append(f"| Open positions | {snap.get('open_positions', 0)} |")
    lines.append(f"| Last 30: W/L/Net | {snap.get('recent_wins', 0)}W / {snap.get('recent_losses', 0)}L / ${fmt(snap.get('recent_net_pnl'))} |")
    lines.append(f"| All-time WR / Payoff | {snap.get('total_wr', '?')}% / {snap.get('total_payoff', '?')} |")
    lines.append(f"| All-time Net PnL | ${snap.get('total_net_pnl', '?')} |")
    lines.append(f"| Avg win / Avg loss | ${snap.get('avg_win', '?')} / ${snap.get('avg_loss', '?')} |")
    lines.append(f"| Fast scalper | {snap.get('fast_label', '?')} @ {snap.get('fast_interval_ms', '?')}ms |")
    lines.append(f"| Fast guard | {snap.get('fast_guard_mode', '?')}, {snap.get('fast_guard_open', 0)} open |")
    lines.append(f"| Learning reviewed | {snap.get('learning_reviewed', '?')} |")
    lines.append(f"| Active proposals | {snap.get('active_proposals', snap.get('proposals_count', 0))} |")

    # Fast decisions
    if snap.get("fast_last_decisions"):
        lines.append(f"\n**Fast scalper last decisions:**")
        for d in snap["fast_last_decisions"]:
            lines.append(f"- {d['ts']} {d['symbol']} -> {d['action']}")

    # Open positions
    if snap.get("positions_detail"):
        lines.append(f"\n**Open positions:**")
        for p in snap["positions_detail"]:
            lines.append(f"- {p['symbol']} {p['side']} profit={p['profit']} entry={p['entry']}")

    # Exit reasons
    if snap.get("exit_reasons"):
        total = sum(snap["exit_reasons"].values())
        sl_rate = round(100 * (
            snap["exit_reasons"].get("stop_loss", 0) +
            snap["exit_reasons"].get("stop_out", 0)
        ) / max(total, 1), 1)
        lines.append(f"\n**Exit reasons (n={total}, SL hit rate: {sl_rate}%):**")
        for reason, count in sorted(snap["exit_reasons"].items(), key=lambda x: -x[1]):
            pct = round(100 * count / max(total, 1), 1)
            lines.append(f"- {reason}: {count} ({pct}%)")

    # Proposals
    if snap.get("proposals_detail"):
        lines.append(f"\n**Recent proposals:**")
        for p in snap["proposals_detail"]:
            status = "(rejected)" if p["rejected"] else "(active)"
            lines.append(f"- {p['symbol']} {p['kind']} {status}")

    return lines


def generate_improvement_notes(snapshot: dict, prev: dict | None = None) -> list[str]:
    """Analyze the snapshot and generate actionable improvement notes."""
    notes: list[str] = []
    now = snapshot["ts_pretty"]

    # SL hit rate analysis
    ers = snapshot.get("exit_reasons", {})
    if ers:
        total = sum(ers.values())
        sl_hits = ers.get("stop_loss", 0) + ers.get("stop_out", 0)
        sl_rate = round(100 * sl_hits / max(total, 1), 1)
        notes.append(f"[{now}] SL hit rate: {sl_rate}% ({sl_hits}/{total} trades)")

        if prev:
            prev_ers = prev.get("exit_reasons", {})
            prev_total = sum(prev_ers.values())
            prev_hits = prev_ers.get("stop_loss", 0) + prev_ers.get("stop_out", 0)
            prev_rate = round(100 * prev_hits / max(prev_total, 1), 1)
            delta = round(sl_rate - prev_rate, 1)
            direction = "up" if delta > 0 else "down" if delta < 0 else "stable"
            notes.append(f"  {direction} by {abs(delta)}pp since previous snapshot")

            if snapshot.get("trade_count") != prev.get("trade_count"):
                new_trades = (snapshot.get("trade_count") or 0) - (prev.get("trade_count") or 0)
                if new_trades > 0:
                    notes.append(f"  {new_trades} new trades since last check")

    # PnL direction
    net = float(snapshot.get("total_net_pnl", 0) or 0)
    if net < -200:
        notes.append(f"[{now}] TOTAL PnL ${fmt(net)} -- still bleeding")
    elif net < -100:
        notes.append(f"[{now}] Total PnL ${fmt(net)} -- slow bleed")
    elif net < 0:
        notes.append(f"[{now}] Total PnL ${fmt(net)} -- near breakeven")

    # Recent performance trend
    recent_net = float(snapshot.get("recent_net_pnl", 0) or 0)
    if prev:
        prev_recent = float(prev.get("recent_net_pnl", 0) or 0)
        if abs(recent_net - prev_recent) > 1.0:
            trend = "improving" if recent_net > prev_recent else "worsening"
            notes.append(f"[{now}] {trend} -- Last 30 PnL went from ${fmt(prev_recent)} to ${fmt(recent_net)}")

    # Fast scalper
    fast_open = snapshot.get("fast_guard_open", 0)
    fast_mode = snapshot.get("fast_guard_mode", "?")
    if fast_mode == "live" and fast_open == 0:
        notes.append(f"[{now}] Aggressive scalper scanning but no fills")
    elif fast_open > 0:
        notes.append(f"[{now}] Fast scalper has {fast_open} open position(s)")

    # Learning loop
    reviewed = snapshot.get("learning_reviewed", "?")
    if reviewed == 0 or reviewed == "?":
        notes.append(f"[{now}] Learning loop reviewed_count = {reviewed} -- review loop may not be running")
    elif isinstance(reviewed, (int, float)) and reviewed > 0:
        wr = snapshot.get("learning_wr", "?")
        exp = snapshot.get("learning_exp", "?")
        notes.append(f"[{now}] Learning loop: {reviewed} reviewed, {wr}% WR, {exp}R expectancy")

    # Proposals
    proposals = snapshot.get("active_proposals", 0)
    if proposals and proposals != "?" and proposals > 0:
        notes.append(f"[{now}] {proposals} active proposal(s) waiting for review")

    # Open positions
    if snapshot.get("positions_detail"):
        notes.append(f"[{now}] {len(snapshot['positions_detail'])} position(s) open")

    return notes


def run_monitor(hours: float = 6, interval_minutes: int = 15):
    """Main babysit loop."""
    report_path = ROOT / "babysit_report.md"
    notes_path = ROOT / "babysit_improvement_notes.md"
    end_time = time.time() + hours * 3600
    cycle = 0
    prev_snapshot: dict | None = None
    all_improvement_notes: list[str] = []

    print(f"--- Babysit Monitor started ---")
    print(f"Running for {hours}h, checking every {interval_minutes}min")
    print(f"Report: {report_path}")
    print(f"Notes:  {notes_path}")

    while time.time() < end_time:
        cycle += 1
        print(f"\n[Cycle {cycle}] Taking snapshot... ", end="", flush=True)

        snap = take_snapshot()
        print(f"done. balance={snap.get('balance', '?')} trades={snap.get('trade_count', 0)}")

        # Build report
        report_lines = [
            f"# Babysit Report -- Cycle {cycle}",
            "",
            *format_snapshot(snap, prev_snapshot),
            "",
        ]

        # Improvement notes
        imp_notes = generate_improvement_notes(snap, prev_snapshot)
        all_improvement_notes.extend(imp_notes)
        for note in imp_notes:
            report_lines.append(note)
            print(f"  Note: {note}")

        report_lines.extend(["", "---", ""])

        # Write to files
        mode = "a" if report_path.exists() else "w"
        header = "\n" if mode == "a" else ""
        with open(report_path, "a", encoding="utf-8") as f:
            f.write(header + "\n".join(report_lines) + "\n")

        with open(notes_path, "w", encoding="utf-8") as f:
            f.write("# Babysit Improvement Notes (Cumulative)\n\n")
            for note in all_improvement_notes:
                f.write(note + "\n")

        prev_snapshot = snap

        remaining = end_time - time.time()
        if remaining <= 0:
            break
        sleep_min = min(interval_minutes, remaining / 60)
        print(f"  Next check in {sleep_min:.0f}min ({remaining/60:.0f}min remaining)")
        time.sleep(sleep_min * 60)

    # Final summary
    print("\n" + "=" * 60)
    print("BABYSIT COMPLETE")
    print("=" * 60)
    print(f"Cycles completed: {cycle}")
    print(f"Improvement notes: {len(all_improvement_notes)}")
    print(f"Report: {report_path}")

    # Write final recommendations
    with open(notes_path, "a", encoding="utf-8") as f:
        f.write("\n\n## Final Recommendations\n\n")
        if prev_snapshot:
            final = prev_snapshot
            ers = final.get("exit_reasons", {})
            if ers:
                total = sum(ers.values())
                sl_hits = ers.get("stop_loss", 0) + ers.get("stop_out", 0)
                sl_rate = round(100 * sl_hits / max(total, 1), 1)
                f.write(f"1. SL hit rate: {sl_rate}% -- still the #1 bleed. Check if BE@1.0R + SL@1.0ATR are active.\n")
            net = float(final.get("total_net_pnl", 0) or 0)
            wr = final.get("total_wr", "?")
            payoff = final.get("total_payoff", "?")
            f.write(f"2. PnL: ${fmt(net)} | WR: {wr}% | Payoff: {payoff}\n")
            f.write(f"3. Fast scalper: {final.get('fast_label', '?')} @ {final.get('fast_interval_ms', '?')}ms\n")
            reviewed = final.get("learning_reviewed", "?")
            status = "(not writing state)" if reviewed == 0 or reviewed == "?" else "(OK)"
            f.write(f"4. Learning reviewed: {reviewed} {status}\n")
            if final.get("positions_detail"):
                f.write(f"5. Open positions: {len(final['positions_detail'])} -- review for stale holds\n")
            proposals = final.get("active_proposals", 0)
            if proposals and proposals != "?" and proposals > 0:
                f.write(f"6. Proposals: {proposals} active -- review and apply\n")

    with open(notes_path, "r", encoding="utf-8") as f:
        print("\nFinal notes:\n")
        print(f.read())


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Babysit the MT5 Quant bot")
    parser.add_argument("--hours", type=float, default=6)
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        snap = take_snapshot()
        lines = format_snapshot(snap)
        notes = generate_improvement_notes(snap)
        for ln in lines:
            print(ln)
        print("\n--- Improvement Notes ---")
        for n in notes:
            print(n)
    else:
        run_monitor(hours=args.hours, interval_minutes=args.interval)
