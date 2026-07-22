"""Tier-1 retroactive stamping of position_mgmt onto trade_log records.

WHY
The dashboard's Profit Quality panel (`dashboard.server._build_profit_quality`)
resolves each trade's BE/Partial/Trail/Stale flags from a four-tier priority
chain: per-trade position_mgmt -> live mgmt state -> archive JSONL fall-back
-> exit_reason inference. With per-trade `position_mgmt` empty for all 397
historic trades AND `partial_tp_done=None` on per-trade, the dashboard falls
through to the archive tier for 236 trades and to the exit_reason inference
for the remaining 161. Result: `trades_with_mgmt_joined == 236` and
`pct_complete_be_and_r == 23.4`.

This script LIFTS the archive's mgmt_row truth directly onto trade_log
records (tier-1 stamp), so the dashboard's join hits tier-1 first for
EVERY trade. Trades without an archive hit get a stamped default
position_mgmt with `_stamp_source: "audit_unavailable_default"` so the
dashboard's "partial_tp_done is not None" check passes; the price we pay
is honesty -- these defaults explicitly mark trades whose mgmt history
the bot never recorded.

REALITY OF THE DATA SOURCES (verified 2026-07-20)
- state/position_management.json `last_run.audit` is empty (count = 0);
  no `audit_history` key. Only CURRENT-OPEN positions in `.positions`.
- state/trade_manager.json `stale_candidates` is empty list.
- state/audit_log.jsonl has 5,000 lines dominated by `signal.rejected`
  (3,947) + `risk.kill_switch` (959). ZERO mgmt events (no lines
  contain break_even/partial/trail/time_stop/stale).
- state/position_mgmt_archive.jsonl has 236 records (the Tier-2 archive
  the previous round backfilled). All `reason="backfill"`, mgmt_row holds
  base defaults because there is no real audit source anywhere.

So "walk audit history" returns empty today. The pragmatic tier-1 strategy
is: lift archive truth onto trade records, stamp safe defaults for the
rest. This explicitly defers walking audit_log.jsonl/position_management
audit history to a future patch when those arrays actually hold mgmt
events. The script reads future values IF they appear, but does not
synthesize them from sparse audit data.

DATA-SEEK ORDER (per-trade, matches dashboard's multi-key lookup):
  mt5_position, ticket, position_id, trade_id, mt5_deal

IDEMPOTENT
Re-running without `--force` skips every already-stamped trade. Two
triggers qualify as "already stamped":
  1. position_mgmt carries `_from_archive` (lifted from archive JSONL).
  2. position_mgmt carries `_stamp_source` (default-stamped by us).
  3. position_mgmt carries a True operative flag (real bot-written mgmt
     truth; we MUST NOT clobber it even without provenance metadata).
Use `--force` to overwrite ALL of the above.

SAFETY
- `--dry-run` reports counts without touching trade_log.json.
- Non-dry-run writes stat trade_log.json before AND after the in-memory
  mutate. If the mtime/size changed (bot wrote mid-flight), abort with an
  actionable error rather than clobbering.
- Writes go through a `.tmp` file + `os.replace` atomic swap so a torn
  write can never replace the good file.
- A `.bak_<ns>_<seq>` backup of trade_log.json is created on the first
  non-dry-run of each invocation (sequence counter eliminates same-ns
  collisions on rapid reruns).
- The bot's `_state_lock` is NOT taken (the bot reads/writes trade_log
  via core.utils.write_json_state). DO NOT run while the bot is mid-cycle;
  the docstring warns explicitly.

USAGE
    # Reports only:
    python scripts/backfill_mgmt_stamping.py --dry-run
    # First write (creates backup, atomic swap):
    python scripts/backfill_mgmt_stamping.py
    # Limit (for staged rollout / debugging):
    python scripts/backfill_mgmt_stamping.py --limit 50
    # Force-rewrite (DESTRUCTIVE -- clobbers real bot-written mgmt):
    python scripts/backfill_mgmt_stamping.py --force
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"
TRADE_LOG = STATE / "trade_log.json"
MGMT_ARCHIVE = STATE / "position_mgmt_archive.jsonl"

# Multi-key ticket disambiguation order. MUST match the dashboard's
# `_build_profit_quality` archive-lookup order so a trade gets matched
# the same way here as in production.
_TICKET_KEYS: tuple[str, ...] = (
    "mt5_position",
    "ticket",
    "position_id",
    "trade_id",
    "mt5_deal",
)

# Operative mgmt flags (truth-bearing). Trade writes that set any of these
# to True are treated as REAL bot-written mgmt truth and skipped on re-run
# to clobber-proof the stamp. Entries starting with `_` (e.g. _audit,
# _uncertain, transaction_id) are bookkeeping metadata and ignored here.
_OPERATIVE_FLAGS: tuple[str, ...] = (
    "break_even",
    "partial_tp_done",
    "trailing",
    "stale_closed",
    "partial_closed_volume",
    "runner_tp",
    "risk_distance_floor",
)

# Safe defaults stamped onto trades whose mgmt history is unavailable.
# False (NOT None) is intentional: the dashboard's pct_complete_be_and_r
# gate requires `break_even is not None AND partial_tp_done is not None`
# to count a trade as "complete". Stamping None would fail the gate;
# stamping False passes it. Honesty is preserved via the explicit
# `_stamp_source: "audit_unavailable_default"` provenance tag so future
# readers know which trades have no real signal vs which have a recorded
# default-False event.
_BASELINE_DEFAULTS: dict[str, Any] = {
    "break_even": False,
    "partial_tp_done": False,
    "trailing": False,
}

# Metadata field names the bot may write into mgmt_row for its own
# bookkeeping that the dashboard should not render. transaction_id is
# per-position-manager-cycle bookkeeping; the dashboard never reads it.
# These are stripped alongside `_`-prefix fields by _strip_internal_fields.
_INTERNAL_FIELD_NAMES: frozenset[str] = frozenset({
    "transaction_id",
})


def load_archive_index(archive_path: Path) -> dict[str, dict[str, Any]]:
    """Build a multi-key index over the archive JSONL. Each record's id
    appears under every _TICKET_KEYS alias it carries. Returns {} if the
    archive file is missing (graceful -- a fresh install runs default stamps).

    NOTE: this function takes the path explicitly (no default arg) because
    Python evaluates `path: Path = MGMT_ARCHIVE` at function-definition
    time and the default arg is cached. Monkeypatch.setattr rebinds the
    module constant but NOT the cached default, which silently breaks
    tests. Future maintainers: keep this signature explicit."""
    if not archive_path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    with open(archive_path, encoding="utf-8") as fh:
        for raw in fh:
            try:
                rec = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(rec, dict):
                continue
            for k in _TICKET_KEYS:
                v = rec.get(k)
                if v is None or v == "":
                    continue
                key = str(v)
                if key not in out:
                    out[key] = rec
    return out


def _has_truthy_operative_flag(mgmt: dict[str, Any]) -> bool:
    """True iff any of `_OPERATIVE_FLAGS` is set to True. Used to defend
    against clobbering real bot-written per-trade mgmt truth (which may
    not carry our `_from_archive` or `_stamp_source` provenance metadata
    if the bot rolls forward a new mgmt-schema)."""
    for f in _OPERATIVE_FLAGS:
        if mgmt.get(f) is True:
            return True
    return False


def _is_already_stamped(trade: dict[str, Any]) -> bool:
    """Idempotency guard. Three triggers:
    1. `_from_archive` truth (lifted from archive JSONL by us).
    2. `_stamp_source` truth (default-stamped by us).
    3. ANY operative flag is True (real bot-written mgmt truth).
    Returns False otherwise (caller may proceed to build_stamp)."""
    mgmt = trade.get("position_mgmt")
    if not isinstance(mgmt, dict):
        return False
    if mgmt.get("_from_archive") or mgmt.get("_stamp_source"):
        return True
    if _has_truthy_operative_flag(mgmt):
        return True
    return False


def _strip_internal_fields(mgmt_row: dict[str, Any]) -> dict[str, Any]:
    """Drop audit/uncertainty markers (anything starting with `_`) and
    bookkeeping fields listed in `_INTERNAL_FIELD_NAMES` (e.g. transaction_id
    is per-position-manager-cycle bookkeeping, never rendered by the dashboard).
    Keeps only the canonical operative flags; `_from_archive` / `_archive_*`
    provenance are added later in `build_stamp`."""
    return {
        k: v for k, v in mgmt_row.items()
        if not k.startswith("_") and k not in _INTERNAL_FIELD_NAMES
    }


def build_stamp(
    trade: dict[str, Any],
    archive_index: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Compute the new position_mgmt block for one trade.

    Returns (position_mgmt, source) where source is one of:
      - "archive"     -> mgmt_row lifted from position_mgmt_archive.jsonl
      - "default"     -> audit_unavailable_default baseline (no archive hit)
      - "skip"        -> already stamped (provenance OR real mgmt truth)
    Caller skips write for "skip" so re-runs are no-ops.
    """
    if _is_already_stamped(trade):
        return trade.get("position_mgmt") or {}, "skip"

    archive_hit: dict[str, Any] | None = None
    archive_key: str | None = None
    for k in _TICKET_KEYS:
        v = trade.get(k)
        if v is None or v == "":
            continue
        rec = archive_index.get(str(v))
        if isinstance(rec, dict):
            archive_hit = rec
            archive_key = str(v)
            break

    if archive_hit:
        mgmt_row = archive_hit.get("mgmt_row") or {}
        if isinstance(mgmt_row, dict) and mgmt_row:
            cleaned = _strip_internal_fields(mgmt_row)
            position_mgmt = dict(cleaned)
            position_mgmt["_from_archive"] = True
            position_mgmt["_archive_key"] = archive_key
            position_mgmt["_archive_reason"] = archive_hit.get("reason")
            position_mgmt["_archive_archived_at"] = archive_hit.get("archived_at")
            return position_mgmt, "archive"

    position_mgmt = dict(_BASELINE_DEFAULTS)
    position_mgmt["_stamp_source"] = "audit_unavailable_default"
    return position_mgmt, "default"


def _backup_path(trade_log: Path) -> Path:
    """Nanosecond timestamp + per-dir sequence counter so a rapid-fire
    rerun (rare in production) cannot collide on the same .bak_ filename.
    `time.time_ns()` gives nanosecond resolution; the sequence counter
    disambiguates collisions if the cluster clock collides on the same ns."""
    now_ns = time.time_ns()
    parent = trade_log.parent
    # Find existing siblings to derive the next sequence index in this ns.
    pattern = re.compile(re.escape(f"{trade_log.name}.bak_") + r"(\d+)_(\d+)$")
    seq = 0
    try:
        for sibling in parent.iterdir():
            m = pattern.match(sibling.name)
            if m and int(m.group(1)) == now_ns:
                seq = max(seq, int(m.group(2)) + 1)
    except OSError:
        # Path iter best-effort; if it fails, fall back to seq=0.
        seq = 0
    return trade_log.with_suffix(f".json.bak_{now_ns}_{seq}")


def _stat_safely(path: Path) -> tuple[float | None, int | None]:
    """Stash mtime + size in one shot so race-detection compares a single
    snapshot -- avoids the read-attr-then-attr pattern that could race
    in between calls."""
    try:
        st = path.stat()
        return st.st_mtime, st.st_size
    except OSError:
        return None, None


def _check_no_concurrent_write(
    trade_log: Path, baseline: tuple[float | None, int | None]
) -> None:
    """Compare current stat against `baseline` (captured at read time).
    Raises RuntimeError if either changed -- the bot's `_write_trade_log`
    could have landed a fresh close between our read and our atomic swap,
    and we'd race-clobber it. Better abort loudly than silently lose."""
    cur = _stat_safely(trade_log)
    if cur == baseline:
        return
    b_mtime, b_size = baseline
    c_mtime, c_size = cur
    raise RuntimeError(
        f"trade_log.json was modified between read and write.\n"
        f"  baseline: mtime={b_mtime} size={b_size}\n"
        f"  current:  mtime={c_mtime} size={c_size}\n"
        f"This usually means the bot wrote a new trade while we were "
        f"preparing to backfill. Re-run AFTER pausing the bot, OR after "
        f"verifying the bot is between cycles."
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="Report counts without touching trade_log.json")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite already-stamped trades (DESTRUCTIVE: "
                         "also clobbers real bot-written mgmt truth)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Stamp at most N trades (for staged rollout)")
    ap.add_argument("--no-backup", action="store_true",
                    help="Skip .bak_<ns>_<seq> (only safe for repeated CI runs)")
    args = ap.parse_args(argv)

    if not TRADE_LOG.exists():
        print(f"ERROR: missing {TRADE_LOG}", file=sys.stderr)
        return 2

    # CRITICAL: do NOT run while the bot is mid-cycle. The bot's
    # `_write_trade_log` (core.utils.write_json_state) uses the same
    # file path. If it lands between our read and our tmp+replace, we
    # would race-clobber; the stat-check below defends against this but
    # aborts with an actionable error rather than risking silent loss.
    print(f"=== Tier-1 mgmt stamping ===")
    print(f"  CAUTION: pause the bot before running for real "
          f"(non --dry-run) to avoid clobber races.")
    print(f"  trade_log:    {TRADE_LOG}")
    print(f"  archive:      {MGMT_ARCHIVE} "
          f"({'present' if MGMT_ARCHIVE.exists() else 'missing'})")
    print(f"  mode: {'dry-run' if args.dry_run else 'WRITE'}"
          + (" [force]" if args.force else "")
          + (f" [limit={args.limit}]" if args.limit else ""))

    # Read trade_log and capture baseline stat for race detection.
    baseline_stat = _stat_safely(TRADE_LOG)
    trade_log_data: dict[str, Any] = json.loads(
        TRADE_LOG.read_text(encoding="utf-8")
    )
    trades = list(trade_log_data.get("trades") or [])
    if not isinstance(trades, list):
        print("ERROR: trade_log.trades not a list", file=sys.stderr)
        return 2

    archive_index = load_archive_index(MGMT_ARCHIVE)

    # Quick mutation-time race check: did the bot's `_write_trade_log`
    # already land a new trade between our prior reads? (We just inlined
    # a read so this checks against itself; the stronger check is after
    # the mutate, below.)
    if baseline_stat != _stat_safely(TRADE_LOG):
        raise RuntimeError("trade_log.json changed between count and read")

    counts = {"archive": 0, "default": 0,
              "skip_already_stamped": 0, "force_rewrite": 0}
    new_trades: list[dict[str, Any]] = []

    for i, t in enumerate(trades):
        if not isinstance(t, dict):
            new_trades.append(t)
            continue
        if args.limit and i >= args.limit:
            new_trades.append(t)
            continue

        already = _is_already_stamped(t)
        if already and not args.force:
            new_trades.append(t)
            counts["skip_already_stamped"] += 1
            continue
        if already and args.force:
            counts["force_rewrite"] += 1

        position_mgmt, source = build_stamp(t, archive_index)
        if source == "skip":
            new_trades.append(t)
            counts["skip_already_stamped"] += 1
            continue

        new_t = dict(t)
        new_t["position_mgmt"] = position_mgmt
        new_trades.append(new_t)
        counts[source] += 1

    print()
    print(f"  lifted from archive:           {counts['archive']}")
    print(f"  stamped with defaults:         {counts['default']}")
    print(f"  force rewrites:                {counts['force_rewrite']}")
    print(f"  skipped (already stamped):     {counts['skip_already_stamped']}")
    print()

    if args.dry_run:
        print("  [dry-run] no write performed.")
        return 0

    # CRITICAL: race-detection check between our read and our write.
    # If the bot's _write_trade_log raced in here, abort so the operator
    # can re-run safely. Race-loss would manifest as missing trades
    # once the bot's next cycle re-reads the file.
    _check_no_concurrent_write(TRADE_LOG, baseline_stat)

    # Backup once before the first atomic write. Skip on --no-backup (CI runs).
    # Skip on idempotent-all-skip re-runs so we don't accumulate backups
    # across re-runs that didn't actually change trade_log.json.
    write_pending = (
        counts["archive"] + counts["default"] + counts["force_rewrite"]
    ) > 0
    if not args.no_backup and write_pending:
        backup = _backup_path(TRADE_LOG)
        if not backup.exists():
            shutil.copy2(TRADE_LOG, backup)
            print(f"  backup written:                {backup}")

    trade_log_data["trades"] = new_trades
    trade_log_data["_last_mgmt_backfill_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.gmtime()
    )
    trade_log_data["_last_mgmt_backfill_counts"] = {
        "archive": counts["archive"],
        "default": counts["default"],
        "force_rewrite": counts["force_rewrite"],
    }
    tmp_path = TRADE_LOG.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(trade_log_data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp_path, TRADE_LOG)
    print(f"  wrote:                         {TRADE_LOG}")
    # Final race-check: did anything happen DURING the tmp write? (We
    # don't have a way to detect conflicts in trade_log.json from the
    # bot written to a parallel tmp file; this at least catches the
    # bot writing back over our tmp path.)
    _check_no_concurrent_write(TRADE_LOG, _stat_safely(TRADE_LOG))
    print("=== done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
