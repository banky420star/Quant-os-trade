"""One-time archive repair — backfill clean setup/context onto historical closes.

USER-AUTHORIZED 2026-07-01 (Workstream A of the data-driven culturing plan).
Historical closed-trade records in state/paper_trades.json are polluted: many
have ``setup_type`` = the MT5 OUT-deal comment (e.g. ``[sl 4031.33]``) instead
of the real setup, because the live enrichment join (Fix 1,
core/trade_tracker.sync_mt5_closed_deals) only landed recently. That noise
made any per-setup optimization unreliable.

This script re-derives the clean label for every historical close by replaying
the Fix-1 join against the MT5 deal history and the opening order in
state/paper_orders.json:

  OUT deal (magic 20250625) --position_id--> paper_orders.mt5_ticket --> order
  order.signal_meta  -->  setup / regime / confidence / market_context
  normalize_setup_type(...)  -->  clean setup label

Recovered records get their fields rewritten in place; unrecoverable records
(no matching order, or still polluted) are quarantined as
``setup_type="unknown", archive_polluted=true`` so the forward-test ledger
skips them. state/edge_database.json edge records are re-labeled to match by
trade_id and their aggregates recomputed.

Read-only on MT5 (history_deals_get only). Run once:
    python scripts/repair_archive.py --dry-run   # print before/after, no writes
    python scripts/repair_archive.py             # write the repaired files
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore

from core.edge_database import EdgeDatabase  # noqa: E402
from core.mt5_connection_manager import MT5ConnectionManager  # noqa: E402
from core.strategy_policy import KNOWN_SETUPS, normalize_setup_type  # noqa: E458, E402
from core.utils import load_config, read_json_state, setup_logger, write_json_state  # noqa: E402


def _build_ticket_index(orders_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """mt5 position ticket -> opening order (status filled). Mirrors Fix 1."""
    index: dict[str, dict[str, Any]] = {}
    for o in (orders_state.get("orders") or []):
        tkt = o.get("mt5_ticket")
        if tkt and o.get("status") == "filled":
            index[str(tkt)] = o
    return index


def _recover_from_deal(deal, ticket_index: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Recover the clean enriched record for one OUT deal (Fix-1 replay)."""
    pos_id = getattr(deal, "position_id", None)
    order = ticket_index.get(str(pos_id)) if pos_id else None
    meta = (order or {}).get("signal_meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    mc = meta.get("market_context") if isinstance(meta.get("market_context"), dict) else {}
    setup_type = normalize_setup_type(
        (order or {}).get("setup_type") or meta.get("setup_type") or (deal.comment or ""),
        meta=meta,
        market_context=mc,
    )
    entry_price = float(order.get("fill_price")) if order and order.get("fill_price") else float(deal.price)
    return {
        "trade_id": str(deal.ticket),
        "mt5_deal": deal.ticket,
        "mt5_position": int(pos_id) if pos_id else None,
        "signal_id": (order or {}).get("signal_id") or meta.get("signal_id"),
        "symbol": deal.symbol,
        "side": "BUY" if deal.type == mt5.DEAL_TYPE_BUY else "SELL",
        "entry": entry_price,
        "exit": float(deal.price),
        "sl": meta.get("sl"),
        "tp1": meta.get("tp1"),
        "pnl": float(deal.profit),
        "result": "win" if deal.profit > 0 else "loss",
        "exit_reason": "mt5_close",
        "setup_type": (order or {}).get("setup_type") or meta.get("setup_type") or setup_type or "unknown",
        "reason": (order or {}).get("reason") or meta.get("reason"),
        "signal_meta": meta,
        "confidence": meta.get("confidence"),
        "confidence_tree": meta.get("confidence_tree"),
        "evidence": meta.get("evidence"),
        "market_context": meta.get("market_context"),
        "closed_at": datetime.fromtimestamp(int(deal.time), tz=timezone.utc).isoformat(),
        "archive_polluted": False,
        "_recovered": True,
    }


def _is_polluted(setup_type: str | None) -> bool:
    raw = (setup_type or "").strip()
    return not raw or raw not in KNOWN_SETUPS


def repair_paper_trades(
    recovered: dict[str, dict[str, Any]],
    dry_run: bool,
    log,
) -> dict[str, int]:
    """Rewrite paper_trades.json records with clean labels. Returns counts."""
    state = read_json_state("paper_trades.json", default={"trades": []})
    trades = state.get("trades", []) if isinstance(state, dict) else []
    counts = {"total": len(trades), "recovered": 0, "quarantined": 0, "unchanged": 0}
    sample: list[tuple[str, str]] = []

    for t in trades:
        tid = t.get("trade_id")
        before = t.get("setup_type") or ""
        rec = recovered.get(str(tid)) if tid is not None else None
        if rec is not None:
            # Overwrite the context-bearing fields with the recovered clean ones.
            for k in (
                "mt5_deal", "mt5_position", "signal_id", "entry", "exit",
                "sl", "tp1", "setup_type", "reason", "signal_meta",
                "confidence", "confidence_tree", "evidence", "market_context",
                "archive_polluted",
            ):
                if rec.get(k) is not None:
                    t[k] = rec[k]
            t["archive_polluted"] = False
            counts["recovered"] += 1
            if _is_polluted(before) and len(sample) < 12:
                sample.append((before, t.get("setup_type") or ""))
        else:
            # No matching deal/order — normalize from whatever context the
            # record already carries; quarantine if still unresolvable.
            mc = t.get("market_context") or {}
            meta = t.get("signal_meta") or {}
            clean = normalize_setup_type(
                t.get("setup_type"),
                meta=meta if isinstance(meta, dict) else {},
                market_context=mc if isinstance(mc, dict) else {},
            )
            t["setup_type"] = clean
            if _is_polluted(clean):
                t["setup_type"] = "unknown"
                t["archive_polluted"] = True
                counts["quarantined"] += 1
                if len(sample) < 12:
                    sample.append((before, "unknown (quarantined)"))
            else:
                t["archive_polluted"] = False
                counts["unchanged"] += 1

    if sample:
        log.info("before -> after sample:")
        for b, a in sample:
            log.info("  %r -> %r", b, a)
    log.info(
        "paper_trades: %d total, %d recovered, %d quarantined, %d clean-kept",
        counts["total"], counts["recovered"], counts["quarantined"], counts["unchanged"],
    )
    if not dry_run:
        write_json_state("paper_trades.json", state)
    return counts


def repair_edge_database(dry_run: bool, log) -> int:
    """Re-label edge DB records to match the repaired paper_trades by trade_id."""
    pt = read_json_state("paper_trades.json", default={"trades": []})
    trades = pt.get("trades", []) if isinstance(pt, dict) else []
    clean_by_tid = {
        str(t.get("trade_id")): (t.get("setup_type"), t.get("archive_polluted"))
        for t in trades
        if t.get("trade_id")
    }

    db = read_json_state("edge_database.json", default={"records": [], "aggregates": {}})
    records = db.get("records", []) if isinstance(db, dict) else []
    updated = 0
    for r in records:
        tid = str(r.get("trade_id") or "")
        if tid in clean_by_tid:
            setup, polluted = clean_by_tid[tid]
            if setup and setup in KNOWN_SETUPS:
                r["setup_type"] = setup
            r["archive_polluted"] = bool(polluted)
            updated += 1

    db["records"] = records
    db["timestamp"] = datetime.now(timezone.utc).isoformat()
    # Recompute aggregates from the re-labeled records.
    try:
        edge = EdgeDatabase(log)
        db["aggregates"] = edge._compute_aggregates(records)
    except Exception as exc:  # noqa: BLE001
        log.warning("edge DB aggregate recompute failed: %s", exc)

    log.info("edge_database: %d/%d records re-labeled", updated, len(records))
    if not dry_run:
        write_json_state("edge_database.json", db)
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair polluted trade-archive labels")
    parser.add_argument("--dry-run", action="store_true", help="Print changes; write nothing")
    parser.add_argument("--days", type=int, default=30, help="MT5 deal history lookback days")
    args = parser.parse_args()

    log = setup_logger("repair_archive", "repair_archive.log")
    config = load_config()
    magic = int(config.get("execution", {}).get("magic_number", 20250625))

    if mt5 is None:
        log.error("MetaTrader5 not installed; cannot fetch deal history.")
        return 1

    conn = MT5ConnectionManager(config, log)
    if not conn.connect():
        log.error("MT5 connect failed; aborting repair.")
        return 2
    try:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        deals = mt5.history_deals_get(since, datetime.now(timezone.utc)) or []
        log.info("Fetched %d deals (last %d days, magic=%s)", len(deals), args.days, magic)

        orders_state = read_json_state("paper_orders.json", default={})
        ticket_index = _build_ticket_index(orders_state)
        log.info("paper_orders index: %d filled orders by mt5_ticket", len(ticket_index))

        recovered: dict[str, dict[str, Any]] = {}
        for deal in deals:
            if deal.magic != magic or deal.entry != mt5.DEAL_ENTRY_OUT:
                continue
            rec = _recover_from_deal(deal, ticket_index)
            if rec and rec.get("setup_type") in KNOWN_SETUPS:
                recovered[str(rec["trade_id"])] = rec
        log.info("Recovered clean labels for %d OUT deals", len(recovered))

        if args.dry_run:
            log.info("DRY RUN — no files will be written")
        repair_paper_trades(recovered, args.dry_run, log)
        repair_edge_database(args.dry_run, log)
        log.info("Repair %s.", "complete (dry-run only)" if args.dry_run else "complete")
        return 0
    finally:
        try:
            mt5.shutdown()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())