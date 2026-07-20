"""Full trading + bot state analysis report."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    state = ROOT / "state"
    with open(state / "trade_log.json", encoding="utf-8") as f:
        tl = json.load(f)

    print("=" * 60)
    print("MT5 QUANT AGENT — FULL ANALYSIS")
    print("=" * 60)

    print("\n## OVERALL PERFORMANCE")
    print(f"  Trades:      {tl.get('total')} ({tl.get('wins')}W / {tl.get('losses')}L)")
    print(f"  Win rate:    {tl.get('win_rate_pct')}%")
    print(f"  Total PnL:   ${tl.get('total_pnl', 0):+.2f}")
    print(f"  Avg R:       {tl.get('avg_R')}R")
    print(f"  Expectancy:  {tl.get('expectancy_R')}R")

    by_sym = tl.get("organized", {}).get("by_symbol", {})
    rows = sorted(by_sym.items(), key=lambda x: x[1].get("total_pnl", 0), reverse=True)
    print("\n## BY SYMBOL (all-time)")
    for sym, d in rows:
        if d.get("n", 0) == 0:
            continue
        print(
            f"  {sym:<10} {d.get('n', 0):>4} trades  "
            f"{d.get('win_rate_pct', 0):>5.1f}% WR  "
            f"${d.get('total_pnl', 0):>+9.2f}  "
            f"{d.get('avg_R') or 0:>+7.3f}R"
        )

    for path, title in [
        ("growth_campaign.json", "GROWTH CAMPAIGN"),
        ("daily_growth.json", "TODAY"),
        ("equity_history.json", "EQUITY"),
        ("runtime_mode.json", "RUNTIME"),
        ("active_profile.json", "ACTIVE PROFILE"),
        ("kill_switch.json", "KILL SWITCH"),
    ]:
        p = state / path
        if p.exists():
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            print(f"\n## {title}")
            if path == "equity_history.json":
                pts = data.get("points") or []
                if pts:
                    print(f"  Latest equity: ${pts[-1].get('equity', 0):.2f}")
                    print(f"  Open positions: {pts[-1].get('open_positions', 0)}")
                    print(f"  Exposure: {pts[-1].get('exposure_used_pct', 0):.1f}%")
            else:
                for k, v in data.items():
                    if k not in ("points", "services"):
                        print(f"  {k}: {v}")

    trades = tl.get("trades") or tl.get("closed_trades") or []
    if trades:
        def pnl(t):
            return t.get("pnl", t.get("profit", 0)) or 0
        top = sorted(trades, key=pnl, reverse=True)
        print("\n## TOP 5 WINNERS")
        for t in top[:5]:
            print(f"  {t.get('symbol')} ${pnl(t):+.2f}  {t.get('setup','?')} @ {t.get('session','?')}")
        print("\n## TOP 5 LOSERS")
        for t in top[-5:][::-1]:
            print(f"  {t.get('symbol')} ${pnl(t):+.2f}  {t.get('setup','?')} @ {t.get('session','?')}")

    print("\n## PROFILE LAUNCHERS")
    print("  start30.bat      — $30 micro Culturing Evolution (winner)")
    print("  start30-c2.bat   — $30 C2 adaptive weight evolution")
    print("  start30-c3.bat   — $30 C3 session edge specialist")
    print("  python scripts/historical_news_replay.py — news+growth historical replay")
    print("  start100.bat     — $100 small (+ US30m)")
    print("  start_growth.bat — full 14-symbol growth")
    print("  scripts\\kill_agent.bat — stop all agent processes")
    print("=" * 60)


if __name__ == "__main__":
    main()