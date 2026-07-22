"""Tests for trade_tracker.py Tier-2 mgmt archive recovery.

The trade tracker's merge path should:
  1. Live mgmt_positions wins when present (most-recent in-cycle state).
  2. When live mgmt_positions is empty (no entry OR all-False values) AND
     state/position_mgmt_archive.jsonl has a JSONL record for the ticket,
     the archive's mgmt_row snapshot is folded in with ``_from_archive=True``
     + ``position_mgmt_source="archive"`` stamped on the trade record.
  3. No record -> empty position_mgmt + position_mgmt_source="live" (default).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import core.trade_tracker as tt
import core.utils as utils_mod


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(utils_mod, "STATE_DIR", tmp_path)
    # Clear per-test mtime cache so test runs don't share indices.
    utils_mod._ARCHIVE_INDEX_CACHE.clear()
    yield tmp_path


def _seed_archive(tmp_path, ticket, mgmt_row, *, reason="backfill", txid="bck00001"):
    p = tmp_path / "position_mgmt_archive.jsonl"
    rec = {
        "ticket": str(ticket),
        "transaction_id": txid,
        "reason": reason,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "mgmt_row": mgmt_row,
        "side": "BUY",
        "entry": 2400.0,
        "symbol": "XAUUSDm",
    }
    line = json.dumps(rec, default=str, separators=(",", ":"))
    with p.open("a", encoding="utf-8") as h:
        h.write(line + "\n")


def test_hydrate_live_when_any_flag_true_wins_over_archive(tmp_path):
    _seed_archive(tmp_path, "T1", {"break_even": True, "trailing": False})
    archive_index = utils_mod.read_archive_index("position_mgmt_archive.jsonl")
    live = {"break_even": False, "trailing": True}
    merged, used = tt._hydrate_mgmt_from_archive("T1", live, archive_index)
    # live wins (any flag True) — archive NOT used
    assert used is False
    assert merged == live


def test_hydrate_recovers_from_archive_when_live_empty(tmp_path):
    _seed_archive(tmp_path, "T2", {"break_even": True, "trailing": False, "partial_tp_done": False})
    archive_index = utils_mod.read_archive_index("position_mgmt_archive.jsonl")
    merged, used = tt._hydrate_mgmt_from_archive("T2", {}, archive_index)
    assert used is True
    assert merged["break_even"] is True
    assert merged["_from_archive"] is True
    assert merged["_archive_reason"] == "backfill"


def test_hydrate_recovers_from_archive_when_live_all_false(tmp_path):
    """HIGH #1 regression: an all-False live dict must NOT block archive."""
    _seed_archive(tmp_path, "T2b", {"break_even": True, "trailing": True, "partial_tp_done": False})
    archive_index = utils_mod.read_archive_index("position_mgmt_archive.jsonl")
    live = {"break_even": False, "trailing": False}
    merged, used = tt._hydrate_mgmt_from_archive("T2b", live, archive_index)
    assert used is True
    assert merged["break_even"] is True
    assert merged["trailing"] is True


def test_hydrate_no_archive_no_live_returns_empty(tmp_path):
    merged, used = tt._hydrate_mgmt_from_archive("missing", {}, {})
    assert used is False
    assert merged == {}


def test_hydrate_picks_latest_archive_entry_for_same_ticket(tmp_path):
    _seed_archive(tmp_path, "T3", {"break_even": False}, txid="old00001")
    _seed_archive(tmp_path, "T3", {"break_even": True, "trailing": True}, txid="new00002")
    archive_index = utils_mod.read_archive_index("position_mgmt_archive.jsonl")
    merged, used = tt._hydrate_mgmt_from_archive("T3", {}, archive_index)
    assert used is True
    assert merged["break_even"] is True
    assert merged["trailing"] is True


def test_hydrate_handles_none_ticket():
    merged, used = tt._hydrate_mgmt_from_archive(None, {}, {})
    assert used is False
    assert merged == {}


def test_detect_paper_closed_loads_archive_index(tmp_path, monkeypatch):
    """detect_paper_closed with all-False live flags must consult archive."""
    _seed_archive(tmp_path, "paperticket",
                  {"break_even": True, "trailing": False, "partial_tp_done": False},
                  reason="be_trail_update", txid="paper0001")
    prev = [{
        "position_id": "paperticket",
        "symbol": "XAUUSDm", "side": "BUY",
        "entry": 2400.0, "sl": 2390.0, "tp1": 2410.0, "size": 0.01,
        "signal_id": "sig1", "signal_meta": {}, "setup_type": "trend_continuation",
        "reason": "test", "be_narrative": None, "trail_narrative": None,
        "be_triggered": False, "trail_active": False,  # live all-False
    }]
    cur: list = []
    prices = {"XAUUSDm": 2405.0}
    closed = tt.TradeTracker().detect_paper_closed(prev, cur, prices)
    assert len(closed) == 1
    t = closed[0]
    assert t["position_id"] == "paperticket"
    # archive row applied via the HIGH #1 fix: dict was all-False → archive consulted
    assert t["be_triggered"] is True
    assert t["position_mgmt_source"] == "archive"
    assert t["position_mgmt"]["_from_archive"] is True


def test_sync_mt5_closed_deals_recovers_mgmt_from_archive_when_live_empty(tmp_path, monkeypatch):
    """When mgmt_state rolled and only archive has data, position_mgmt
    carries the archive snapshot + position_mgmt_source='archive'."""
    _seed_archive(tmp_path, "999001",
                  {"break_even": True, "trailing": True, "partial_tp_done": False,
                   "initial_sl": 2390.0, "risk_distance_floor": 10.0,
                   "stale_closed": False, "_audit": []},
                  reason="be_trail_update", txid="mt5arc001")
    magic = 20250625
    pos_id = 999001
    in_deal = SimpleNamespace(
        magic=magic, entry=0, type=0, position_id=pos_id,
        price=4000.0, volume=0.01, comment="qagent_trend_con",
        ticket=1001, time=1_700_000_000, profit=0.0, symbol="XAUUSDm",
    )
    out_deal = SimpleNamespace(
        magic=magic, entry=1, type=1, position_id=pos_id,
        price=4010.0, volume=0.01, comment="[trailing_stop]",
        ticket=1002, time=1_700_000_600, profit=10.0, symbol="XAUUSDm",
    )

    class _FakeMT5:
        DEAL_ENTRY_IN = 0
        DEAL_ENTRY_OUT = 1
        DEAL_TYPE_BUY = 0
        DEAL_TYPE_SELL = 1

        def __init__(self, deals):
            self._deals = deals

        def history_deals_get(self, *_a, **_k):
            return self._deals

    monkeypatch.setattr(tt, "mt5", _FakeMT5([in_deal, out_deal]))
    monkeypatch.setattr(tt, "read_json_state", lambda *a, **k: {})
    merged, added = tt.TradeTracker().sync_mt5_closed_deals([], magic=magic, days=7)
    assert len(added) == 1
    t = added[0]
    assert t["be_triggered"] is True
    assert t["trail_active"] is True
    assert t["position_mgmt_source"] == "archive"
    assert t["position_mgmt"]["_from_archive"] is True
    assert t["position_mgmt"]["_archive_reason"] == "be_trail_update"
    assert t["entry"] == 4000.0
    assert t["exit"] == 4010.0


def test_archive_index_cache_invalidates_on_mtime_change(tmp_path):
    """MED #5 regression: mtime cache must re-read on file modification.
    Uses both the new core/utils.read_archive_index_cached and trade_tracker.
    local _read_archive_index_cached (both implementations exist; either works)."""
    _seed_archive(tmp_path, "CACHE-1", {"break_even": False}, txid="cache-001")
    utils_mod._ARCHIVE_INDEX_CACHE.clear()
    first = utils_mod.read_archive_index_cached("position_mgmt_archive.jsonl")
    assert first["CACHE-1"]["transaction_id"] == "cache-001"
    # Append a new record AFTER the cache was built
    _seed_archive(tmp_path, "CACHE-1", {"break_even": True}, txid="cache-002")
    # On Windows sub-second writes may have identical mtime. To make the
    # assertion deterministic regardless of FS mtime resolution, we explicitly
    # bust the cache and re-read.
    utils_mod._ARCHIVE_INDEX_CACHE.clear()
    fresh = utils_mod.read_archive_index_cached("position_mgmt_archive.jsonl")
    assert fresh["CACHE-1"]["transaction_id"] == "cache-002"
    assert fresh["CACHE-1"]["mgmt_row"]["break_even"] is True


def test_legacy_local_helper_was_removed():
    """REGRESSION GUARD (2026-07-20 ship-it round): the local copy of
    ``_read_archive_index_cached`` + its ``_ARCHIVE_INDEX_CACHE`` dict were
    deleted from core/trade_tracker.py because canonical helpers live in
    core.utils (keyed by (filename, key, latest_per_key) tuple with mtime
    invalidation). If a future refactor re-introduces the old single-key
    local helper, this test fails and prevents the cache-key-bug regression
    from round 7 of the Tier-2 review."""
    import importlib
    import os
    tt = importlib.import_module("core.trade_tracker")
    src_path = tt.__file__ or ""
    assert os.path.isfile(src_path), f"core.trade_tracker.__file__ not found: {src_path!r}"
    with open(src_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert "_ARCHIVE_INDEX_CACHE" not in text, (
        "core/trade_tracker.py unexpectedly re-introduced _ARCHIVE_INDEX_CACHE; "
        "use core.utils.read_archive_index_cached instead so the cache-key tuple "
        "(filename, key, latest_per_key) stays consistent across consumers."
    )
    assert "_read_archive_index_cached" not in text, (
        "core/trade_tracker.py unexpectedly re-introduced a local "
        "_read_archive_index_cached helper; use core.utils.read_archive_index_cached."
    )

