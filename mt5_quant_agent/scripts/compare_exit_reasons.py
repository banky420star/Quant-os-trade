"""Compare exit_reason distribution before/after the DEAL_REASON mapping patch.

Usage:
    python scripts/compare_exit_reasons.py              # show baseline + live comparison\n    python scripts/compare_exit_reasons.py --overwrite  # replace baseline with current state

The script maintains ``state/exit_reason_baseline.json`` — a snapshot of the trade_log
exit_reason distribution BEFORE the 2026-07-21 DEAL_REASON mapping patch was active.
Rerunning without ``--overwrite`` compares the current live data against that frozen
baseline and prints the delta.

Expected improvement after 1 week of live trading:

    Exit reason          Before (baseline)   After (live)   Delta
    ──────────────────────────────────────────────────────────────
    mt5_close               399 (99.8%)         ?            -X
    take_profit               1 ( 0.2%)         ?            +Y
    stop_loss                 0 ( 0.0%)         ?            +Z
    break_even_stop           0 ( 0.0%)         ?            +W
    trailing_stop             0 ( 0.0%)         ?            +V
    stop_out                  0 ( 0.0%)         ?            +U
    partial_take_profit       0 ( 0.0%)         ?            +T

    Data quality:
      position_mgmt w/ flags: 3 -> ?
      be_triggered=True:       0 -> ?
      mae_R/mfe_R present:     0 -> ?

The key question is whether the -$127.11 loss from the 399 "mt5_close" trades can now
be attributed to specific exit modes (stop_loss, break_even_stop, trailing_stop, etc.)
once the new trades carry correct DEAL_REASON mapping + MAE/MFE data.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import read_json_state, write_json_state  # noqa: E402

BASELINE_FILE = "exit_reason_baseline.json"


def _extract_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the full metric set from a list of trade records."""
    n = len(trades)
    reasons = Counter(t.get("exit_reason", "?") for t in trades)
    pnl_by_reason: dict[str, float] = defaultdict(float)
    for t in trades:
        try:
            pnl = float(t.get("pnl", 0))
        except (TypeError, ValueError):
            pnl = 0.0
        pnl_by_reason[t.get("exit_reason", "?")] += pnl

    # Data quality
    has_mgmt = sum(
        1
        for t in trades
        if t.get("position_mgmt")
        and isinstance(t.get("position_mgmt"), dict)
        and any(bool(v) for v in t["position_mgmt"].values())
    )
    be_true = sum(1 for t in trades if t.get("be_triggered") is True)
    has_mae = sum(1 for t in trades if t.get("mae_R") is not None)
    has_mfe = sum(1 for t in trades if t.get("mfe_R") is not None)

    # WR/payoff per exit reason
    wr_by_reason: dict[str, dict[str, Any]] = {}
    for reason in sorted(reasons.keys()):
        subset = [t for t in trades if t.get("exit_reason", "?") == reason]
        wins = sum(1 for t in subset if float(t.get("pnl", 0)) > 0)
        losses = len(subset) - wins
        total_pnl = pnl_by_reason.get(reason, 0.0)
        avg_w = (
            sum(float(t.get("pnl", 0)) for t in subset if float(t.get("pnl", 0)) > 0)
            / max(wins, 1)
        )
        avg_l = (
            sum(float(t.get("pnl", 0)) for t in subset if float(t.get("pnl", 0)) <= 0)
            / max(losses, 1)
        )
        wr_by_reason[reason] = {
            "n": len(subset),
            "wins": wins,
            "losses": losses,
            "wr_pct": round(wins / max(len(subset), 1) * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(avg_w, 4),
            "avg_loss": round(avg_l, 4),
            "payoff": (
                round(abs(avg_w / max(abs(avg_l), 0.001)), 2)
                if wins > 0 and losses > 0
                else None
            ),
        }

    # Symbol x exit_reason cross-tab
    cross: dict[str, Counter] = defaultdict(Counter)
    for t in trades:
        cross[t.get("symbol", "?")][t.get("exit_reason", "?")] += 1
    cross_tab = {
        sym: dict(cx.most_common()) for sym, cx in sorted(cross.items())
    }

    # BE-triggered breakdown
    be_wins = sum(
        1
        for t in trades
        if t.get("be_triggered") is True and float(t.get("pnl", 0)) > 0
    )
    be_losses = sum(
        1
        for t in trades
        if t.get("be_triggered") is True and float(t.get("pnl", 0)) <= 0
    )

    # MAE/MFE basic stats
    mae_vals = [
        float(t["mae_R"])
        for t in trades
        if t.get("mae_R") is not None and isinstance(t.get("mae_R"), (int, float))
    ]
    mfe_vals = [
        float(t["mfe_R"])
        for t in trades
        if t.get("mfe_R") is not None and isinstance(t.get("mfe_R"), (int, float))
    ]

    return {
        "n_total": n,
        "total_pnl": round(sum(float(t.get("pnl", 0)) for t in trades), 2),
        "exit_reason_distribution": dict(reasons.most_common()),
        "pnl_by_exit_reason": {k: round(v, 2) for k, v in sorted(pnl_by_reason.items(), key=lambda x: x[1])},
        "wr_payoff_by_exit_reason": wr_by_reason,
        "data_quality": {
            "trades_with_position_mgmt": has_mgmt,
            "be_triggered_true": be_true,
            "be_wins": be_wins,
            "be_losses": be_losses,
            "mae_R_present": has_mae,
            "mfe_R_present": has_mfe,
            "mae_R_avg": round(sum(mae_vals) / max(len(mae_vals), 1), 4) if mae_vals else None,
            "mfe_R_avg": round(sum(mfe_vals) / max(len(mfe_vals), 1), 4) if mfe_vals else None,
        },
        "symbol_cross_tab": cross_tab,
    }


def report(baseline: dict[str, Any], live: dict[str, Any]) -> str:
    """Pretty-print the delta between baseline and live."""
    lines: list[str] = []

    def _sep(title: str = "") -> None:
        lines.append("")
        if title:
            lines.append(f"  {title}")
            lines.append(f"  {'-' * len(title)}")

    def _val(d: dict[str, Any], k: str, default: Any = "?") -> str:
        v = d.get(k, default)
        if v is None:
            return "N/A"
        return str(v)

    _sep("EXIT REASON DISTRIBUTION DELTA")
    lines.append(
        f"  {'Exit reason':>25s} {'Baseline':>10s} {'Live':>10s} {'Delta':>10s}"
    )
    lines.append(f"  {'-' * 58}")
    b_reasons = baseline.get("exit_reason_distribution", {})
    l_reasons = live.get("exit_reason_distribution", {})
    all_reasons = sorted(set(b_reasons.keys()) | set(l_reasons.keys()))
    for r in all_reasons:
        b_count = b_reasons.get(r, 0)
        l_count = l_reasons.get(r, 0)
        b_pct = b_count / max(baseline.get("n_total", 1), 1) * 100
        l_pct = l_count / max(live.get("n_total", 1), 1) * 100
        delta_count = l_count - b_count
        delta_pct = l_pct - b_pct
        lines.append(
            f"  {r:>25s} {b_count:>4d} ({b_pct:>4.1f}%) {l_count:>4d} ({l_pct:>4.1f}%) "
            f"{delta_count:+d} ({delta_pct:+.1f}%)"
        )

    _sep("TOTAL PnL")
    b_pnl = baseline.get("total_pnl", 0)
    l_pnl = live.get("total_pnl", 0)
    delta_pnl = l_pnl - b_pnl
    lines.append(f"  Baseline: ${b_pnl:.2f}   Live: ${l_pnl:.2f}   Delta: ${delta_pnl:.2f}")

    _sep("PnL BY EXIT REASON")
    lines.append(f"  {'Exit reason':>25s} {'Baseline':>10s} {'Live':>10s} {'Delta':>10s}")
    lines.append(f"  {'-' * 58}")
    b_pnl_reason = baseline.get("pnl_by_exit_reason", {})
    l_pnl_reason = live.get("pnl_by_exit_reason", {})
    for r in all_reasons:
        b_p = b_pnl_reason.get(r, 0)
        l_p = l_pnl_reason.get(r, 0)
        lines.append(
            f"  {r:>25s} ${b_p:>7.2f} ${l_p:>7.2f} ${(l_p - b_p):+>.2f}"
        )

    _sep("WR / PAYOFF (live)")
    wr_live = live.get("wr_payoff_by_exit_reason", {})
    lines.append(
        f"  {'Exit reason':>25s} {'n':>4s} {'WR%':>5s} {'Avg W$':>9s} {'Avg L$':>9s} {'Payoff':>7s}"
    )
    lines.append(f"  {'-' * 63}")
    for r in sorted(wr_live.keys()):
        w = wr_live[r]
        lines.append(
            f"  {r:>25s} {w['n']:4d} {w['wr_pct']:>4.1f}% "
            f"${w['avg_win']:>7.4f} ${w['avg_loss']:>7.4f} "
            f"{_val(w, 'payoff', 'inf')}"
        )

    _sep("DATA QUALITY")
    b_dq = baseline.get("data_quality", {})
    l_dq = live.get("data_quality", {})
    dq_keys = [
        ("trades_with_position_mgmt", "position_mgmt w/ flags"),
        ("be_triggered_true", "be_triggered=True"),
        ("mae_R_present", "mae_R data"),
        ("mfe_R_present", "mfe_R data"),
        ("mae_R_avg", "avg mae_R"),
        ("mfe_R_avg", "avg mfe_R"),
    ]
    for k, label in dq_keys:
        bv = b_dq.get(k, "?")
        lv = l_dq.get(k, "?")
        if isinstance(bv, (int, float)) and isinstance(lv, (int, float)):
            if isinstance(bv, float) and isinstance(lv, float):
                lines.append(f"  {label:>30s}: {bv:.4f} → {lv:.4f} ({lv-bv:+.4f})")
            else:
                lines.append(f"  {label:>30s}: {bv} → {lv} ({lv-bv:+d})")
        else:
            lines.append(f"  {label:>30s}: {bv} → {lv}")

    _sep("SYMBOL CROSS-TAB (live)")
    sym_tab = live.get("symbol_cross_tab", {})
    for sym, cx in sorted(sym_tab.items()):
        parts = ", ".join(f"{r}={c}" for r, c in sorted(cx.items(), key=lambda x: -x[1]))
        lines.append(f"  {sym:>12s} (n={sum(cx.values()):4d}): {parts}")

    # Key question
    _sep("KEY QUESTION")
    b_unexplained = b_pnl_reason.get("mt5_close", 0)
    l_new_reasons = {r: p for r, p in l_pnl_reason.items() if r != "mt5_close"}
    l_unexplained = l_pnl_reason.get("mt5_close", 0)
    lines.append(
        f"  Loss attributed to 'mt5_close': ${b_unexplained:.2f} → ${l_unexplained:.2f}"
    )
    if l_new_reasons:
        lines.append(
            f"  Loss re-attributed to specific exit modes: "
            + ", ".join(f"{r}=${p:.2f}" for r, p in sorted(l_new_reasons.items(), key=lambda x: -abs(x[1])))
        )

    return "\n".join(lines)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Compare exit_reason distribution before/after DEAL_REASON patch"
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace baseline snapshot with current trade_log state",
    )
    args = ap.parse_args()

    live_data = read_json_state("trade_log.json", default={}) or {}
    live_trades = live_data.get("trades", []) if isinstance(live_data, dict) else []

    if not live_trades:
        print("ERROR: No trades found in state/trade_log.json")
        return 1

    live_metrics = _extract_metrics(live_trades)

    if args.overwrite:
        write_json_state(BASELINE_FILE, live_metrics)
        print(f"Baseline overwritten — {len(live_trades)} trades frozen.")
        print(report(live_metrics, live_metrics))
        return 0

    # Read existing baseline
    baseline = read_json_state(BASELINE_FILE, default=None)
    if baseline is None:
        # No baseline exists — offer to create one
        print(
            f"No baseline found at state/{BASELINE_FILE}.\n"
            f"Run with --overwrite to capture the current {len(live_trades)} trades\n"
            f"as the pre-patch baseline, then re-run after a week of live trading."
        )
        return 0

    # Compare
    print(report(baseline, live_metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
