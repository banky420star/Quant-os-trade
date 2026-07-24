"""run_canary_pick — advance the symbol canary state machine by one cycle.

Loads the latest ``state/edge_projection_*.json`` row produced by
``scripts/verify_edge.py`` plus the realized trade history from
``state/trade_log.json`` and ticks ``core.canary.tick``. Writes the
resulting audit row to ``state/canary_audit.jsonl`` and persists the
new state to ``state/canary_state.json``.

This CLI does NOT trade, does NOT mutate ``config.yaml``, and does NOT
write ``learning_config_overrides.json`` or ``config_proposals.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from core.canary import (  # noqa: E402
    append_audit,
    audit_path,
    load_latest_projection,
    load_state,
    save_state,
    state_path,
    tick,
)
from core.utils import (  # noqa: E402
    STATE_DIR,
    read_json_state,
    setup_logger,
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Advance the symbol canary A/B state by one daily tick."
    )
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be written; do not persist state.")
    ap.add_argument("--lookback-trades", type=int, default=2000,
                    help="How many recent closed trades to load for realised PnL.")
    args = ap.parse_args()

    log = setup_logger("run_canary_pick", "run_canary_pick.log")
    log.info("---- run_canary_pick start ----")
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    proj_path, projection_rows = load_latest_projection(STATE_DIR)
    log.info("Loaded %d projection rows from %s",
             len(projection_rows), proj_path or "<none>")

    trade_log = read_json_state("trade_log.json", default={}) or {}
    all_trades = (
        trade_log.get("trades", []) if isinstance(trade_log, dict) else trade_log
    )
    if not isinstance(all_trades, list):
        all_trades = []
    realized_trades = all_trades[-args.lookback_trades:]
    log.info("Loaded %d closed trades for realised PnL lookback",
             len(realized_trades))

    state = load_state()
    log.info("Loaded state status=%s symbol=%s history_len=%d",
             state.status, state.symbol, len(state.history))

    new_state, audit_row = tick(
        state, projection_rows,
        realized_trades=realized_trades,
        config=config, log=log,
    )
    log.info("Tick decision=%s new_status=%s",
             audit_row.get("decision"), new_state.status)

    if args.dry_run:
        print(json.dumps({
            "dry_run": True,
            "input_projection_file": str(proj_path) if proj_path else None,
            "input_projection_count": len(projection_rows),
            "previous_state": state.to_dict(),
            "new_state": new_state.to_dict(),
            "audit_row": audit_row,
        }, indent=2, default=str))
        return 0

    append_audit(audit_row, audit_path())
    save_state(new_state, state_path())
    log.info("Wrote canary_audit.jsonl + canary_state.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
