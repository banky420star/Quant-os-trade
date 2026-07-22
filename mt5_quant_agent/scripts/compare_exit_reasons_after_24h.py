"""Compare exit_reason distributions before and after the BE/TP/SL fixes.

Run this after 24h of live trading with BE@1.0R + TP1@1.65R + SL@2.5ATR active.
Shows both the overall shift and the "new trades only" breakdown.

Usage:
  python scripts/compare_exit_reasons_after_24h.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"

BASELINE_FILE = STATE_DIR / "exit_reason_baseline.json"


def _fmt(v: float, width: int = 10) -> str:
    return f"{v:>{width}.2f}"


def _pct(v: int, total: int) -> str:
    if total <= 0:
        return "  N/A  "
    return f"{100 * v / total:>6.1f}%"


def load_trade_log() -> list[dict]:
    tl = json.loads((STATE_DIR / "trade_log.json").read_text(encoding="utf-8"))
    return list(tl.get("trades") or [])


def compute_stats(trades: list[dict]) -> dict:
    """Return exit-reason-level stats matching the baseline format."""
    exit_reasons: Counter = Counter()
    pnl_by_reason: dict[str, float] = defaultdict(float)
    wins_by_reason: Counter = Counter()
    losses_by_reason: Counter = Counter()
    r_by_reason: dict[str, list[float]] = defaultdict(list)

    for t in trades:
        er = t.get("exit_reason", "unknown")
        pnl = float(t.get("pnl", 0) or 0)
        r = t.get("r_multiple")

        exit_reasons[er] += 1
        pnl_by_reason[er] += pnl
        if pnl > 0:
            wins_by_reason[er] += 1
        elif pnl < 0:
            losses_by_reason[er] += 1
        if r is not None:
            r_by_reason[er].append(float(r))

    total = len(trades)
    stats: dict = {"n_total": total}
    for reason in exit_reasons:
        cnt = exit_reasons[reason]
        r_vals = r_by_reason.get(reason, [])
        stats[reason] = {
            "count": cnt,
            "pct": round(100 * cnt / total, 1),
            "net_pnl": round(pnl_by_reason[reason], 2),
            "wins": wins_by_reason[reason],
            "losses": losses_by_reason[reason],
            "wr_pct": round(100 * wins_by_reason[reason] / max(cnt, 1), 1),
            "avg_r": round(sum(r_vals) / len(r_vals), 3) if r_vals else 0.0,
        }
    return stats


def print_header(label: str) -> None:
    print()
    print("=" * 110)
    print(f"  {label}")
    print("=" * 110)


def print_stats_table(
    stats: dict,
    baseline: dict | None = None,
    label: str = "Stats",
    total_trades: int = 0,
) -> None:
    """Print a side-by-side table of exit reason stats, optionally with baseline delta."""
    n_total = stats.get("n_total", total_trades)

    # Collect all exit reasons (as dict values, not metadata keys)
    reasons = sorted(
        k for k, v in stats.items()
        if isinstance(v, dict) and "count" in v
    )

    has_baseline = baseline is not None

    if has_baseline:
        print(f"  {'Exit Reason':<25} {'Count':>6} {'Distrib':>8} {'Net PnL':>10} {'WR':>7}")
        print(f"  {'-'*25:25} {'-'*6:6} {'-'*8:8} {'-'*10:10} {'-'*7:7}")
        for reason in reasons:
            s = stats.get(reason, {})
            if not isinstance(s, dict):
                continue
            cnt = s.get("count", 0)
            pnl = s.get("net_pnl", 0)
            wr = s.get("wr_pct", 0)
            print(
                f"  {reason:<25} {cnt:>6} {_pct(cnt, n_total):>8} "
                f"${pnl:>8.2f} {wr:>6.1f}%"
            )
    else:
        # Full comparison table with baseline delta
        base_reasons = set()
        for k, v in (baseline or {}).items():
            if isinstance(v, dict) and "count" in v:
                base_reasons.add(k)

        all_reasons = sorted(reasons | base_reasons)

        print(
            f"  {'Exit Reason':<25} {'Before':>14} {'After':>14} "
            f"{'D Count':>8} {'Before $':>12} {'After $':>12} {'D PnL':>10}"
        )
        print(
            f"  {'-'*25:25} {'-'*14:14} {'-'*14:14} "
            f"{'-'*8:8} {'-'*12:12} {'-'*12:12} {'-'*10:10}"
        )

        b_total = 0
        a_total = 0
        b_pnl_total = 0.0
        a_pnl_total = 0.0

        for reason in all_reasons:
            b = (baseline or {}).get(reason, {})
            a = stats.get(reason, {})
            b_count = b.get("count", 0) if isinstance(b, dict) else 0
            a_count = a.get("count", 0) if isinstance(a, dict) else 0
            b_pnl = b.get("net_pnl", 0) if isinstance(b, dict) else 0
            a_pnl = a.get("net_pnl", 0) if isinstance(a, dict) else 0
            delta_count = a_count - b_count
            delta_pnl = round(a_pnl - b_pnl, 2)

            b_total += b_count
            a_total += a_count
            b_pnl_total += b_pnl
            a_pnl_total += a_pnl

            b_pct_str = _pct(b_count, baseline.get("n_total", 1)) if baseline else "  N/A  "
            a_pct_str = _pct(a_count, n_total) if n_total else "  N/A  "

            print(
                f"  {reason:<25} "
                f"{b_count:>5}{b_pct_str:>9} "
                f"{a_count:>5}{a_pct_str:>9} "
                f"{delta_count:>+5}    "
                f"${b_pnl:>8.2f}  ${a_pnl:>8.2f}  ${delta_pnl:>+8.2f}"
            )

        d_total = a_total - b_total
        d_pnl_total = round(a_pnl_total - b_pnl_total, 2)
        print(
            f"  {'TOTAL':<25} "
            f"{b_total:>5}          "
            f"{a_total:>5}          "
            f"{d_total:>+5}    "
            f"${b_pnl_total:>8.2f}  ${a_pnl_total:>8.2f}  ${d_pnl_total:>+8.2f}"
        )

    print()


def print_wr_table(
    stats: dict,
    baseline: dict | None = None,
    label: str = "Win Rate",
) -> None:
    """Print win rate comparison by exit reason."""
    reasons = sorted(
        k for k, v in stats.items()
        if isinstance(v, dict) and "count" in v
    )
    print(f"  {label}:")
    print(f"  {'Exit Reason':<25} {'Before WR':>10} {'After WR':>10} {'D WR':>8}")
    print(f"  {'-'*25:25} {'-'*10:10} {'-'*10:10} {'-'*8:8}")
    for reason in reasons:
        s = stats.get(reason, {})
        if not isinstance(s, dict):
            continue
        a_wr = s.get("wr_pct", 0)
        if baseline:
            b = baseline.get(reason, {})
            b_wr = b.get("wr_pct", 0) if isinstance(b, dict) else 0
            d_wr = round(a_wr - b_wr, 1)
        else:
            b_wr = 0
            d_wr = 0
        print(f"  {reason:<25} {b_wr:>7.1f}%   {a_wr:>7.1f}%   {d_wr:>+6.1f}%")
    print()


def main():
    if not BASELINE_FILE.exists():
        print(f"ERROR: Baseline file not found at {BASELINE_FILE}")
        print("Run the baseline capture first.")
        sys.exit(1)

    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    trades = load_trade_log()
    baseline_n = baseline.get("n_total", 0)

    print(f"Loaded {len(trades)} trades from trade_log.json")
    print(f"Baseline at capture: {baseline_n} trades ({baseline.get('description', '?')})")

    # Split trades by baseline capture timestamp
    baseline_ts = baseline.get("captured_at", "")
    after_fixes: list[dict] = []
    before_fixes: list[dict] = []
    if baseline_ts:
        for t in trades:
            ts = t.get("closed_at", "") or ""
            if ts > baseline_ts:
                after_fixes.append(t)
            else:
                before_fixes.append(t)
        print(f"  Before fixes: {len(before_fixes)} trades")
        print(f"  After fixes:  {len(after_fixes)} trades")
    else:
        after_fixes = trades

    # --- SECTION 1: Full comparison (all trades vs baseline) ---
    current_stats = compute_stats(trades)
    print_header("1. OVERALL COMPARISON - All trades vs Baseline")
    print_stats_table(current_stats, baseline=baseline.get("exit_reasons"))
    print_wr_table(current_stats, baseline=baseline.get("exit_reasons"))

    # --- SECTION 2: New trades only ---
    if after_fixes:
        new_stats = compute_stats(after_fixes)
        print_header(f"2. NEW TRADES ONLY - {len(after_fixes)} closes since fixes")
        print_stats_table(new_stats, label="New trades", total_trades=len(after_fixes))
        print_wr_table(new_stats, label="New trades WR")
    else:
        print_header("2. NEW TRADES ONLY - (none yet)")
        print("  No new trades have closed since the baseline was captured.")
        print("  Let the bot accumulate closes, then re-run this script.")
        print()

    # --- SECTION 3: Summary + Recommendations ---
    print_header("3. SUMMARY")
    if after_fixes:
        n_new = len(after_fixes)
        new_pnl = sum(float(t.get("pnl", 0) or 0) for t in after_fixes)
        new_wins = sum(1 for t in after_fixes if float(t.get("pnl", 0) or 0) > 0)
        new_losses = sum(1 for t in after_fixes if float(t.get("pnl", 0) or 0) < 0)
        new_wr = 100 * new_wins / max(n_new, 1)

        # Count exit reasons in new trades
        new_exit_reasons = Counter(t.get("exit_reason", "unknown") for t in after_fixes)

        print(f"  New closes:     {n_new}")
        print(f"  Net PnL:        ${new_pnl:.2f}")
        print(f"  Win rate:       {new_wr:.1f}%")
        print(f"  Wins/Losses:    {new_wins}/{new_losses}")
        print(f"  Exit reasons:   {dict(new_exit_reasons.most_common())}")
        print()

        # Compare exit reason profile of new trades vs baseline
        baseline_reasons = baseline.get("exit_reasons", {})
        print(f"  Exit reason profile shift (new vs baseline):")
        new_reasons_total = sum(new_exit_reasons.values())
        base_reasons_total = baseline.get("n_total", 1)
        for reason in sorted(set(list(new_exit_reasons.keys()) + list(baseline_reasons.keys()))):
            new_pct = 100 * new_exit_reasons.get(reason, 0) / max(new_reasons_total, 1)
            base_pct = baseline_reasons.get(reason, {}).get("pct", 0) if isinstance(baseline_reasons.get(reason), dict) else 0
            delta_pct = round(new_pct - base_pct, 1)
            direction = "+" if delta_pct > 0 else ""
            print(f"    {reason:<25}: {new_pct:>5.1f}% (was {base_pct}%) [{direction}{delta_pct:.1f}%]")

        print()

        if n_new >= 25:
            print("  ✅ 25+ new trades - meaningful sample for re-tuning.")
            print("  Run:  python scripts/tune_persymbol_be.py --dry-run")
            print("  Run:  python -c 'from loops.learning_review_loop import run; run()'")
        elif n_new >= 10:
            print("  ⚠️ 10+ new trades - early signal. Wait for 25+ before re-tuning.")
        else:
            print("  ⏳ Less than 10 new trades. Let the bot accumulate more.")
    else:
        print("  No new trades yet. Check back after the bot has been running for 24h.")
    print()


if __name__ == "__main__":
    main()
