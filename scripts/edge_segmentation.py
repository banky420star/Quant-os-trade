"""Segment the closed-trade edge database to find any sub-population with a
real, statistically-usable edge — the honest alternative to parameter tuning.

The 713-trade live record as a whole loses money (43% win, PF<1). The question
is whether any SLICE of it (by symbol / regime / session / trend alignment /
side) has a genuine positive expectancy over enough trades that the bot could
be restricted to it. If yes -> a real tradable edge. If no -> the setup
generator has no edge and needs redesign, not retuning.

This is read-only analysis of state/edge_database.json. It places no trades.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EDGE_DB = ROOT / "state" / "edge_database.json"


def _pf(win_pnl: float, loss_pnl: float) -> float:
    g = abs(loss_pnl)
    return round(win_pnl / g, 3) if g > 0 else float("inf")


def cell_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(trades)
    wins = [t for t in trades if t.get("result") == "win"]
    losses = [t for t in trades if t.get("result") == "loss"]
    other = n - len(wins) - len(losses)
    win_pnl = sum(float(t.get("pnl", 0) or 0) for t in wins)
    loss_pnl = sum(float(t.get("pnl", 0) or 0) for t in losses)  # negative
    total_pnl = sum(float(t.get("pnl", 0) or 0) for t in trades)
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "other": other,
        "win_rate_pct": round(100.0 * len(wins) / n, 1) if n else 0.0,
        "total_pnl": round(total_pnl, 2),
        "expectancy_per_trade": round(total_pnl / n, 3) if n else 0.0,
        "profit_factor": _pf(win_pnl, loss_pnl),
    }


def fmt_row(label: str, s: dict[str, Any]) -> str:
    return (f"{label:40s} n={s['n']:4d} wr={s['win_rate_pct']:5.1f}% "
            f"PF={str(s['profit_factor']):>6s} exp/trade={s['expectancy_per_trade']:+8.3f} "
            f"total=${s['total_pnl']:+.2f}")


def segment_by(trades: list[dict[str, Any]], key: str) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups[str(t.get(key) or "unknown")].append(t)
    return groups


def main() -> int:
    data = json.loads(EDGE_DB.read_text(encoding="utf-8"))
    records = data["records"]
    print(f"Edge DB: {len(records)} records\n")

    # Overall
    overall = cell_stats(records)
    print("OVERALL:")
    print("  " + fmt_row("ALL_TRADES", overall))

    # Date span — the single most important guard against reading a short,
    # directional period as an "edge". A 2-day record cannot validate a strategy.
    dates = sorted(t.get("closed_at", "") for t in records if t.get("closed_at"))
    if dates:
        from datetime import datetime
        d0 = datetime.fromisoformat(dates[0].replace("Z", "+00:00"))
        d1 = datetime.fromisoformat(dates[-1].replace("Z", "+00:00"))
        span_days = (d1 - d0).total_seconds() / 86400.0
        print(f"  date span: {dates[0][:10]} -> {dates[-1][:10]}  ({span_days:.1f} days)")
        if span_days < 30:
            print("  *** WARNING: record < 30 days. Segmentation patterns are almost "
                  "certainly period drift, not a tradable edge. Do not act on them. ***")
    print()

    # 1-D segments
    for key in ("symbol", "market_regime", "session", "volatility", "phase",
                "m5_trend", "m15_trend", "side", "exit_reason"):
        groups = segment_by(records, key)
        print(f"--- by {key} ---")
        rows = [(k, cell_stats(v)) for k, v in groups.items()]
        rows.sort(key=lambda r: r[1]["total_pnl"], reverse=True)
        for k, s in rows:
            tag = "  *" if s["n"] >= 20 and s["profit_factor"] != "inf" and s["profit_factor"] >= 1.2 and s["expectancy_per_trade"] > 0 else ""
            print("  " + fmt_row(f"{key}={k}", s) + tag)
        print()

    # 2-D: symbol x regime, symbol x session, regime x m5_trend_aligned
    def trend_aligned(t):
        m5 = t.get("m5_trend"); m15 = t.get("m15_trend"); side = t.get("side")
        if not m5 or not m15 or not side:
            return "unknown"
        bull = m5 in ("bullish", "up") and m15 in ("bullish", "up") and side == "BUY"
        bear = m5 in ("bearish", "down") and m15 in ("bearish", "down") and side == "SELL"
        return "aligned" if (bull or bear) else "counter"

    pairs = [
        ("symbol", "market_regime"),
        ("symbol", "session"),
        ("market_regime", "trend_aligned"),
        ("session", "market_regime"),
    ]
    for a, b in pairs:
        groups: dict[str, list[dict]] = defaultdict(list)
        for t in records:
            av = str(t.get(a) or "unknown")
            bv = str(trend_aligned(t)) if b == "trend_aligned" else str(t.get(b) or "unknown")
            groups[f"{av}|{bv}"].append(t)
        rows = [(k, cell_stats(v)) for k, v in groups.items()]
        rows = [r for r in rows if r[1]["n"] >= 15]
        if not rows:
            continue
        rows.sort(key=lambda r: r[1]["profit_factor"] if r[1]["profit_factor"] != "inf" else 9e9, reverse=True)
        print(f"--- by {a} x {b} (cells with n>=15) ---")
        for k, s in rows:
            tag = "  * CANDIDATE" if (s["n"] >= 30 and s["profit_factor"] != "inf"
                                      and s["profit_factor"] >= 1.25 and s["expectancy_per_trade"] > 0) else ""
            print("  " + fmt_row(f"{a}={k}", s) + tag)
        print()

    # Setup-type sanity check (data quality)
    sts = defaultdict(int)
    for t in records:
        sts[str(t.get("setup_type"))[:20]].append(1) if False else None
    unique_setups = len({str(t.get("setup_type")) for t in records})
    print(f"setup_type distinct values: {unique_setups} (if ~per-trade, the field is mis-populated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())