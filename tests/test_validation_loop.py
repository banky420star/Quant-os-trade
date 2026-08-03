"""Tests for the safe offline validation loop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loops import validation_loop


def test_run_instances_honors_requested_count_and_bounds() -> None:
    orders = [{"status": "filled", "price": 100.0}]
    result = validation_loop._run_instances(10_000, orders, {"kill_switch": False, "approved_count": 0})
    assert result["requested"] == 10_000
    assert result["completed"] == 10_000
    assert result["passed"] == 10_000
    assert result["failed"] == 0
    assert result["kind"] == "deterministic_consistency_checks"
    assert result["independent_simulations"] is False


def test_orders_for_review_does_not_use_mt5_rows_as_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    docs = {
        "mt5_orders.json": {},
        "paper_orders.json": {"mode": "mt5", "orders": [{"status": "filled"}]},
    }
    monkeypatch.setattr(validation_loop, "_safe_load", lambda name, default: docs.get(name, default))
    source, orders = validation_loop._orders_for_review()
    assert source == "no_compatible_ledger"
    assert orders == []


def test_run_persists_report_and_history_without_mutating_trade_ledgers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    writes: dict[str, object] = {}

    def fake_read(name: str, default=None):
        docs = {
            "mt5_orders.json": {},
            "paper_orders.json": {"mode": "paper", "orders": [{"status": "filled", "price": 100.0}]},
            "mt5_trades.json": {},
            "paper_trades.json": {"trades": [{"pnl": 2.0}]},
            "approved_signals.json": {"count": 0},
            "kill_switch.json": {"kill_switch": False},
            "runtime_mode.json": {"label": "growth"},
            "validation_history.json": [],
        }
        return docs.get(name, default)

    monkeypatch.setattr(validation_loop, "ensure_dirs", lambda: None)
    monkeypatch.setattr(validation_loop, "_safe_load", fake_read)
    monkeypatch.setattr(validation_loop, "_run_tests", lambda targets, timeout: {"status": "passed", "targets": targets})
    monkeypatch.setattr(validation_loop, "_software_review", lambda: {"verdict": "ok", "files_checked": 0})
    monkeypatch.setattr(validation_loop, "write_json_state", lambda name, data: writes.__setitem__(name, data))
    monkeypatch.setattr(validation_loop, "setup_logger", lambda *args, **kwargs: type("Logger", (), {"info": lambda *a, **k: None})())

    result = validation_loop.run({"validation_loop": {"enabled": True, "instance_count": 3}})

    assert result["safety"]["offline_only"] is True
    assert result["safety"]["orders_submitted"] == 0
    assert result["profit_review"]["net_pnl"] == 2.0
    assert result["instances"]["completed"] == 3
    assert "validation_cycle.json" in writes
    assert "validation_history.json" in writes
    assert "paper_orders.json" not in writes
    assert "paper_trades.json" not in writes


def test_trades_for_review_does_not_use_mt5_rows_as_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    docs = {
        "mt5_trades.json": {},
        "paper_trades.json": {"mode": "mt5", "trades": [{"pnl": 99}]},
    }
    monkeypatch.setattr(validation_loop, "_safe_load", lambda name, default: docs.get(name, default))
    source, trades = validation_loop._trades_for_review()
    assert source == "no_compatible_ledger"
    assert trades == []


def test_malformed_ledgers_are_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    docs = {
        "mt5_orders.json": [],
        "paper_orders.json": [],
        "mt5_trades.json": [],
        "paper_trades.json": [],
        "runtime_mode.json": {"label": "growth"},
    }
    monkeypatch.setattr(validation_loop, "_safe_load", lambda name, default: docs.get(name, default))
    source, orders = validation_loop._orders_for_review()
    trade_source, trades = validation_loop._trades_for_review()
    software = validation_loop._software_review()
    assert source == "no_compatible_ledger"
    assert orders == []
    assert trade_source == "no_compatible_ledger"
    assert trades == []
    assert software["namespace_consistent"] is True


def test_run_marks_failed_test_as_attention(monkeypatch: pytest.MonkeyPatch) -> None:
    writes: dict[str, object] = {}
    monkeypatch.setattr(validation_loop, "ensure_dirs", lambda: None)
    monkeypatch.setattr(validation_loop, "_safe_load", lambda name, default: {
        "mt5_orders.json": {"mode": "mt5", "orders": [{"status": "failed", "error": "x"}]},
        "mt5_trades.json": {"mode": "mt5", "trades": []},
        "approved_signals.json": {"count": 0},
        "kill_switch.json": {"kill_switch": False},
        "runtime_mode.json": {"label": "growth"},
        "validation_history.json": [],
    }.get(name, default))
    monkeypatch.setattr(validation_loop, "_run_tests", lambda targets, timeout: {"status": "failed", "returncode": 1})
    monkeypatch.setattr(validation_loop, "_software_review", lambda: {"verdict": "ok"})
    monkeypatch.setattr(validation_loop, "write_json_state", lambda name, data: writes.__setitem__(name, data))
    monkeypatch.setattr(validation_loop, "setup_logger", lambda *args, **kwargs: type("Logger", (), {"info": lambda *a, **k: None})())

    result = validation_loop.run({"validation_loop": {"enabled": True, "instance_count": 1}})

    assert result["status"] == "attention"
    assert "focused tests failed" in result["warnings"]
    assert json.loads(json.dumps(writes["validation_cycle.json"]))["next_action"] == "manual_review"
