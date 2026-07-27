"""Backfill missing position_mgmt sub-dicts onto trade_log records.

Reads position_mgmt_archive.jsonl (236 entries) + position_management.json
+ trade_manager.json and stamps the mgmt flags (break_even, partial_tp_done,
trailing, stale_closed) onto every trade_log record by ticket.

Why:
    399/400 historic trade_log records have position_mgmt=None and
    be_triggered=False, making the Profit Quality dashboard's BE/Partial/Stale
    splits 100% "not_triggered/unknown". The archive already carries the mgmt
    state for each closed position; this script just joins it back.

Safe to re-run (idempotent) — skips trades where position_mgmt already has
any truthy mgmt flag (live enrichment wins).

Usage:
    python scripts/backfill_trade_enrichment.py
    python scripts/backfill_trade_enrichment.py --dry-run   # preview only
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import read_json_state, write_json_state

_LOG = logging.getLogger("backfill_trade_enrichment")
_LOG.setLevel(logging.INFO)
_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
_LOG.addHandler(_console)


# Fields in the position_mgmt sub-dict that are meaningful
# for the Profit Quality dashboard's BE/Partial/Stale axes.
MGMT_FLAGS = ("break_even", "partial_tp_done", "trailing", "stale_closed")


def _normalize_mgmt_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a mgmt_row from the archive to clean booleans.

    The archive stores boolean-strings e.g. ``partial_tp_done: "False"``
    which must be normalized to Python ``False`` so the dashboard's
    ``bool(mgmt.get(...))`` works correctly.
    """
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, str) and v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        elif v is None:
            out[k] = None
        else:
            out[k] = v
    return out


def _has_truthy_mgmt(mgmt: dict[str, Any] | None) -> bool:
    """Return True if any meaningful mgmt flag is truthy.

    Mirrors the logic in core.utils._live_mgmt_has_truthy so the archive
    join is consistently skipped when live data already exists.
    """
    if not isinstance(mgmt, dict):
        return False
    return any(bool(mgmt.get(f)) for f in MGMT_FLAGS)


def _build_archive_index() -> dict[str, dict[str, Any]]:
    """Load position_mgmt_archive.jsonl into a {ticket -> mgmt_row} index.

    Returns an empty dict if the archive doesn't exist or is empty.
    """
    archive_path = ROOT / "state" / "position_mgmt_archive.jsonl"
    if not archive_path.exists():
        _LOG.warning("archive not found at %s", archive_path)
        return {}
    index: dict[str, dict[str, Any]] = {}
    with archive_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ticket = str(entry.get("ticket") or "")
            if not ticket:
                continue
            mgmt_raw = entry.get("mgmt_row") or {}
            index[ticket] = _normalize_mgmt_row(mgmt_raw)
    _LOG.info("loaded %d entries from archive index", len(index))
    return index


def _build_live_mgmt_index() -> dict[str, dict[str, Any]]:
    """Load position_management.json for any live mgmt data.

    Keys are ticket strings; values are mgmt rows from the live manager.
    """
    pm = read_json_state("position_management.json", default={}) or {}
    positions = pm.get("positions", {})
    if not isinstance(positions, dict):
        return {}
    index: dict[str, dict[str, Any]] = {}
    for ticket, row in positions.items():
        if isinstance(row, dict) and row:
            index[str(ticket)] = {
                "initial_sl": row.get("initial_sl"),
                "break_even": bool(row.get("break_even")),
                "partial_tp_done": bool(row.get("partial_tp_done")),
                "trailing": bool(row.get("trailing")),
                "stale_closed": bool(row.get("stale_closed")),
                "peak_price": row.get("peak_price"),
                "trail_distance": row.get("trail_distance"),
            }
    _LOG.info("loaded %d entries from live position_management", len(index))
    return index


def _build_stale_tickets() -> set[str]:
    """Load trade_manager.json stale_candidates and return ticket set."""
    tm = read_json_state("trade_manager.json", default={}) or {}
    stale = tm.get("stale_candidates") or []
    if not isinstance(stale, list):
        return set()
    tickets: set[str] = set()
    for s in stale:
        if isinstance(s, dict):
            t = s.get("ticket") or s.get("position_id")
            if t is not None:
                tickets.add(str(t))
    _LOG.info("built stale-ticket set: %d tickets", len(tickets))
    return tickets


def _match_ticket(trade: dict[str, Any], index: dict[str, dict[str, Any]]) -> str | None:
    """Try all ticket-like fields on a trade to find a match in the index.

    The archive was keyed by MT5 position ticket, but trade_log may use
    trade_id, mt5_deal, or mt5_position — any of which could be the same
    ticket value depending on the source broker.
    """
    for field in ("mt5_position", "trade_id", "ticket", "position_id", "mt5_deal"):
        kval = trade.get(field)
        if kval is not None and str(kval) in index:
            return str(kval)
    return None


def backfill(*, dry_run: bool = False) -> dict[str, int]:
    """Main backfill routine.

    Returns a summary dict with counts of trades enriched, skipped, etc.
    """
    # Load data sources
    archive_index = _build_archive_index()
    live_index = _build_live_mgmt_index()
    stale_tickets = _build_stale_tickets()

    # Read trade_log
    tl_data = read_json_state("trade_log.json", default={}) or {}
    trades = list(tl_data.get("trades") or [])
    if not trades:
        _LOG.warning("trade_log.json has no trades — nothing to backfill")
        return {"total": 0, "enriched": 0, "already_had_data": 0, "unmatched": 0}

    counts = {"total": len(trades), "enriched": 0, "already_had_data": 0, "unmatched": 0}

    for t in trades:
        existing_mgmt = t.get("position_mgmt")
        if isinstance(existing_mgmt, dict) and _has_truthy_mgmt(existing_mgmt):
            counts["already_had_data"] += 1
            continue

        # Try archive first, then live
        ticket = _match_ticket(t, archive_index)
        mgmt_source = None
        mgmt_row: dict[str, Any] = {}

        if ticket and ticket in archive_index:
            mgmt_row = dict(archive_index[ticket])
            mgmt_source = "archive"
        elif ticket and ticket in live_index:
            mgmt_row = dict(live_index[ticket])
            mgmt_source = "live"
        elif ticket and ticket in stale_tickets:
            # Stale flag from trade_manager — no other mgmt data available
            mgmt_row = {"stale_closed": True}
            mgmt_source = "stale_flag"

        if not mgmt_row:
            counts["unmatched"] += 1
            continue

        # Stamp the position_mgmt sub-dict
        mgmt_row["_source"] = mgmt_source
        t["position_mgmt"] = mgmt_row

        # Also stamp top-level be_triggered from the mgmt row
        if mgmt_row.get("break_even") is True:
            t["be_triggered"] = True
        elif "break_even" in mgmt_row:
            # Only override if we have explicit data from the archive
            t["be_triggered"] = bool(mgmt_row.get("break_even", False))

        counts["enriched"] += 1

    _LOG.info(
        "backfill complete: %d/%d enriched, %d already had data, %d unmatched",
        counts["enriched"], counts["total"],
        counts["already_had_data"], counts["unmatched"],
    )

    if not dry_run:
        tl_data["trades"] = trades
        write_json_state("trade_log.json", tl_data)
        _LOG.info("wrote updated trade_log.json (%d trades)", len(trades))
    else:
        _LOG.info("DRY RUN — no changes written")

    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill missing position_mgmt onto trade_log records")
    ap.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    args = ap.parse_args()
    counts = backfill(dry_run=args.dry_run)
    print(
        f"\nBackfill summary:"
        f"\n  Total trades:        {counts['total']}"
        f"\n  Enriched:            {counts['enriched']}"
        f"\n  Already had data:    {counts['already_had_data']}"
        f"\n  Unmatched (no data): {counts['unmatched']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
