"""Run the bot's real replay pipeline on a symbol and produce a trade-by-trade
report (PnL, R, drawdown curve). Isolated state dir — never touches live state.

Usage:
    python scripts/replay_xau_report.py [symbol] [max_bars] [step] [start_cash]
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

# Make the repo root importable when run as `python scripts/foo.py`.
_SCRIPT_DIR = Path(__file__).resolve().parent
_ROOT = _SCRIPT_DIR.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Isolate ALL state writes before importing core modules (STATE_DIR is read at import).
OUT_DIR = Path("tmp/replay_xau_run")
OUT_DIR.mkdir(parents=True, exist_ok=True)
os.environ["MT5_QUANT_TEST_STATE_DIR"] = str(OUT_DIR.resolve())

import pandas as pd  # noqa: E402

from core.replay_engine import run_portfolio_replay  # noqa: E402
from core.utils import load_config  # noqa: E402

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)


def equity_curve(trades: list[dict], start_cash: float) -> list[dict]:
    """Chronological realized-equity curve from closed trades (cash basis)."""
    ordered = sorted(
        (t for t in trades if t.get("closed_at")),
        key=lambda t: t["closed_at"],
    )
    curve: list[dict] = []
    eq = start_cash
    peak = start_cash
    max_dd = 0.0
    for t in ordered:
        pnl = float(t.get("pnl", 0) or 0)
        eq += pnl
        peak = max(peak, eq)
        dd = peak - eq
        max_dd = max(max_dd, dd)
        curve.append({
            "closed_at": t.get("closed_at"),
            "symbol": t.get("symbol"),
            "side": t.get("side"),
            "pnl": round(pnl, 2),
            "r": t.get("r_multiple"),
            "result": t.get("result"),
            "exit_reason": t.get("exit_reason"),
            "equity": round(eq, 2),
            "drawdown_usd": round(dd, 2),
            "drawdown_pct": round(dd / peak * 100.0, 2) if peak > 0 else 0.0,
        })
    return curve


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "XAUUSDm"
    max_bars = int(sys.argv[2]) if len(sys.argv) > 2 else 12000
    step = int(sys.argv[3]) if len(sys.argv) > 3 else 20
    start_cash = float(sys.argv[4]) if len(sys.argv) > 4 else 100.0

    config = load_config()
    config["execution"]["starting_cash"] = start_cash
    # Let the engine force paper mode (it sets execution.mode = "paper").

    print(f"REPLAY_START symbol={symbol} max_bars={max_bars} step={step} start_cash={start_cash}")
    t0 = time.time()
    out = run_portfolio_replay(
        config,
        [symbol],
        max_bars=max_bars,
        step=step,
        return_all_trades=True,
    )
    elapsed = time.time() - t0
    print(f"REPLAY_DONE elapsed={elapsed:.1f}s")

    trades = out.get("trades") or []

    curve = equity_curve(trades, start_cash)
    wins = [t for t in trades if t.get("result") == "win"]
    losses = [t for t in trades if t.get("result") == "loss"]
    gross_win = sum(float(t.get("pnl", 0) or 0) for t in wins)
    gross_loss = sum(float(t.get("pnl", 0) or 0) for t in losses)
    max_dd_usd = max((c["drawdown_usd"] for c in curve), default=0.0)
    max_dd_pct = max((c["drawdown_pct"] for c in curve), default=0.0)
    peak_eq = max((c["equity"] for c in curve), default=start_cash)
    final_eq = curve[-1]["equity"] if curve else start_cash

    report = {
        "symbol": symbol,
        "timeframe": "M5 decision / M15 alignment",
        "max_bars": max_bars,
        "step": step,
        "elapsed_s": round(elapsed, 1),
        "starting_cash": start_cash,
        "signals_generated": out.get("signals_generated"),
        "signals_approved": out.get("signals_approved"),
        "news_blocked": out.get("news_blocked_signals"),
        "growth_blocked": out.get("growth_blocked_signals"),
        "trades_closed": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "pnl_total": round(gross_win + gross_loss, 2),
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "final_equity": round(out.get("final_equity", final_eq), 2),
        "realized_equity": round(final_eq, 2),
        "peak_equity": round(peak_eq, 2),
        "max_drawdown_usd": round(max_dd_usd, 2),
        "max_drawdown_pct": max_dd_pct,
        "avg_win_r": round(sum(float(t.get("r_multiple") or 0) for t in wins) / len(wins), 3) if wins else 0,
        "avg_loss_r": round(sum(float(t.get("r_multiple") or 0) for t in losses) / len(losses), 3) if losses else 0,
        "expectancy_r": round(
            (len(wins) / len(trades) * (sum(float(t.get("r_multiple") or 0) for t in wins) / len(wins)))
            + (len(losses) / len(trades) * (sum(float(t.get("r_multiple") or 0) for t in losses) / len(losses)))
            if wins and losses else 0, 3),
        "setup_stats": out.get("setup_stats"),
        "growth_replay": out.get("growth_replay"),
        "trades": trades,
        "equity_curve": curve,
    }
    (OUT_DIR / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")

    # Markdown report
    md = [
        f"# {symbol} Replay Report — real pipeline (paper broker)",
        "",
        f"- Window: last {max_bars} M5 bars, step {step} bars/step, start ${start_cash:.0f}",
        f"- Signals generated: {report['signals_generated']} · approved: {report['signals_approved']} · news-blocked: {report['news_blocked']} · growth-blocked: {report['growth_blocked']}",
        f"- Trades closed: **{len(trades)}** (wins {len(wins)} / losses {len(losses)}, win rate {report['win_rate_pct']}%)",
        f"- Net PnL: **${report['pnl_total']:.2f}** (gross +${gross_win:.2f} / −${-gross_loss:.2f})",
        f"- Final equity: **${report['final_equity']:.2f}** (realized ${final_eq:.2f}, peak ${peak_eq:.2f})",
        f"- Max drawdown: **${max_dd_usd:.2f} ({max_dd_pct:.2f}%)**",
        f"- Avg win {report['avg_win_r']}R · avg loss {report['avg_loss_r']}R · expectancy {report['expectancy_r']}R",
        f"- Elapsed: {elapsed:.1f}s",
        "",
        "## Trade-by-trade",
        "| # | Closed (UTC) | Side | Setup | Entry | Exit | R | PnL $ | Result | Exit reason | Regime |",
        "|---|--------------|------|-------|-------|------|---|-------|--------|-------------|--------|",
    ]
    for i, t in enumerate(sorted(trades, key=lambda t: t.get("closed_at") or ""), 1):
        mc = t.get("market_context") or {}
        regime = (mc.get("regime") if isinstance(mc, dict) else None) or (t.get("signal_meta") or {}).get("regime") or ""
        if isinstance(regime, dict):
            regime = regime.get("label") or regime.get("bias") or ""
        md.append(
            f"|{i}|{t.get('closed_at','')[:16]}|{t.get('side')}|{t.get('setup_type')}|"
            f"{t.get('entry')}|{t.get('exit')}|{t.get('r_multiple')}|{t.get('pnl')}|"
            f"{t.get('result')}|{t.get('exit_reason')}|{regime}|"
        )
    md.append("")
    md.append("## Equity curve (realized, per close)")
    md.append("| Closed (UTC) | Equity $ | Drawdown $ | Drawdown % |")
    md.append("|--------------|----------|-----------|------------|")
    for c in curve:
        md.append(f"|{c['closed_at'][:16]}|{c['equity']}|{c['drawdown_usd']}|{c['drawdown_pct']}|")
    (OUT_DIR / "report.md").write_text("\n".join(md), encoding="utf-8")

    # Console summary (pollable)
    print(json.dumps({
        "symbol": symbol,
        "elapsed_s": round(elapsed, 1),
        "signals": (report["signals_generated"], report["signals_approved"]),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": report["win_rate_pct"],
        "pnl_total": report["pnl_total"],
        "final_equity": report["final_equity"],
        "max_drawdown_usd": report["max_drawdown_usd"],
        "max_drawdown_pct": report["max_drawdown_pct"],
        "report_json": str((OUT_DIR / "report.json").resolve()),
        "report_md": str((OUT_DIR / "report.md").resolve()),
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
