"""Tests for core/utils.py JSONL archive helpers (Tier-2 mgmt archive)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import core.utils as utils_mod


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Point core.utils.STATE_DIR to a tmp_path so the tests never touch real state."""
    monkeypatch.setattr(utils_mod, "STATE_DIR", tmp_path)
    yield tmp_path


def _record(ticket: str, txid: str, reason: str = "be_trail_update", **extra):
    base = {
        "ticket": ticket,
        "transaction_id": txid,
        "reason": reason,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "mgmt_row": {"break_even": True, "trailing": False, "partial_tp_done": False},
    }
    base.update(extra)
    return base


def test_append_creates_jsonl_with_one_line(tmp_path):
    path = utils_mod.append_archive_record("archive_x", _record("t1", "aaaa1111"))
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["ticket"] == "t1"
    assert obj["transaction_id"] == "aaaa1111"


def test_append_idempotent_on_same_transaction_id(tmp_path):
    utils_mod.append_archive_record("archive_x", _record("t1", "dup00001"))
    utils_mod.append_archive_record("archive_x", _record("t1", "dup00001", reason="partial_tp"))
    assert utils_mod.archive_line_count("archive_x") == 1


def test_append_different_txids_accumulate(tmp_path):
    utils_mod.append_archive_record("archive_x", _record("t1", "tx-a"))
    utils_mod.append_archive_record("archive_x", _record("t2", "tx-b"))
    utils_mod.append_archive_record("archive_x", _record("t3", "tx-c"))
    assert utils_mod.archive_line_count("archive_x") == 3


def test_read_archive_index_latest_per_key_wins(tmp_path):
    utils_mod.append_archive_record("archive_x", _record("t1", "tx-1", mgmt_row={"break_even": False}))
    utils_mod.append_archive_record("archive_x", _record("t1", "tx-2", mgmt_row={"break_even": True, "trailing": True}))
    utils_mod.append_archive_record("archive_x", _record("t2", "tx-3", mgmt_row={"break_even": False}))
    idx = utils_mod.read_archive_index("archive_x")
    assert set(idx.keys()) == {"t1", "t2"}
    assert idx["t1"]["transaction_id"] == "tx-2"
    assert idx["t1"]["mgmt_row"]["trailing"] is True
    assert idx["t2"]["mgmt_row"]["break_even"] is False


def test_read_archive_for_ticket_returns_latest(tmp_path):
    utils_mod.append_archive_record("archive_x", _record("t1", "old", reason="be_trail_update"))
    utils_mod.append_archive_record("archive_x", _record("t1", "new", reason="time_stop"))
    rec = utils_mod.read_archive_for_ticket("archive_x", "t1")
    assert rec is not None
    assert rec["transaction_id"] == "new"
    assert rec["reason"] == "time_stop"


def test_read_archive_for_ticket_missing_returns_none(tmp_path):
    assert utils_mod.read_archive_for_ticket("archive_x", "missing") is None


def test_archive_pruning_keeps_tail(tmp_path):
    for i in range(20):
        utils_mod.append_archive_record("archive_x", _record(f"t{i}", f"tx{i:04d}"), max_lines=10)
    assert utils_mod.archive_line_count("archive_x") == 10
    idx = utils_mod.read_archive_index("archive_x")
    assert set(idx.keys()) == {f"t{i}" for i in range(10, 20)}


def test_append_archive_record_no_pruning_when_max_lines_default(tmp_path):
    for i in range(60):
        utils_mod.append_archive_record("archive_x", _record(f"t{i}", f"tx{i:04d}"))
    assert utils_mod.archive_line_count("archive_x") == 60


def test_archive_index_skips_garbled_lines(tmp_path):
    utils_mod.append_archive_record("archive_x", _record("t1", "ok-tx"))  # warm cache
    # Now corrupt the file with a non-json line; read_archive_index should skip
    p = utils_mod._archive_path("archive_x")
    with p.open("a", encoding="utf-8") as h:
        h.write("not-a-json-line\n")
    utils_mod.append_archive_record("archive_x", _record("t1", "fix01"))
    idx = utils_mod.read_archive_index("archive_x")
    assert idx["t1"]["transaction_id"] == "fix01"


def test_append_sets_archived_at_when_missing(tmp_path):
    rec = {"ticket": "t1", "transaction_id": "auto0001", "reason": "be_trail_update", "mgmt_row": {}}
    utils_mod.append_archive_record("archive_x", rec)
    obj = json.loads(utils_mod._archive_path("archive_x").read_text(encoding="utf-8").strip().splitlines()[0])
    assert "archived_at" in obj
