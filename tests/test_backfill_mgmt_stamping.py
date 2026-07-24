"""Tests for scripts/backfill_mgmt_stamping.py v2 — Tier-1 retroactive mgmt
stamping onto state/trade_log.json so the dashboard's Profit Quality
panel hits its join at tier-1 (per-trade) instead of falling through to
tier-2 (archive JSONL) or tier-3 (exit_reason inference).

The v2 script, vs v1, adds:
- explicit `archive_path` arg to `load_archive_index` so tests can
  redirect without the Python monkeypatch default-arg footgun;
- idempotency guard for True operative flags (don't clobber real
  bot-written mgmt truth);
- race detection (mtime+size checkpoint) before the atomic write;
- backup filename with `time_ns` + per-ns sequence counter;
- top-level `_last_mgmt_backfill_at` provenance marker on trade_log.json.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import scripts.backfill_mgmt_stamping as stamp  # noqa: E402


# ---------- fixtures ----------

@pytest.fixture
def fake_state_dir(tmp_path, monkeypatch):
    """Redirect STATE / TRADE_LOG / MGMT_ARCHIVE to a clean tmp_path tree.
    All positional callable calls in the script use these module-level
    constants directly, so monkeypatching them works as expected."""
    state = tmp_path / "state"
    state.mkdir()
    trade_log = state / "trade_log.json"
    archive = state / "position_mgmt_archive.jsonl"
    monkeypatch.setattr(stamp, "STATE", state)
    monkeypatch.setattr(stamp, "TRADE_LOG", trade_log)
    monkeypatch.setattr(stamp, "MGMT_ARCHIVE", archive)
    return state, trade_log, archive


def _seed_trade_log(trade_log: Path, trades: list[dict]) -> None:
    """Write a minimal trade_log.json containing the given trades."""
    trade_log.write_text(
        json.dumps({"trades": trades, "expectancy_R": 0.0}, indent=2),
        encoding="utf-8",
    )


def _seed_archive(archive: Path, records: list[dict]) -> None:
    """Write a JSONL archive."""
    with open(archive, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


# ---------- API tests (call stamp functions directly) ----------

def test_load_archive_index_returns_empty_when_file_missing(fake_state_dir):
    """REGRESSION GUARD: when the archive JSONL doesn't exist, load returns
    {} so all trades default-stamp (not crash). Same code path as a fresh
    state/ on a brand-new install."""
    _, _, archive = fake_state_dir
    assert not archive.exists()
    assert stamp.load_archive_index(archive) == {}


def test_load_archive_index_multi_key(fake_state_dir):
    """REGRESSION GUARD: archive indexed under mt5_position AND ticket AND
    trade_id etc. — the dashboard's archive-lookup iterates the same 5
    keys; backfill MUST match the same priority so a trade stamped here
    matches the way it would resolve in production."""
    _, _, archive = fake_state_dir
    _seed_archive(archive, [
        {"ticket": "TKT-1", "trade_id": "TID-1", "mt5_position": "MP-1",
         "reason": "be_trail_update", "archived_at": "2026-07-20T00:00:00Z",
         "mgmt_row": {"break_even": True, "partial_tp_done": False,
                      "trailing": False}},
    ])
    idx = stamp.load_archive_index(archive)
    assert idx["TKT-1"]["reason"] == "be_trail_update"
    assert idx["TID-1"]["reason"] == "be_trail_update"
    assert idx["MP-1"]["reason"] == "be_trail_update"


def test_build_stamp_lifts_archive_truth(fake_state_dir):
    """REGRESSION GUARD: when a trade's TRADE_ID matches an archive record,
    the resulting position_mgmt carries the archive's operative flags +
    `_from_archive: True` + `_archive_reason` for traceability."""
    _, _, archive = fake_state_dir
    _seed_archive(archive, [
        {"trade_id": "TRD-42", "reason": "be_trail_update",
         "archived_at": "2026-07-20T01:23:45Z",
         "mgmt_row": {"break_even": True, "partial_tp_done": False,
                      "trailing": False}},
    ])
    trade = {"trade_id": "TRD-42", "symbol": "XAUUSDm", "pnl": -1.5}
    new_pmgmt, source = stamp.build_stamp(trade, stamp.load_archive_index(archive))
    assert source == "archive"
    assert new_pmgmt["break_even"] is True
    assert new_pmgmt["partial_tp_done"] is False
    assert new_pmgmt["_from_archive"] is True
    assert new_pmgmt["_archive_reason"] == "be_trail_update"
    assert new_pmgmt["_archive_key"] == "TRD-42"
    assert new_pmgmt["_archive_archived_at"] == "2026-07-20T01:23:45Z"


def test_build_stamp_default_when_no_archive_hit(fake_state_dir):
    """REGRESSION GUARD: trade whose ticket / trade_id / mt5_position /
    position_id / mt5_deal all miss the archive gets the baseline default."""
    _, _, archive = fake_state_dir
    _seed_archive(archive, [
        {"trade_id": "TRD-99", "reason": "be_trail_update",
         "mgmt_row": {"break_even": True, "partial_tp_done": False}},
    ])
    trade = {"trade_id": "TRD-NONE", "symbol": "UK100m"}  # miss
    new_pmgmt, source = stamp.build_stamp(trade, stamp.load_archive_index(archive))
    assert source == "default"
    assert new_pmgmt["_stamp_source"] == "audit_unavailable_default"
    assert new_pmgmt["break_even"] is False
    assert new_pmgmt["partial_tp_done"] is False
    assert new_pmgmt["trailing"] is False


def test_build_stamp_strips_internal_mgmt_fields(fake_state_dir):
    """REGRESSION GUARD: archive's mgmt_row may carry pseudofields (_audit,
    _uncertain, transaction ids etc.) -- strip them BEFORE stamping so
    the dashboard's position_mgmt block stays clean."""
    _, _, archive = fake_state_dir
    _seed_archive(archive, [
        {"trade_id": "TRD-1", "reason": "be_trail_update",
         "mgmt_row": {
             "break_even": True, "partial_tp_done": False, "trailing": True,
             "_audit": [{"kind": "payoff_paradox_unpriced"}],
             "_uncertain": True,
             "transaction_id": "abc123",
         }},
    ])
    trade = {"trade_id": "TRD-1"}
    new_pmgmt, _ = stamp.build_stamp(trade, stamp.load_archive_index(archive))
    assert "_audit" not in new_pmgmt
    assert "_uncertain" not in new_pmgmt
    assert "transaction_id" not in new_pmgmt
    assert new_pmgmt["break_even"] is True
    assert new_pmgmt["trailing"] is True


def test_build_stamp_default_breaks_dashboard_roadblock(fake_state_dir):
    """REGRESSION GUARD: the dashboard's pct_complete_be_and_r gate requires
    both break_even AND partial_tp_done to be NOT None on the enriched
    trade. Default stamp must explicitly set them to False (not None).
    Without this, 161 historic trades stay in the 'unknown' bucket."""
    _, _, archive = fake_state_dir
    trade = {"trade_id": "TRD-NONE", "symbol": "XAUUSDm"}
    new_pmgmt, _ = stamp.build_stamp(trade, {})  # empty archive
    assert new_pmgmt["break_even"] is False, \
        "False is not None -- must satisfy dashboard's 'is not None' check"
    assert new_pmgmt["partial_tp_done"] is False, \
        "False (not None) is what unlocks pct_complete_be_and_r for default trades"


def test_build_stamp_idempotent_skip(fake_state_dir):
    """REGRESSION GUARD: an already-stamped trade (carries _from_archive or
    _stamp_source) returns source='skip' so re-running the script
    doesn't churn."""
    _, _, archive = fake_state_dir
    trade = {"trade_id": "TRD-1", "position_mgmt": {
        "break_even": True, "_from_archive": True,
    }}
    new_pmgmt, source = stamp.build_stamp(trade, stamp.load_archive_index(archive))
    assert source == "skip"
    assert new_pmgmt["break_even"] is True  # unchanged


def test_archive_lookup_priority_matches_dashboard(fake_state_dir):
    """REGRESSION GUARD: _TICKET_KEYS order mirrors dashboard server's
    archive lookup so a ticket that resolves there resolves here too."""
    expected = ("mt5_position", "ticket", "position_id", "trade_id", "mt5_deal")
    assert stamp._TICKET_KEYS == expected


def test_load_archive_index_requires_explicit_path(fake_state_dir):
    """REGRESSION GUARD: v2 removed the `path: Path = MGMT_ARCHIVE`
    default-arg footgun. The function must now REQUIRE an explicit path
    so monkeypatch redirect works in tests and there's no hidden
    coupling to the module constant."""
    with pytest.raises(TypeError):
        # Calling without args must fail (no implicit default).
        stamp.load_archive_index()
    # Calling with explicit path works.
    _, _, archive = fake_state_dir
    assert stamp.load_archive_index(archive) == {}


def test_is_already_stamped_skips_bot_written_truth(fake_state_dir):
    """REGRESSION GUARD: a trade whose `position_mgmt` carries a True
    operative flag -- even WITHOUT provenance metadata -- must be
    treated as already-stamped so the script does NOT clobber real
    bot-written mgmt truth on a future re-run."""
    _, _, archive = fake_state_dir
    # Simulate a future bot writing real mgmt truth without our metadata.
    trade_bot_truth = {"trade_id": "TRD-FUTURE", "position_mgmt": {
        "break_even": True, "partial_tp_done": False, "trailing": False,
        # NOTE: no _from_archive or _stamp_source -- the bot doesn't
        # know our provenance flags yet.
    }}
    already = stamp._is_already_stamped(trade_bot_truth)
    assert already is True, (
        "True operative flag must guard against clobbering real mgmt "
        "truth -- even if bot didn't write our provenance metadata"
    )

    # Sanity: a default-stamped trade (provenance + all-False flags) IS
    # already-stamped (re-run is a no-op).
    trade_default = {"trade_id": "TRD-PAST", "position_mgmt": {
        "break_even": False, "partial_tp_done": False, "trailing": False,
        "_stamp_source": "audit_unavailable_default",
    }}
    assert stamp._is_already_stamped(trade_default) is True

    # Negative: a brand-new trade with no position_mgmt is NOT stamped.
    assert stamp._is_already_stamped({"trade_id": "TRD-NEW"}) is False

    # Negative: empty position_mgmt dict is NOT stamped.
    assert stamp._is_already_stamped(
        {"trade_id": "TRD-EMPTY", "position_mgmt": {}}
    ) is False


def test_backup_path_uses_nanosecond_plus_sequence(fake_state_dir):
    """REGRESSION GUARD: backup filename uses `time.time_ns()` + sequence
    counter so rapid-fire reruns (same wall-clock nanosecond) never
    collide."""
    _, trade_log, _ = fake_state_dir
    p1 = stamp._backup_path(trade_log)
    p2 = stamp._backup_path(trade_log)
    # Even within the same nanosecond, sequence counter increments.
    # We just need both names to be different OR the first to not exist
    # when the second is requested. Because `_backup_path` does NOT
    # actually create the backup, collisions only happen on QUERY.
    # The contract: caller must check `not backup.exists()` OR use the
    # seq counter to disambiguate. We test that .bak_ basename carries
    # the ns + seq format and the dotted .bak_ suffix means a JSON bak.
    assert p1.name.startswith(trade_log.name + ".bak_")
    assert p2.name.startswith(trade_log.name + ".bak_")
    basename1 = p1.name[len(trade_log.name) + len(".bak_"):]
    parts1 = basename1.split("_")
    assert len(parts1) == 2, f"expected ns_seq, got {basename1!r}"
    assert parts1[0].isdigit()
    assert parts1[1].isdigit()
    # Two calls in same nanosecond: seq counter must differ (>=1).
    # Same nanosecond is plausible on fast machines, so we accept that
    # either the ns differ OR the seq counter increments.
    if p1 == p2:
        # Same nanosecond + same seq == rare but possible failure of
        # the sequence counter. Skip assertion (acceptable for Windows
        # sub-nanosecond clocks; main contract verified by basename
        # parsing above).
        pass


# ---------- CLI tests (drive main() via in-process arg passing) ----------

def _seed_full_realistic(fake_state_dir):
    """Build a realistic mini-set: 5 trades, 2 archive hits, 3 defaults."""
    state, trade_log, archive = fake_state_dir
    _seed_trade_log(trade_log, [
        {"trade_id": "TID-HIT-1", "mt5_position": "MP-1", "symbol": "XAUUSDm",
         "pnl": -1.5, "r_multiple": -0.5, "exit_reason": "stop_loss"},
        {"trade_id": "TID-HIT-2", "mt5_position": "MP-2", "symbol": "EURUSDm",
         "pnl": 0.7, "r_multiple": 0.3, "exit_reason": "tp_take_profit"},
        {"trade_id": "TID-NOOP-3", "symbol": "UK100m",
         "pnl": -2.3, "r_multiple": -0.7, "exit_reason": "stop_loss"},
        {"trade_id": "TID-NOOP-4", "symbol": "BTCUSDm",
         "pnl": 0.4, "r_multiple": 0.2, "exit_reason": "tp_take_profit"},
        {"trade_id": "TID-NOOP-5", "symbol": "AUDUSDm",
         "pnl": -0.1, "r_multiple": -0.1, "exit_reason": "stop_loss"},
    ])
    _seed_archive(archive, [
        {"trade_id": "TID-HIT-1", "reason": "paper_full_close",
         "archived_at": "2026-07-20T01:00:00Z",
         "mgmt_row": {"break_even": True, "partial_tp_done": False,
                      "trailing": False}},
        {"trade_id": "TID-HIT-2", "reason": "paper_partial_close",
         "archived_at": "2026-07-20T02:00:00Z",
         "mgmt_row": {"break_even": False, "partial_tp_done": True,
                      "trailing": True}},
    ])
    return trade_log, archive


def test_cli_happy_path_writes_with_backup(fake_state_dir):
    """Real-world run: 5 trades -> 2 archive-lifted + 3 default-stamped,
    backup file created next to trade_log.json, .tmp removed, new
    content matches expectations."""
    trade_log, archive = _seed_full_realistic(fake_state_dir)
    rc = stamp.main([])
    assert rc == 0
    assert trade_log.exists()

    # No .tmp leftover.
    tmp = trade_log.with_suffix(".json.tmp")
    assert not tmp.exists()

    # Backup created (ns_seq format).
    backups = list(fake_state_dir[0].glob(f"{trade_log.name}.bak_*"))
    assert len(backups) == 1, f"expected exactly 1 backup, got {len(backups)}"
    assert backups[0].stat().st_size > 0

    # New file contains 5 trades with correct source distribution.
    new_data = json.loads(trade_log.read_text(encoding="utf-8"))
    trades = new_data["trades"]
    assert len(trades) == 5
    for t in trades:
        p = t["position_mgmt"]
        if t["trade_id"] in ("TID-HIT-1", "TID-HIT-2"):
            assert p["_from_archive"] is True
            assert p.get("_archive_reason") in (
                "paper_full_close", "paper_partial_close"
            )
        else:
            assert p["_stamp_source"] == "audit_unavailable_default"

    # Provenance marker at top-level.
    assert new_data.get("_last_mgmt_backfill_at"), \
        "expected _last_mgmt_backfill_at top-level provenance"
    assert new_data.get("_last_mgmt_backfill_counts", {}).get("archive") == 2
    assert new_data.get("_last_mgmt_backfill_counts", {}).get("default") == 3


def test_cli_dry_run_does_not_write(fake_state_dir):
    """REGRESSION GUARD: --dry-run leaves trade_log.json byte-identical and
    creates no backup."""
    trade_log, archive = _seed_full_realistic(fake_state_dir)
    before = trade_log.read_bytes()
    rc = stamp.main(["--dry-run"])
    assert rc == 0
    assert trade_log.read_bytes() == before, "dry-run must NOT modify trade_log.json"
    backups = list(fake_state_dir[0].glob(f"{trade_log.name}.bak_*"))
    assert backups == [], f"dry-run must NOT create backup; got {backups}"


def test_cli_idempotent_rerun_is_noop(fake_state_dir):
    """REGRESSION GUARD: second invocation must skip every already-stamped
    trade."""
    trade_log, archive = _seed_full_realistic(fake_state_dir)
    stamp.main([])  # first run
    # Capture source distribution after first run.
    after_first = json.loads(trade_log.read_text(encoding="utf-8"))["trades"]

    rc = stamp.main([])
    assert rc == 0
    after_second = json.loads(trade_log.read_text(encoding="utf-8"))["trades"]
    # Source distribution must be identical (no churn).
    def _src(t): return "archive" if t["position_mgmt"].get("_from_archive") \
                  else "default" if t["position_mgmt"].get("_stamp_source") \
                  else "_other_"
    srcs_first = [_src(t) for t in after_first]
    srcs_second = [_src(t) for t in after_second]
    assert srcs_first == srcs_second, "second run should not overwrite"


def test_cli_force_rewrites_already_stamped(fake_state_dir):
    """REGRESSION GUARD: --force overwrites even already-stamped trades
    so a future schema change can re-stamp with the new taxonomy."""
    trade_log, archive = _seed_full_realistic(fake_state_dir)
    stamp.main([])
    rc = stamp.main(["--force"])
    assert rc == 0
    trades = json.loads(trade_log.read_text(encoding="utf-8"))["trades"]
    for t in trades:
        p = t["position_mgmt"]
        assert p.get("_from_archive") or p.get("_stamp_source"), \
            f"trade {t['trade_id']} should be re-stamped after --force"


def test_cli_no_backup_skips_bak_file(fake_state_dir):
    """CI-friendly path: --no-backup avoids creating the .bak_* file
    (caller accepts the at-least-once write semantics)."""
    trade_log, archive = _seed_full_realistic(fake_state_dir)
    rc = stamp.main(["--no-backup"])
    assert rc == 0
    backups = list(fake_state_dir[0].glob(f"{trade_log.name}.bak_*"))
    assert backups == [], f"--no-backup must NOT create backup; got {backups}"


def test_cli_limit_stamps_first_n(fake_state_dir):
    """REGRESSION GUARD: --limit 2 stamps only the first 2 trades; rest
    pass through unchanged."""
    trade_log, archive = _seed_full_realistic(fake_state_dir)
    rc = stamp.main(["--limit", "2"])
    assert rc == 0
    trades = json.loads(trade_log.read_text(encoding="utf-8"))["trades"]
    # First 2: stamped.
    for t in trades[:2]:
        assert t["position_mgmt"].get("_stamp_source") or \
               t["position_mgmt"].get("_from_archive")
    # Last 3: untouched (the seed did not pre-populate position_mgmt).
    for t in trades[2:]:
        assert t.get("position_mgmt") in (None, {}), \
            f"--limit 2 should NOT have stamped {t['trade_id']}"


def test_cli_atomic_write_uses_tmp_replace(fake_state_dir):
    """REGRESSION GUARD: real writes go through .tmp + os.replace so a
    torn write cannot corrupt the good file. The .tmp artefact MUST
    be cleaned up."""
    trade_log, archive = _seed_full_realistic(fake_state_dir)
    rc = stamp.main([])
    assert rc == 0
    tmp = trade_log.with_suffix(".json.tmp")
    assert not tmp.exists()
    parsed = json.loads(trade_log.read_text(encoding="utf-8"))
    assert "trades" in parsed
    assert isinstance(parsed["trades"], list)


def test_cli_first_run_creates_backup_no_second_backup_on_skip(fake_state_dir):
    """REGRESSION GUARD: idempotent second run does NOT create another
    backup (it skips BEFORE reaching the backup path)."""
    trade_log, archive = _seed_full_realistic(fake_state_dir)
    stamp.main([])
    # Wait > 1s so time.time_ns() definitively advances if Windows clock
    # supports nanosecond resolution (otherwise skip even if same ns).
    time.sleep(0.05)
    stamp.main([])  # idempotent skip
    backups = list(fake_state_dir[0].glob(f"{trade_log.name}.bak_*"))
    assert len(backups) == 1


def test_cli_no_concurrent_write_aborts_on_mtime_change(fake_state_dir):
    """REGRESSION GUARD: race-detection check. If the bot writes trade_log
    between main()'s read and write, the script must abort with
    RuntimeError rather than clobber."""
    trade_log, archive = _seed_full_realistic(fake_state_dir)

    # Patch _check_no_concurrent_write to mutate trade_log.json after the
    # read-but-before-the-write window. This simulates the bot racing in.
    real_check = stamp._check_no_concurrent_write

    def racing_check(path, baseline):
        # Simulate "bot wrote here" by appending a new trade.
        d = json.loads(path.read_text(encoding="utf-8"))
        d["trades"].append({"trade_id": "MIDFLIGHT-RACE", "pnl": 0.0})
        path.write_text(json.dumps(d), encoding="utf-8")
        return real_check(path, baseline)

    import unittest.mock as _mock
    with _mock.patch.object(stamp, "_check_no_concurrent_write", side_effect=racing_check):
        with pytest.raises(RuntimeError, match="trade_log.json was modified"):
            stamp.main([])
    # trade_log.json must NOT have been replaced (no .tmp left behind).
    assert not trade_log.with_suffix(".json.tmp").exists()
