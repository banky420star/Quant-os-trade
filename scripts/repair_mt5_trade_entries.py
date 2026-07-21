"""Re-sync closed trades from MT5 so entry/SL/R are correct.

Fixes historical paper_trades that recorded entry==exit (OUT price used as
entry when order join failed). Rebuilds from MT5 IN+OUT deals via
TradeTracker.sync_mt5_closed_deals, then rebuilds trade_log.json.

Usage (MT5 terminal must be running + logged in):
    python scripts/repair_mt5_trade_entries.py
    python scripts/repair_mt5_trade_entries.py --days 60 --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.mt5_connection_manager import MT5ConnectionManager
from core.trade_tracker import TradeTracker
from core.utils import load_config, read_json_state, setup_logger, write_json_state


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    log = setup_logger("repair_mt5_trade_entries", "repair_mt5_trade_entries.log")
    config = load_config()
    magic = int((config.get("execution") or {}).get("magic_number", 20250625))

    conn = MT5ConnectionManager(config, log)
    try:
        conn.connect()
    except Exception as exc:
        log.error("MT5 connect failed: %s", exc)
        print(f"FAIL: MT5 not available ({exc})")
        return 1

    try:
        existing = read_json_state("paper_trades.json", default={"trades": []}) or {}
        old = list(existing.get("trades") or [])
        bad_old = sum(
            1
            for t in old
            if t.get("entry") == t.get("exit")
            or t.get("sl") in (None, 0, "")
        )
        tracker = TradeTracker(log)
        # Full re-merge from deals (empty base → only MT5 truth)
        merged, added = tracker.sync_mt5_closed_deals([], magic=magic, days=args.days)
        good = sum(
            1
            for t in merged
            if t.get("entry") != t.get("exit") and t.get("sl") not in (None, 0, "")
        )
        with_r = sum(1 for t in merged if t.get("r_multiple") is not None)
        print(
            f"old_trades={len(old)} bad_approx={bad_old} | "
            f"mt5_resync={len(merged)} good_entry_sl={good} with_R={with_r}"
        )
        if args.dry_run:
            print("dry-run: not writing")
            return 0
        write_json_state(
            "paper_trades.json",
            {
                "timestamp": __import__("core.utils", fromlist=["utc_now_iso"]).utc_now_iso(),
                "trades": merged,
                "source": "repair_mt5_trade_entries",
                "days": args.days,
            },
        )
        try:
            from scripts.build_trade_log import build_log

            payload = build_log(config, days=args.days, log=log)
            write_json_state("trade_log.json", payload)
            print(
                f"trade_log rebuilt: total={payload.get('total')} "
                f"avg_R={payload.get('avg_R')} expectancy_R={payload.get('expectancy_R')} "
                f"pnl={payload.get('total_pnl')}"
            )
        except Exception as exc:
            log.warning("trade_log rebuild failed: %s", exc)
            print(f"paper_trades written; trade_log rebuild failed: {exc}")
        print("OK")
        return 0
    finally:
        conn.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
