"""Regenerate the last-N-days trade review report from ``state/mt5_trades.json``.

Reproduces the analysis used for ``docs/reviews/last7d_trade_review.md``:
executive summary, full chronological ledger (entry/exit, setup, hold, R, PnL),
a per-loser diagnosis for every losing trade, PnL by symbol/setup, and the
biggest dollar losers — so you can re-run it after every change.

Usage:
    python scripts/review_last7d.py [--days 7] [--out docs/reviews/last7d_trade_review.md]
                                    [--json-out tmp/last7d_trades.json] [--state-dir state]

- ``--days``          lookback window in days (default 7)
- ``--out``           markdown report path (default docs/reviews/last7d_trade_review.md)
- ``--json-out``      optional path to also dump the raw filtered trade rows
- ``--state-dir``     state directory override (default: core.utils STATE_DIR,
                      which honours MT5_QUANT_TEST_STATE_DIR)

Read-only: never writes live state, never places orders.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

# Make the repo root importable when run as `python scripts/foo.py`.
_SCRIPT_DIR = Path(__file__).resolve().parent
_ROOT = _SCRIPT_DIR.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _parse(ts) -> dt.datetime | None:
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _as_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _load_trades(state_dir: Path) -> list[dict]:
    """Closed trades from state/mt5_trades.json (dict with 'trades' or plain list)."""
    p = state_dir / "mt5_trades.json"
    if not p.exists():
        raise FileNotFoundError(f"{p} missing — nothing to review")
    doc = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    trades = doc.get("trades") if isinstance(doc, dict) else doc
    return list(trades or [])


def _journal_entry_times(state_dir: Path) -> dict[str, dict]:
    """Index per-trade journal files by trade_id -> {opened_at, closed_at, setup}."""
    idx: dict[str, dict] = {}
    jdir = state_dir / "trade_journal"
    if not jdir.is_dir():
        return idx
    for f in jdir.rglob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        rec = d.get("record") or {}
        summ = d.get("summary") or {}
        tid = str(d.get("trade_id") or rec.get("trade_id") or summ.get("trade_id") or "")
        if not tid or tid in idx:
            continue
        idx[tid] = {
            "opened_at": summ.get("opened_at") or rec.get("opened_at"),
            "closed_at": summ.get("closed_at") or rec.get("closed_at"),
            "setup": summ.get("setup") or rec.get("setup_type"),
        }
    return idx


def _classify_loser(t: dict) -> str:
    """Failure pattern for a losing trade, from mae_R/mfe_R + exit reason."""
    if t.get("exit_reason") == "stop_out":
        return "stop_out"
    mae = _as_float(t.get("mae_R"))
    mfe = _as_float(t.get("mfe_R"))
    if mfe is not None and mfe >= 0.8:
        return "gave_back_profit"
    if mae is not None and mfe is not None and mae >= abs(mfe) * 0.9 and mae >= 0.6:
        return "reverse_entry"
    if mae is not None and mae >= 0.5 and (mfe or 0) < 0.3:
        return "reverse_entry"
    if mfe is not None and mfe >= 0.4:
        return "mild_favorable_then_loss"
    return "straight_loss"


def _loser_note(t: dict, diag: str) -> str:
    hold = t.get("hold_min")
    hold_s = f"{hold}min" if hold is not None else "?"
    mae = _as_float(t.get("mae_R"))
    mfe = _as_float(t.get("mfe_R"))
    r = t.get("r_multiple")
    if diag == "reverse_entry":
        return (
            f"Entry fought price from the first tick (max adverse {mae:.2f}R vs only "
            f"{mfe if mfe is not None else 0:.2f}R favorable) — the {t.get('setup')} signal on "
            f"{t.get('symbol')} chased a move that had already peaked; price reversed immediately "
            f"and never confirmed the {t.get('side')} thesis within the {hold_s} hold."
        )
    r_s = f"{r:.2f}" if r is not None else "n/a"
    if diag == "gave_back_profit":
        return (
            f"Reached {mfe:.2f}R in profit but gave it ALL back and stopped at {r_s}R — exit "
            f"management failed. A partial-take or trailing stop should have locked gains when the "
            f"move was {mfe:.2f}R in the money."
        )
    if diag == "mild_favorable_then_loss":
        return (
            f"Was up to {mfe:.2f}R, came back, and hit the stop at {r_s}R. Trend faded before the "
            f"target — the setup lacked follow-through; a break-even trigger once +0.5R would have "
            f"converted this to a scratch."
        )
    if diag == "stop_out":
        return (
            "Margin stop-out (broker-level) — position was too large for available free margin, "
            "not a strategy loss. Sizing/leverage must respect the live equity buffer."
        )
    return (
        f"Straight to the stop ({r_s}R) with no material favorable excursion — the "
        f"{t.get('setup')} on {t.get('symbol')} was on the wrong side of the tape for the {hold_s} "
        f"hold; entry timing was poor, likely mid-retracement or against the prevailing micro-trend."
    )


def build_report(trades: list[dict], days: int, now: dt.datetime) -> dict:
    """Filter to the last `days` days and produce the full report payload."""
    cutoff = now - dt.timedelta(days=days)

    rows = []
    for t in trades:
        closed = _parse(t.get("closed_at"))
        if closed is None or closed < cutoff:
            continue
        entry = _parse(t.get("opened_at") or t.get("entry_time"))
        hold_min = round((closed - entry).total_seconds() / 60.0, 1) if entry else None
        diag = _classify_loser(t)
        rows.append({
            "trade_id": t.get("trade_id"),
            "symbol": t.get("symbol"),
            "side": t.get("side"),
            "setup": t.get("setup_type") or t.get("setup"),
            "entry": t.get("entry"),
            "exit": t.get("exit"),
            "entry_time": (entry.isoformat() if entry else None),
            "closed_at": t.get("closed_at"),
            "hold_min": hold_min,
            "exit_reason": t.get("exit_reason"),
            "r": _as_float(t.get("r_multiple")),
            "pnl": _as_float(t.get("pnl")) or 0.0,
            "result": t.get("result"),
            "volume": t.get("volume"),
            "mae_R": _as_float(t.get("mae_R")),
            "mfe_R": _as_float(t.get("mfe_R")),
            "diag": diag if t.get("result") == "loss" else "",
            "sl": t.get("sl_initial") or t.get("sl"),
            "exit_narrative": t.get("exit_narrative"),
        })
    rows.sort(key=lambda r: r["closed_at"] or "")

    wins = [r for r in rows if r["result"] == "win"]
    losses = [r for r in rows if r["result"] == "loss"]
    breakeven = [r for r in rows if r["result"] == "breakeven"]
    gross_win = sum(r["pnl"] for r in wins)
    gross_loss = sum(r["pnl"] for r in losses)
    avg_win_r = sum(r["r"] or 0 for r in wins) / len(wins) if wins else 0.0
    avg_loss_r = sum(r["r"] or 0 for r in losses) / len(losses) if losses else 0.0
    n = len(rows) or 1
    expectancy = (
        (len(wins) / n) * avg_win_r + (len(losses) / n) * avg_loss_r
        if wins and losses else 0.0
    )

    # Equity / drawdown curve (realized, cash basis).
    curve: list[dict] = []
    eq = peak = 100.0
    max_dd = 0.0
    for r in rows:
        eq += r["pnl"]
        peak = max(peak, eq)
        dd = peak - eq
        max_dd = max(max_dd, dd)
        curve.append({
            "closed_at": r["closed_at"],
            "equity": round(eq, 2),
            "drawdown_usd": round(dd, 2),
            "drawdown_pct": round(dd / peak * 100.0, 2) if peak > 0 else 0.0,
        })

    def by_key(key: str) -> dict[str, tuple[int, float]]:
        agg: dict[str, list[float]] = {}
        for r in rows:
            agg.setdefault(str(r[key] or "?"), []).append(r["pnl"])
        return {k: (len(v), round(sum(v), 2)) for k, v in sorted(agg.items(), key=lambda x: x[1][1])}

    return {
        "days": days,
        "cutoff_utc": cutoff.isoformat(),
        "trades": rows,
        "trades_closed": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate_pct": round(len(wins) / n * 100, 1),
        "pnl_total": round(gross_win + gross_loss, 2),
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_win_r": round(avg_win_r, 3),
        "avg_loss_r": round(avg_loss_r, 3),
        "expectancy_r": round(expectancy, 3),
        "max_drawdown_usd": round(max_dd, 2),
        "max_drawdown_pct": round(max(d["drawdown_pct"] for d in curve), 2) if curve else 0.0,
        "by_exit": dict(Counter(r["exit_reason"] for r in rows)),
        "by_setup": dict(Counter(r["setup"] for r in rows)),
        "by_symbol": dict(Counter(r["symbol"] for r in rows)),
        "loser_patterns": dict(Counter(r["diag"] for r in losses)),
        "pnl_by_symbol": by_key("symbol"),
        "pnl_by_setup": by_key("setup"),
        "equity_curve": curve,
        "biggest_losers": sorted(losses, key=lambda r: r["pnl"])[:15],
    }


def render_markdown(report: dict) -> str:
    out: list[str] = []
    out.append("# Last %d Days — Complete Trade Review" % report["days"])
    out.append(
        f"Window: trades closed on/after {report['cutoff_utc'][:16]}Z "
        f"(source: `state/mt5_trades.json`, generated {dt.datetime.now(dt.timezone.utc).isoformat()[:16]}Z)"
    )
    out.append("")
    out.append("## Executive summary")
    out.append(f"- **Trades closed:** {report['trades_closed']} "
               f"(wins {report['wins']} · losses {report['losses']} · breakeven {report['breakeven']})")
    out.append(f"- **Win rate:** {report['win_rate_pct']}%")
    out.append(f"- **Net PnL:** ${report['pnl_total']:.2f}  "
               f"(gross win ${report['gross_win']:.2f} / gross loss ${report['gross_loss']:.2f})")
    out.append(f"- **Avg win:** {report['avg_win_r']}R · **Avg loss:** {report['avg_loss_r']}R")
    out.append(f"- **Expectancy:** {report['expectancy_r']}R/trade")
    out.append(f"- **Max drawdown (realized):** ${report['max_drawdown_usd']:.2f} "
               f"({report['max_drawdown_pct']:.2f}%)")
    out.append(f"- **By exit:** {report['by_exit']}")
    out.append(f"- **By setup:** {report['by_setup']}")
    out.append(f"- **By symbol:** {report['by_symbol']}")
    out.append("")
    out.append("## All closed trades (chronological)")
    out.append("| # | Closed (UTC) | Symbol | Side | Setup | Vol | Entry | Exit | Hold | R | PnL $ | Exit |")
    out.append("|---|--------------|--------|------|-------|-----|-------|------|------|----|-------|------|")
    for i, r in enumerate(report["trades"], 1):
        ent = r["entry"]
        ex = r["exit"]
        out.append(
            f"|{i}|{str(r['closed_at'])[:16]}|{r['symbol']}|{r['side']}|{r['setup']}|{r['volume']}|"
            f"{round(ent, 3) if isinstance(ent, (int, float)) else ent}|"
            f"{round(ex, 3) if isinstance(ex, (int, float)) else ex}|"
            f"{r['hold_min'] if r['hold_min'] is not None else '?'}m|{r['r']}|{r['pnl']:.2f}|{r['exit_reason']}|"
        )
    out.append("")
    out.append("## All losers — diagnosis & how to do better")
    out.append(f"Pattern counts: {report['loser_patterns']}")
    out.append("")
    out.append("| Closed (UTC) | Symbol | Side | Setup | R | PnL $ | What went wrong / how to do better |")
    out.append("|--------------|--------|------|-------|---|-------|--------------------------------------|")
    for r in report["trades"]:
        if r["result"] != "loss":
            continue
        out.append(
            f"|{str(r['closed_at'])[:16]}|{r['symbol']}|{r['side']}|{r['setup']}|{r['r']}|"
            f"{r['pnl']:.2f}|{_loser_note(r, r['diag'])}|"
        )
    out.append("")
    out.append("## PnL by symbol and setup")
    out.append(f"- **symbol:** " + ", ".join(f"{k}: {v[0]} trades, ${v[1]:.2f}" for k, v in report["pnl_by_symbol"].items()))
    out.append(f"- **setup:** " + ", ".join(f"{k}: {v[0]} trades, ${v[1]:.2f}" for k, v in report["pnl_by_setup"].items()))
    out.append("")
    out.append("## Biggest dollar losers (top 15)")
    for r in report["biggest_losers"]:
        out.append(f"- **${r['pnl']:.2f}** {r['symbol']} {r['side']} {r['setup']} R={r['r']} "
                   f"@ {str(r['closed_at'])[:16]} — {_loser_note(r, r['diag'])}")
    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Regenerate the last-N-days trade review report.")
    ap.add_argument("--days", type=int, default=7, help="lookback window in days (default 7)")
    ap.add_argument("--out", default="docs/reviews/last7d_trade_review.md", help="markdown report path")
    ap.add_argument("--json-out", default=None, help="optional path to dump raw filtered trade rows")
    ap.add_argument("--state-dir", default=None, help="state dir override (default: core.utils STATE_DIR)")
    args = ap.parse_args()

    from core.utils import STATE_DIR  # noqa: E402 (honours MT5_QUANT_TEST_STATE_DIR)

    state_dir = Path(args.state_dir) if args.state_dir else STATE_DIR
    trades = _load_trades(state_dir)
    if not trades:
        print("No trades in state/mt5_trades.json — nothing to review.", file=sys.stderr)
        return 1

    # Attach journal entry times where the ledger lacks them.
    jidx = _journal_entry_times(state_dir)
    for t in trades:
        if not t.get("opened_at") and not t.get("entry_time"):
            j = jidx.get(str(t.get("trade_id") or ""))
            if j and j.get("opened_at"):
                t["opened_at"] = j["opened_at"]

    report = build_report(trades, args.days, now=dt.datetime.now(dt.timezone.utc))
    md = render_markdown(report)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")

    if args.json_out:
        jp = Path(args.json_out)
        jp.parent.mkdir(parents=True, exist_ok=True)
        jp.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")

    print(json.dumps({
        "days": report["days"],
        "trades_closed": report["trades_closed"],
        "wins": report["wins"],
        "losses": report["losses"],
        "breakeven": report["breakeven"],
        "win_rate_pct": report["win_rate_pct"],
        "pnl_total": report["pnl_total"],
        "gross_win": report["gross_win"],
        "gross_loss": report["gross_loss"],
        "avg_win_r": report["avg_win_r"],
        "avg_loss_r": report["avg_loss_r"],
        "expectancy_r": report["expectancy_r"],
        "max_drawdown_usd": report["max_drawdown_usd"],
        "max_drawdown_pct": report["max_drawdown_pct"],
        "loser_patterns": report["loser_patterns"],
        "report_path": str(out_path.resolve()),
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
