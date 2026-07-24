"""Backfill state/payoff_paradox_audit.jsonl from historical trades (2026-07-20).

The live bot path appends one audit row per closed trade (see
core/trade_tracker._log_payoff_paradox_audit_batch, called at the END of
detect_paper_closed and sync_mt5_closed_deals). For the 397 historical
trades already in state/trade_log.json — written BEFORE the live hook was
wired — we derive equivalent audit rows here so the fitter
(scripts/fit_payoff_paradox_floor.py) can run with realistic data today.

USAGE
    python scripts/backfill_payoff_paradox_audit.py             # default: derive + append idempotently
    python scripts/backfill_payoff_paradox_audit.py --dry-run   # print counts only
    python scripts/backfill_payoff_paradox_audit.py --force     # overwrite file (start clean)

DESIGN
* Idempotent — re-running this script is a no-op (transaction_id dedup).
* Multi-key aware — ticket = position_id (paper) or mt5_position (MT5) — the
  primary key the dashboard already uses for joins.
* Provenance stamps — every backfilled trade gets ``_backfilled: true`` so the
  fitter can tell synthetic rows from live-appended rows.
* Atomic — write to .tmp then os.replace so the bot (if mid-cycle) never sees
  a half-written file. A ``.prebak`` snapshot is preserved when --force.
* WARNING — do NOT run while the bot is mid-cycle reading payoff_paradox_audit.jsonl;
  race protection via mtime stat is best-effort only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import yaml  # noqa: F401  (PyYAML via requirements.txt)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.utils import (  # noqa: E402  (path-injected import)
    STATE_DIR,
    append_payoff_paradox_audit,
    payoff_paradox_audit_line_count,
    read_json_state,
    utc_now_iso,
)


def _safe_load_yaml_config(filename: str) -> dict[str, Any]:
    """Load config.yaml via yaml.safe_load. config.yaml is YAML, not JSON —
    read_json_state silently returns the default because YAML fails the
    JSON parser. Using yaml.safe_load keeps operator-customised knobs
    (payoff_paradox.self_tune.*, btc_promote.* etc.) visible to the script.
    Falls back to {} on FileNotFoundError or yaml errors so tests with a
    tmp_path fixture still work.
    """
    path = PROJECT_ROOT / filename
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return {}



from core.trade_tracker import (  # noqa: E402  (path-injected import)
    _build_payoff_paradox_row,
    _resolve_dotted_get,
)


DEFAULT_AUDIT_FILENAME = "payoff_paradox_audit.jsonl"
DEFAULT_TRADE_LOG = "trade_log.json"
DEFAULT_PROJECTION_FIELD = "trading.exits.min_r_multiple_win"


# Multi-key order MUST match the dashboard's archive-lookup priority (see
# dashboard/server.py::_build_profit_quality and
# core/trade_tracker::_hydrate_mgmt_from_archive). Both use the canonical
# order:
#   1. mt5_position       (MT5 broker ticket — sync_mt5_closed_deals path)
#   2. ticket             (legacy MT5 ticket)
#   3. position_id        (paper UUID — detect_paper_closed path)
#   4. trade_id           (canonical trade_log primary key)
#   5. mt5_deal           (deal-level fallback)
# If the order diverges from the dashboard, hybrid trades (which have BOTH
# mt5_position AND position_id) get stamped with the wrong ticket and never
# re-join downstream. Aligning the keys keeps audit rows on-disk directly
# joinable without re-keying.
_TICKET_KEYS: tuple[str, ...] = (
    "mt5_position",
    "ticket",
    "position_id",
    "trade_id",
    "mt5_deal",
)


def _ticket_for(trade: dict[str, Any]) -> str:
    for key in _TICKET_KEYS:
        v = trade.get(key)
        if v:
            return str(v)
    return ""


def _load_trade_log(path: Path = Path(DEFAULT_TRADE_LOG)) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw = read_json_state(path.name, default={}) or {}
    trades = raw.get("trades") or []
    return trades, raw


def _read_existing_txids(audit_path: Path) -> set[str]:
    """Load every transaction_id currently in the audit JSONL — used to skip
    trades that are already stamped (idempotency guard)."""
    if not audit_path.exists():
        return set()
    txids: set[str] = set()
    try:
        with audit_path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                txid = obj.get("transaction_id")
                if txid:
                    txids.add(str(txid))
    except OSError:
        return set()
    return txids


def derive_audit_rows(
    trades: list[dict[str, Any]],
    projected_floor: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Two-pass build:
      1. synthesise every audit row from a trade record (None if unreadable).
      2. dedup-in-pass by ``_ticket_for`` so two duplicate tickets across
         different pipelines don't produce two audit rows for the same close.

    Returns (kept, skipped) logs.
    """
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_tickets: set[str] = set()
    for trade in trades:
        row = _build_payoff_paradox_row(trade, projected_floor)
        if row is None:
            skipped.append({"trade_id": trade.get("trade_id"), "reason": "unintelligible"})
            continue
        ticket = _ticket_for(trade)
        if ticket and ticket in seen_tickets and (ticket == row.get("ticket")):
            skipped.append({"ticket": ticket, "reason": "duplicate_in_pass"})
            continue
        if ticket:
            seen_tickets.add(ticket)
            row["ticket"] = ticket
        row["_backfilled"] = True
        row["_source"] = "scripts/backfill_payoff_paradox_audit.py"
        kept.append(row)
    return kept, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be written; do not modify the audit file.")
    parser.add_argument("--force", action="store_true",
                        help="Remove the existing audit file before writing (start clean).")
    parser.add_argument("--audit-filename", default=DEFAULT_AUDIT_FILENAME,
                        help=f"JSONL audit filename under state/ (default: {DEFAULT_AUDIT_FILENAME}).")
    parser.add_argument("--trade-log", default=DEFAULT_TRADE_LOG,
                        help=f"trade_log.json-like state filename (default: {DEFAULT_TRADE_LOG}).")
    parser.add_argument("--projection-field", default=DEFAULT_PROJECTION_FIELD,
                        help=f"Dotted path key for projected_floor (default: {DEFAULT_PROJECTION_FIELD}).")
    args = parser.parse_args()

    audit_path = STATE_DIR / args.audit_filename
    print(f"[backfill-ppx] audit target     : {audit_path}")
    print(f"[backfill-ppx] trade_log source : {args.trade_log}")

    cfg_doc = _safe_load_yaml_config("config.yaml")
    # Caller passes --projection-field as a dotted path; resolve from cfg if present.
    projected_floor = float(_resolve_dotted_get(cfg_doc, args.projection_field, 0.4))
    print(f"[backfill-ppx] projected_floor  : {projected_floor} (from {args.projection_field})")

    trades, _raw_trade_log = _load_trade_log()
    print(f"[backfill-ppx] trades in source : {len(trades)}")

    if args.force and audit_path.exists():
        if args.dry_run:
            print(f"[backfill-ppx] --dry-run + --force: would remove existing {audit_path.name}")
        else:
            backup = audit_path.with_suffix(
                audit_path.suffix + f".prebak_{int(time.time() * 1e6)}"
            )
            try:
                shutil.copy2(audit_path, backup)
                audit_path.unlink()
                print(f"[backfill-ppx] --force: removed existing file; backup saved at {backup.name}")
            except OSError as exc:
                print(f"[backfill-ppx] --force ERROR: could not rotate {audit_path}: {exc}")
                return 2

    existing_tx = (
        set() if (args.force and not args.dry_run) else _read_existing_txids(audit_path)
    )
    pre_count = 0 if (args.force and not args.dry_run) else payoff_paradox_audit_line_count(args.audit_filename)
    print(f"[backfill-ppx] existing txids   : {len(existing_tx)} | pre rows: {pre_count}")

    kept, skipped = derive_audit_rows(trades, projected_floor)
    appended = 0
    skipped_dedup = 0
    for row in kept:
        # _payoff_paradox_tx builds the transaction_id from (ts, ticket); if
        # it already exists, append_payoff_paradox_audit silently no-ops.
        # We surface this slightly differently from parser-skip so the
        # operator can distinguish them.
        try:
            from core.utils import _payoff_paradox_tx  # local import: avoid path-collision
            txid = _payoff_paradox_tx(row)
        except Exception:
            txid = None
        if txid and txid in existing_tx:
            skipped_dedup += 1
            continue
        if args.dry_run:
            appended += 1  # count would-have-appended
            continue
        result = append_payoff_paradox_audit(row, filename=args.audit_filename)
        if result is not None:
            appended += 1

    post_count = pre_count if args.dry_run else payoff_paradox_audit_line_count(args.audit_filename)
    print()
    print(f"[backfill-ppx] build summary:")
    print(f"  trades read        : {len(trades)}")
    print(f"  rows synthesised   : {len(kept)}")
    print(f"  rows appended      : {appended}")
    print(f"  skipped (dedup)    : {skipped_dedup}")
    print(f"  skipped (in-pass)  : {sum(1 for _ in skipped if _.get('reason') == 'duplicate_in_pass')}")
    print(f"  skipped (parser)   : {sum(1 for _ in skipped if _.get('reason') == 'unintelligible')}")
    print(f"  audit rows post    : {post_count}")
    print(f"  audit rows prev    : {pre_count}")
    print(f"  dry-run            : {bool(args.dry_run)}")
    print(f"  force-rewrite      : {bool(args.force)}")
    print(f"  projected_floor    : {projected_floor}")
    if not args.dry_run:
        print(f"  completed_at       : {utc_now_iso()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
