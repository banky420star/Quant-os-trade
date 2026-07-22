"""One-shot reset of learning_state.json with accurate counts from trade_log.

Reads the 401 trades in trade_log.json, re-reviews each with the fixed
detect_bad_session() (which now computes session from opened_at), aggregates
correct mistake_counts, rolling WR, expectancy, and per-symbol ratings, and
writes the result to state/learning_state.json.

This replaces the inflated ~81K counts caused by the pre-fix _new_closes()
re-review bug. After running, the dashboard shows accurate numbers immediately.
Run once; the learning loop picks up from the reset state going forward.

Usage:
    python scripts/reset_learning_state.py --dry-run   # preview only, no write
    python scripts/reset_learning_state.py              # write to state
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.trade_reviewer import review_trade
from core.utils import load_config, read_json_state, setup_logger, utc_now_iso, write_json_state

STATE_FILE = "learning_state.json"


def _rolling_stats(reviews: list[dict[str, Any]]) -> tuple[float, float]:
    if not reviews:
        return 50.0, 0.0
    wins = sum(1 for r in reviews if (r.get("net_profit") or 0) > 0)
    wr = 100.0 * wins / len(reviews)
    rs = [r.get("r_multiple") for r in reviews if r.get("r_multiple") is not None]
    exp = sum(rs) / len(rs) if rs else 0.0
    return round(wr, 1), round(exp, 3)


def reset_state(*, dry_run: bool = False) -> dict[str, Any]:
    log = setup_logger("reset_learning_state", "reset_learning_state.log")

    config = load_config()
    tl = read_json_state("trade_log.json", default={}) or {}
    trades = list(tl.get("trades") or [])
    log.info("Read %d trades from trade_log.json", len(trades))

    reviews: list[dict[str, Any]] = []
    for t in trades:
        rev = review_trade(t, config=config)
        reviews.append(rev)

    # --- Aggregate ---
    wr, exp = _rolling_stats(reviews)
    log.info("Reviewed %d trades -> WR=%.1f%% exp=%.3fR", len(reviews), wr, exp)

    # Mistake counts
    counts: Counter[str] = Counter()
    for r in reviews:
        for m in r.get("mistake_categories") or []:
            counts[m] += 1
    log.info("Mistake counts: %s", dict(counts))

    # Per-symbol ratings
    by_sym: dict[str, list[int]] = defaultdict(list)
    for r in reviews:
        rating = r.get("rating_total")
        sym = r.get("symbol")
        if rating is not None and sym:
            by_sym[sym].append(int(rating))
    avg_rating_by_symbol = {
        sym: round(sum(vals) / len(vals), 1)
        for sym, vals in by_sym.items()
    }

    # Recent ratings (last 200)
    recent_ratings = [
        r.get("rating_total") for r in reviews[-200:]
        if r.get("rating_total") is not None
    ]

    # Determine last reviewed trade ID (most recent close)
    last_trade_id = None
    for r in reversed(reviews):
        tid = r.get("trade_id")
        if tid:
            last_trade_id = tid
            break

    new_state = {
        "updated_at": utc_now_iso(),
        "mode": str((config.get("learning") or {}).get("mode", "observe_only")),
        "last_reviewed_trade_id": last_trade_id,
        "reviewed_count": len(reviews),
        "rolling_win_rate_pct": wr,
        "rolling_expectancy_r": exp,
        "rolling_drawdown_pct": 0.0,
        "avg_rating_by_symbol": avg_rating_by_symbol,
        "avg_rating_by_setup": {},
        "mistake_counts": dict(counts),
        "recent_ratings": recent_ratings,
        "active_proposals": [],
        "rejected_proposals": [],
        "applied_patches": [],
        "rollback_triggers": [],
    }

    log.info("New state: reviewed=%d mistakes=%s", new_state["reviewed_count"], dict(counts))

    if dry_run:
        print("=== DRY RUN — no file written ===")
        print(f"  reviewed_count: {new_state['reviewed_count']}")
        print(f"  win_rate: {new_state['rolling_win_rate_pct']}%")
        print(f"  expectancy: {new_state['rolling_expectancy_r']}R")
        print(f"  last_reviewed_trade_id: {new_state['last_reviewed_trade_id']}")
        print(f"  avg_rating_by_symbol: {avg_rating_by_symbol}")
        print(f"  mistake_counts: {dict(counts)}")
        print(f"  recent_ratings (count): {len(recent_ratings)}")
    else:
        write_json_state(STATE_FILE, new_state)
        log.info("Wrote state/%s (%d trades, %d reviews)",
                 STATE_FILE, len(trades), len(reviews))
        print("Written state/learning_state.json with corrected counts.")

    return new_state


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset learning_state.json with accurate counts")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; no file write")
    args = parser.parse_args()
    reset_state(dry_run=bool(args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
