"""Phase 2 SQLite state store tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.state_store import StateStore


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    db = tmp_path / "test_quant_os.db"
    return StateStore(db)


def _sample_candidate() -> dict:
    return {
        "signal_id": "sig-001",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "setup_type": "pullback",
        "confidence": 85,
        "entry": 2400.0,
        "sl": 2390.0,
        "tp1": 2420.0,
        "tp2": 2430.0,
        "entry_quality": 0.8,
    }


def test_schema_init_and_health(store: StateStore) -> None:
    ok, msg = store.health_check()
    assert ok, msg
    assert store.db_path.exists()


def test_signals_roundtrip(store: StateStore) -> None:
    doc = {
        "timestamp": "2026-07-06T12:00:00+00:00",
        "engine": "decision_engine",
        "candidates": [_sample_candidate()],
    }
    store.write_signals(doc)
    out = store.read_signals()
    assert out["count"] == 1
    assert out["candidates"][0]["signal_id"] == "sig-001"
    assert out["engine"] == "decision_engine"


def test_signals_replace_cycle(store: StateStore) -> None:
    store.write_signals({"timestamp": "t1", "candidates": [_sample_candidate()]})
    store.write_signals({
        "timestamp": "t2",
        "candidates": [{**_sample_candidate(), "signal_id": "sig-002", "symbol": "USOILm"}],
    })
    out = store.read_signals()
    assert out["count"] == 1
    assert out["candidates"][0]["signal_id"] == "sig-002"


def test_approved_rejected_roundtrip(store: StateStore) -> None:
    approved_doc = {
        "timestamp": "2026-07-06T12:01:00+00:00",
        "approved": [{**_sample_candidate(), "verifier_score": 90}],
    }
    rejected_doc = {
        "timestamp": "2026-07-06T12:01:00+00:00",
        "rejected": [{**_sample_candidate(), "signal_id": "sig-rej", "reason": "spread"}],
    }
    store.write_approved(approved_doc)
    store.write_rejected(rejected_doc)
    assert store.read_approved()["count"] == 1
    assert store.read_rejected()["count"] == 1
    assert store.read_rejected()["rejected"][0]["reason"] == "spread"


def test_positions_orders_trades(store: StateStore) -> None:
    store.write_positions({
        "timestamp": "t",
        "mode": "mt5",
        "positions": [{
            "position_id": "p1",
            "symbol": "XAUUSDm",
            "side": "BUY",
            "size": 0.01,
            "entry": 2400.0,
            "sl": 2390.0,
            "tp1": 2420.0,
            "ticket": 12345,
        }],
    })
    store.write_orders({
        "timestamp": "t",
        "orders": [{
            "order_id": "o1",
            "signal_id": "sig-001",
            "symbol": "XAUUSDm",
            "side": "BUY",
            "status": "filled",
            "entry": 2400.0,
        }],
    })
    store.write_trades({
        "timestamp": "t",
        "trades": [{
            "trade_id": "tr1",
            "symbol": "XAUUSDm",
            "side": "BUY",
            "entry": 2400.0,
            "exit": 2410.0,
            "pnl": 1.0,
            "setup_type": "pullback",
            "closed_at": "t",
        }],
    })
    pos = store.read_positions()
    assert len(pos["positions"]) == 1
    assert pos["positions"][0]["position_id"] == "p1"


def test_kv_and_audit(store: StateStore) -> None:
    store.set_kv("kill_switch", {"kill_switch": False})
    assert store.get_kv("kill_switch") == {"kill_switch": False}
    store.append_audit("signal.approved", symbol="XAUUSDm", details={"side": "BUY"})
    row = store.get_kv("kill_switch")
    assert row["kill_switch"] is False


def test_seed_from_json_idempotent(store: StateStore, tmp_path: Path, monkeypatch) -> None:
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    cand = {"timestamp": "t", "candidates": [_sample_candidate()]}
    (state_dir / "candidate_signals.json").write_text(json.dumps(cand), encoding="utf-8")
    (state_dir / "approved_signals.json").write_text(
        json.dumps({"timestamp": "t", "approved": [_sample_candidate()]}),
        encoding="utf-8",
    )
    (state_dir / "rejected_signals.json").write_text(
        json.dumps({"timestamp": "t", "rejected": []}),
        encoding="utf-8",
    )
    (state_dir / "paper_positions.json").write_text(
        json.dumps({"timestamp": "t", "positions": []}),
        encoding="utf-8",
    )
    (state_dir / "paper_orders.json").write_text(
        json.dumps({"timestamp": "t", "orders": []}),
        encoding="utf-8",
    )
    (state_dir / "paper_trades.json").write_text(
        json.dumps({"timestamp": "t", "trades": []}),
        encoding="utf-8",
    )

    store2 = StateStore(tmp_path / "seed.db")
    counts1 = store2.seed_from_json()
    counts2 = store2.seed_from_json()
    assert counts1["signals"] == 1
    assert counts2["signals"] == 1
    assert store2.read_signals()["count"] == 1