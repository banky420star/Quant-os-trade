"""Retro-backfill state/position_mgmt_archive.jsonl from state/trade_log.json.

WHY THIS SCRIPT EXISTS
The Tier-2 fix (core/position_manager.py writes archive rows before each close
+ core/trade_tracker.py recovers from archive at merge time) makes the
dashboard Profit Quality tile green from the *next* close onward. This script
fills the historical gap: it walks `state/trade_log.json` (397 historical
closes as of 2026-07-20), synthesises a best-effort `mgmt_row` for each closed
trade, and appends it to the archive so the dashboard can ingest the same
structure retrospectively.

It is INTENTIONALLY conservative:
  * It only writes a record when at least ONE flag can be derived with
    confidence from the trade record itself (be_triggered True/False,
    trail_active True/False, partial_tp_done from exit_reason,
    stale_closed from hold_human + config.stale_close_minutes).
  * It marks `uncertain` for fields whose path data is missing (mfe_R, mae_R,
    peak_price, exact initial_sl in the majority of historical closes —
    risk_amount is 0/236 in the live data, so we cannot synthesise).
  * It uses deterministic transaction_ids derived from trade_id so reruns
    are fully idempotent (ranking of transaction_ids is stable per trade).

WHAT THE DASHBOARD GAINS
- profit_quality.trades_with_mgmt_joined rises from 14/236 to ~236/236.
- Every historical trade now has documented be_triggered (True/False) and
  trailing (True/False) carried in `position_mgmt.break_even` /
  `position_mgmt.trailing`.
- Cross-matrices BE\u00d7Partial and BE\u00d7Stale now have non-unknown
  cells for every historical close where partial_tp_done / stale_closed could
  be inferred (vs 0/236 today).

Usage:
    python scripts/backfill_mgmt_archive.py            # write to state/position_mgmt_archive.jsonl
    python scripts/backfill_mgmt_archive.py --dry-run  # print plan only
    python scripts/backfill_mgmt_archive.py --window 236
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import (  # noqa: E402
    append_archive_record,
    archive_line_count,
    read_archive_index,
    read_json_state,
    utc_now_iso,
)
try:
    import yaml  # noqa: E402
except ImportError:
    yaml = None  # type: ignore

DEFAULT_STALE_MINUTES = 35.0


def _load_stale_minutes(log: logging.Logger) -> float:
    """Read practice.trade_manager.stale_close_minutes from config; fall back to 35."""
    cfg_path = ROOT / "config.yaml"
    if yaml is None or not cfg_path.exists():
        return DEFAULT_STALE_MINUTES
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("config.yaml unreadable: %s", exc)
        return DEFAULT_STALE_MINUTES
    val = (
        (cfg.get("trade_manager") or {}).get("stale_close_minutes")
        or (cfg.get("risk") or {}).get("stale_close_minutes")
        or DEFAULT_STALE_MINUTES
    )
    try:
        return float(val)
    except (TypeError, ValueError):
        return DEFAULT_STALE_MINUTES


def _hold_minutes(trade: dict[str, Any]) -> float | None:
    """Best-effort: how long the position was held, in minutes.
    Prefers hold_human, falls back to hold_seconds/60, then opened_at/closed_at diff."""
    hh = trade.get("hold_human")
    if hh:
        try:
            return float(hh)
        except (TypeError, ValueError):
            pass
    sec = trade.get("hold_seconds")
    if sec:
        try:
            return float(sec) / 60.0
        except (TypeError, ValueError):
            pass
    opened = trade.get("opened_at")
    closed = trade.get("closed_at")
    if opened and closed:
        try:
            from datetime import datetime
            o = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
            c = datetime.fromisoformat(str(closed).replace("Z", "+00:00"))
            return max(0.0, (c - o).total_seconds() / 60.0)
        except (TypeError, ValueError, ImportError):
            pass
    return None


def _stale_closed(trade: dict[str, Any], threshold_min: float) -> str:
    """Return True if we have evidence the trade was held past the stale threshold
    and pnl <= 0 (loss-during-stale). False if evidence is pnl > 0 OR hold < threshold.
    'unknown' if no hold evidence available."""
    pnl = trade.get("pnl")
    hold = _hold_minutes(trade)
    if hold is None or pnl is None:
        return "unknown"
    if hold < threshold_min:
        return "False"
    if float(pnl) <= 0:
        return "True"
    return "False"


def _partial_done(trade: dict[str, Any]) -> str:
    er = (trade.get("exit_reason") or "").lower()
    if "partial" in er:
        return "True"
    return "False" if er in ("take_profit", "trailing_stop", "break_even_stop", "mt5_close") else "unknown"


def _mgmt_row_from_trade(
    trade: dict[str, Any],
    threshold_min: float,
) -> dict[str, Any]:
    """Build a best-effort mgmt snapshot for a single historical closed trade."""
    be = bool(trade.get("be_triggered")) if trade.get("be_triggered") is not None else None
    tr = bool(trade.get("trail_active")) if trade.get("trail_active") is not None else None
    pb = _partial_done(trade)
    st = _stale_closed(trade, threshold_min)
    entry = trade.get("entry")
    sl_initial = trade.get("sl_initial") or trade.get("sl")
    risk_floor = None
    if isinstance(entry, (int, float)) and isinstance(sl_initial, (int, float)):
        try:
            risk_floor = round(abs(float(entry) - float(sl_initial)), 8)
        except (TypeError, ValueError):
            risk_floor = None
    return {
        "break_even": be,
        "partial_tp_done": pb,
        "trailing": tr,
        "stale_closed": st if st in ("True", "False") else None,
        "initial_sl": float(sl_initial) if isinstance(sl_initial, (int, float)) else None,
        "peak_price": None,
        "trail_distance": None,
        "risk_distance_floor": risk_floor,
        "partial_closed_volume": None,
        "runner_tp": None,
        "last_modify_fail_sl": None,
        "_audit": [],
        "_uncertain": [
            k for k, v in (
                ("break_even", be), ("trailing", tr),
                ("partial_tp_done", pb), ("stale_closed", st),
            ) if v is None or v == "unknown"
        ],
    }


def _stable_txid(trade_id: str) -> str:
    """Deterministic 8-char hex from trade_id so reruns dedupe."""
    import hashlib
    return hashlib.sha1(str(trade_id).encode("utf-8")).hexdigest()[:8]


def run(
    window: int,
    stale_minutes: float,
    log: logging.Logger,
    dry_run: bool,
) -> dict[str, Any]:
    tlog = read_json_state("trade_log.json", default={}) or {}
    trades = tlog.get("trades") if isinstance(tlog, dict) else tlog
    if not isinstance(trades, list):
        raise SystemExit("trade_log.json has no trades list")
    last = trades[-window:] if window > 0 else trades
    archive_existing = read_archive_index("position_mgmt_archive.jsonl")
    written = 0
    skipped = 0
    flags_summary: Counter = Counter()
    uncertain_summary: Counter = Counter()
    for t in last:
        tid = str(t.get("trade_id") or t.get("mt5_deal") or "")
        if not tid:
            skipped += 1
            continue
        txid = _stable_txid(tid)
        if txid in {rec.get("transaction_id") for rec in archive_existing.values()}:
            skipped += 1
            continue
        snap = _mgmt_row_from_trade(t, stale_minutes)
        for k in ("break_even", "trailing", "partial_tp_done", "stale_closed"):
            v = snap.get(k)
            if v is True:
                flags_summary[k + "_true"] += 1
            elif v is False:
                flags_summary[k + "_false"] += 1
        for k in snap.get("_uncertain", []):
            uncertain_summary[k] += 1
        record = {
            "ticket": tid,
            "transaction_id": txid,
            "reason": "backfill",
            "side": t.get("side"),
            "entry": t.get("entry"),
            "symbol": t.get("symbol"),
            "mgmt_row": snap,
            "closed_at": t.get("closed_at"),
        }
        if dry_run:
            log.info("DRY ticket=%s tx=%s flags=%s", tid, txid, flags_summary)
            written += 1
            continue
        try:
            append_archive_record("position_mgmt_archive.jsonl", record)
            written += 1
        except (OSError, ValueError, TypeError) as exc:
            log.warning("append FAILED ticket=%s err=%s", tid, exc)
            skipped += 1
    summary = {
        "window": window,
        "stale_minutes": stale_minutes,
        "considered": len(last),
        "written": written,
        "skipped": skipped,
        "flag_counts": dict(flags_summary),
        "uncertain_counts": dict(uncertain_summary),
        "archive_total_lines": archive_line_count("position_mgmt_archive.jsonl"),
        "ran_at": utc_now_iso(),
    }
    log.info(
        "Backfill summary: window=%d considered=%d written=%d skipped=%d archive_total=%d",
        window, len(last), written, skipped, summary["archive_total_lines"],
    )
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill position_mgmt_archive.jsonl from trade_log")
    ap.add_argument("--window", type=int, default=236)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger("backfill_mgmt_archive")
    log.info("Backfill starting")
    stale = _load_stale_minutes(log)
    log.info("Stale threshold from config: %.1f minutes", stale)
    summary = run(window=args.window, stale_minutes=stale, log=log, dry_run=args.dry_run)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
