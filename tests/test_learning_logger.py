"""Tests for the normalized learning logger (Phase 2.4)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import learning_logger as ll
from core.learning_schema import build_decision_event, config_snapshot_hash


def test_decision_is_normalized_to_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(ll, "LOG_DIR", tmp_path)
    ev = build_decision_event(
        timestamp="2026-07-08T23:01:00",
        symbol="XAUUSDm", timeframe="M5", mode="fast", price=4077.4,
        spread_points=32, atr=4.0, side="BUY", confidence=0.61,
        decision="skip", reason="m5_recovery_but_m15_bearish_context",
        guards={"spread_ok": True, "risk_ok": True},
        config_hash="abc123", profile="growth",
    )
    ll.log_decision(ev)
    lines = (tmp_path / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert "category" not in row
    assert row["symbol"] == "XAUUSDm"
    assert row["decision"] == "skip"
    assert row["reason"] == "m5_recovery_but_m15_bearish_context"
    assert row["guards"]["spread_ok"] is True


def test_read_jsonl_most_recent_first(tmp_path, monkeypatch):
    monkeypatch.setattr(ll, "LOG_DIR", tmp_path)
    for i in range(3):
        ll.log_decision(build_decision_event(symbol="USOILm", decision="wait", reason=f"r{i}"))
    rows = ll.read_jsonl("decisions", limit=10)
    assert len(rows) == 3
    # most-recent-first
    assert rows[0]["reason"] == "r2"


def test_config_snapshot_hash_stable():
    c = {"evaluation": {"min_policy_score": 35}, "signals": {"min_confidence": 50}}
    h1 = config_snapshot_hash(c)
    h2 = config_snapshot_hash(dict(c))
    assert h1 == h2 and len(h1) == 12


def test_each_log_file_writes_to_own_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(ll, "LOG_DIR", tmp_path)
    ll.log_order({"symbol": "XAUUSDm", "action": "buy_market"})
    ll.log_position_management({"symbol": "XAUUSDm", "action": "break_even"})
    ll.log_outcome({"symbol": "XAUUSDm", "result": "win"})
    ll.log_error({"symbol": "XAUUSDm", "error": "market_closed"})
    ll.log_review({"trade_id": "1", "rating_total": 60})
    ll.log_config_proposal({"proposal_id": "p1"})
    ll.log_config_change({"action": "apply_limited"})
    for name in ("orders", "position_management", "outcomes", "errors", "reviews", "config_proposals", "config_changes"):
        assert (tmp_path / ll._FILES[name]).exists(), name
