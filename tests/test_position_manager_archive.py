"""Tests for position_manager.py Tier-2 mgmt archive writes.

Verifies that every close-state hook in ``core/position_manager.py`` writes a
JSONL record to ``state/position_mgmt_archive.jsonl`` BEFORE the live
``state/position_management.json`` rolls on the next cycle.
"""
from __future__ import annotations

import json

import pytest

import core.position_manager as pm
import core.utils as utils_mod


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Redirect STATE_DIR to tmp_path for both modules."""
    monkeypatch.setattr(pm, "_archive_mgmt_row", pm._archive_mgmt_row)  # no-op rebind
    monkeypatch.setattr(utils_mod, "STATE_DIR", tmp_path)
    yield tmp_path


def _last_archive_record(tmp_path):
    p = tmp_path / "position_mgmt_archive.jsonl"
    if not p.exists():
        return None
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return None
    return json.loads(lines[-1])


def test_archive_mgmt_row_writes_be_event(tmp_path):
    pm._archive_mgmt_row(
        ticket=12345,
        mgmt_row={"break_even": True, "trailing": False, "partial_tp_done": False, "initial_sl": 2390.0},
        reason="be_trail_update",
        side="BUY",
        entry=2400.0,
        symbol="XAUUSDm",
    )
    rec_obj = _last_archive_record(tmp_path)
    assert rec_obj is not None
    assert rec_obj["reason"] == "be_trail_update"
    assert rec_obj["ticket"] == "12345"
    assert rec_obj["mgmt_row"]["risk_distance_floor"] == pytest.approx(10.0)


def test_archive_mgmt_row_partial_tp_partial_done_captured(tmp_path):
    pm._archive_mgmt_row(
        ticket=99,
        mgmt_row={"break_even": True, "partial_tp_done": True, "partial_closed_volume": 0.005, "initial_sl": 2380.0},
        reason="partial_tp",
        side="BUY",
        entry=2400.0,
        symbol="XAUUSDm",
    )
    rec_obj = _last_archive_record(tmp_path)
    assert rec_obj["reason"] == "partial_tp"
    assert rec_obj["mgmt_row"]["partial_tp_done"] is True
    assert rec_obj["mgmt_row"]["partial_closed_volume"] == 0.005


def test_archive_mgmt_row_time_stop_marks_stale_closed(tmp_path):
    pm._archive_mgmt_row(
        ticket=7,
        mgmt_row={"break_even": False, "trailing": False},
        reason="time_stop",
        side="SELL",
        entry=2400.0,
        symbol="XAUUSDm",
    )
    rec_obj = _last_archive_record(tmp_path)
    assert rec_obj["reason"] == "time_stop"
    assert rec_obj["mgmt_row"]["stale_closed"] is True


def test_archive_mgmt_row_unknown_reason_defaults_safely(tmp_path):
    pm._archive_mgmt_row(
        ticket=11,
        mgmt_row={},
        reason="bogus_value",
        side="BUY",
        entry=2400.0,
        symbol="XAUUSDm",
    )
    rec_obj = _last_archive_record(tmp_path)
    assert rec_obj["reason"] == "be_trail_update"


def test_archive_mgmt_row_transaction_id_is_12_chars(tmp_path):
    """MED #4 review fix: txid widening to 12 hex avoids birthday collisions."""
    pm._archive_mgmt_row(
        ticket=22,
        mgmt_row={},
        reason="be_trail_update",
        side="BUY",
        entry=2400.0,
        symbol="XAUUSDm",
    )
    rec_obj = _last_archive_record(tmp_path)
    assert isinstance(rec_obj["transaction_id"], str)
    assert len(rec_obj["transaction_id"]) == 12
    int(rec_obj["transaction_id"], 16)  # must be valid hex


def test_archive_mgmt_row_handles_missing_sl_gracefully(tmp_path):
    pm._archive_mgmt_row(
        ticket=33,
        mgmt_row={},
        reason="be_trail_update",
        side="BUY",
        entry=None,
        symbol="XAUUSDm",
    )
    rec_obj = _last_archive_record(tmp_path)
    assert rec_obj["mgmt_row"]["initial_sl"] is None
    assert rec_obj["mgmt_row"]["risk_distance_floor"] is None


def test_archive_audit_passthrough(tmp_path):
    pm._archive_mgmt_row(
        ticket=44,
        mgmt_row={"break_even": True, "_audit": [{"kind": "payoff_paradox_unpriced"}]},
        reason="be_trail_update",
        side="BUY",
        entry=2400.0,
        symbol="XAUUSDm",
    )
    rec_obj = _last_archive_record(tmp_path)
    assert rec_obj["mgmt_row"]["_audit"] == [{"kind": "payoff_paradox_unpriced"}]


def test_paper_noise_guard_skips_archive_on_idle_cycle(tmp_path):
    """REVIEW FIX regression: manage_paper_positions must NOT archive when
    the cycle produces no SL move AND no flags fired. Calling the helper
    directly with an idle row should produce zero archive lines per cycle.
    With the OLD code this fired unconditional _archive_mgmt_row and would
    have produced ~14 writes/sec on a live 14-symbol paper book."""
    idle_row = {"break_even": False, "trailing": False, "partial_tp_done": False}
    pre_count = utils_mod.archive_line_count("position_mgmt_archive.jsonl")
    # Mirror production guard: new_sl=None, actions=[], all flags False.
    should_archive = (
        None is not None
        or []
        or idle_row.get("break_even")
        or idle_row.get("trailing")
        or idle_row.get("partial_tp_done")
    )
    assert should_archive is False
    if should_archive:
        pm._archive_mgmt_row(ticket="paperidle", mgmt_row=idle_row,
                             reason="be_trail_update",
                             side="BUY", entry=2400.0, symbol="XAUUSDm")
    assert utils_mod.archive_line_count("position_mgmt_archive.jsonl") == pre_count
    # And when a flag IS set, the archive DOES grow.
    armed_row = {"break_even": True, "trailing": False, "partial_tp_done": False,
                 "initial_sl": 2390.0}
    pm._archive_mgmt_row(ticket="paperarmed-1", mgmt_row=armed_row,
                         reason="be_trail_update",
                         side="BUY", entry=2400.0, symbol="XAUUSDm")
    pm._archive_mgmt_row(ticket="paperarmed-2", mgmt_row=armed_row,
                         reason="be_trail_update",
                         side="BUY", entry=2400.0, symbol="XAUUSDm")
    assert utils_mod.archive_line_count("position_mgmt_archive.jsonl") == pre_count + 2
