"""One-shot force-rebuild of state/trade_log.json.

The trade_log_loop.py rebuild is mtime-gated (only fires when paper_trades.json
OR paper_orders.json has changed since the last rebuild). On cold boots, after
profile swaps, or whenever the live state rolls, the cache can be empty even
though state/quant_os.db contains hundreds of closed trades. This entrypoint:

  1. Calls scripts/build_trade_log.build_log() directly.
  2. Writes both state/trade_log.json AND state/trade_log_meta.json atomically.
  3. Logs the rebuild count + source mtimes so the user can see what populated.

Usage:
    python scripts/rebuild_trade_log.py            # default 30-day window
    python scripts/rebuild_trade_log.py --days 90  # widen window
    python scripts/rebuild_trade_log.py --dry-run # don't write, just sniff

The script is idempotent: re-running just overwrites with the latest build.

See scripts/build_trade_log.py for the join + drawdown + R-multiple logic.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import (  # noqa: E402
    setup_logger,
    read_json_state,
    write_json_state,
)
from core.trade_history import trade_history_filename, trade_orders_filename  # noqa: E402
from scripts.build_trade_log import build_log  # noqa: E402

STATE = ROOT / "state"


def _mtime(path: Path) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Force-rebuild state/trade_log.json.")
    parser.add_argument("--days", type=int, default=30, help="History window in days (default 30).")
    parser.add_argument("--dry-run", action="store_true", help="Build but do not write.")
    args = parser.parse_args(argv)

    logger = setup_logger("rebuild_trade_log", "rebuild_trade_log.log")

    try:
        from core.utils import load_config
        config = load_config()
    except Exception as cfg_exc:  # noqa: BLE001
        logger.error("load_config failed: %s", cfg_exc)
        config = {}
    trades_path = STATE / trade_history_filename(config)
    orders_path = STATE / trade_orders_filename(config)
    trades_m = _mtime(trades_path)
    orders_m = _mtime(orders_path)
    logger.info(
        "Force rebuild initiated. %s mtime=%.0f %s mtime=%.0f days=%d",
        trades_path.name, trades_m, orders_path.name, orders_m, args.days,
    )

    try:
        payload = build_log(config, days=args.days, log=logger)
    except Exception as exc:  # noqa: BLE001
        logger.error("build_log failed: %s", exc)
        print(f"ERROR: build_log failed: {exc}", file=sys.stderr)
        return 2

    if isinstance(payload, list):
        n = len(payload)
    elif isinstance(payload, dict):
        n = payload.get("total", len(payload.get("trades") or []))
    else:
        n = 0
    logger.info("build_log produced %d trade records.", n)

    if args.dry_run:
        print(f"[dry-run] would have written {n} trades to state/trade_log.json")
        prev = read_json_state("trade_log.json", default=[]) or []
        if not isinstance(prev, list):
            prev = []
        print(f"[dry-run] current cache has {len(prev)} trades.")
        return 0

    try:
        write_json_state("trade_log.json", payload)
        write_json_state("trade_log_meta.json", {
            "trades_file": trades_path.name,
            "orders_file": orders_path.name,
            "trades_mtime": trades_m,
            "orders_mtime": orders_m,
            "forced_rebuild_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": "scripts/rebuild_trade_log.py",
            "trade_count": n,
        })
    except Exception as exc:  # noqa: BLE001
        logger.error("write_json_state failed: %s", exc)
        print(f"ERROR: write failed: {exc}", file=sys.stderr)
        return 2

    print(f"OK: state/trade_log.json rebuilt with {n} trades (window={args.days}d).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
