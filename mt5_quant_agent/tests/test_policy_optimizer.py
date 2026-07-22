"""Policy optimizer — sample gates and best policy selection."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.evaluation_policy import merge_best_policy
from core.policy_optimizer import (
    BEST_POLICIES_FILE,
    POLICY_SCORES_FILE,
    build_best_policies,
    count_samples_by_cell,
    exit_labels_suspect,
    pick_best_policy,
    policy_cell_key,
    resolve_gate,
    run,
)
from core.policy_score import session_entry_bias
from core.state_store import StateStore


def _trade(
    *,
    symbol: str = "XAUUSDm",
    setup: str = "pullback",
    session: str = "rollover",
    pnl: float = 1.0,
    result: str = "win",
    exit_reason: str = "take_profit",
) -> dict:
    return {
        "trade_id": f"{symbol}-{setup}-{session}-{pnl}",
        "symbol": symbol,
        "setup_type": setup,
        "side": "BUY",
        "pnl": pnl,
        "result": result,
        "exit_reason": exit_reason,
        "market_context": {"session": session, "market_regime": {"primary": "strong_trend", "bias": "bullish"}},
    }


def _policy_scores_doc() -> dict:
    cell = policy_cell_key("XAUUSDm", "pullback", "rollover")
    return {
        "timestamp": "2026-07-07T00:00:00+00:00",
        "policies": {
            cell: [
                {
                    "entry_type": "limit",
                    "policy_score": 55.0,
                    "management_profile": {
                        "entry_type": "limit",
                        "limit_offset_atr": 0.2,
                        "break_even_trigger_r": 0.3,
                    },
                },
                {
                    "entry_type": "market",
                    "policy_score": 72.5,
                    "management_profile": {
                        "entry_type": "market",
                        "limit_offset_atr": 0.05,
                        "break_even_trigger_r": 0.5,
                    },
                },
            ],
        },
    }


def test_resolve_gate_thresholds():
    assert resolve_gate(5, "auto", min_suggest=10, min_soft=30, min_auto=50) == "observe"
    assert resolve_gate(15, "shadow", min_suggest=10, min_soft=30, min_auto=50) == "suggest"
    assert resolve_gate(35, "shadow", min_suggest=10, min_soft=30, min_auto=50) == "soft"
    assert resolve_gate(60, "shadow", min_suggest=10, min_soft=30, min_auto=50) == "soft"
    assert resolve_gate(60, "auto", min_suggest=10, min_soft=30, min_auto=50) == "auto"
    assert resolve_gate(60, "auto", min_suggest=10, min_soft=30, min_auto=50, auto_blocked=True) == "soft"


def test_exit_labels_suspect_detects_loss_take_profit():
    clean = [_trade(pnl=1.0, result="win")]
    suspect = [_trade(pnl=-2.0, result="loss", exit_reason="take_profit")]
    assert not exit_labels_suspect(clean)
    assert exit_labels_suspect(suspect)


def test_pick_best_policy_highest_score():
    best = pick_best_policy({
        "variants": [
            {"entry_type": "limit", "policy_score": 40},
            {"entry_type": "market", "policy_score": 68},
        ],
    })
    assert best["entry_type"] == "market"
    assert best["policy_score"] == 68


def test_count_samples_by_cell():
    trades = [_trade() for _ in range(12)] + [_trade(symbol="USOILm", session="tokyo") for _ in range(3)]
    counts = count_samples_by_cell(trades)
    assert counts[policy_cell_key("XAUUSDm", "pullback", "rollover")] == 12
    assert counts[policy_cell_key("USOILm", "pullback", "tokyo")] == 3


def test_build_best_policies_selects_winner_and_gate(tmp_path: Path, monkeypatch):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    config = {
        "policy_optimizer": {
            "enabled": True,
            "mode": "shadow",
            "min_trades_suggest": 10,
            "min_trades_soft": 30,
            "min_trades_auto": 50,
        },
    }
    trades = [_trade() for _ in range(35)]
    doc = build_best_policies(_policy_scores_doc(), trades, config)
    cell = policy_cell_key("XAUUSDm", "pullback", "rollover")
    row = doc["policies"][cell]
    assert row["entry_type"] == "market"
    assert row["policy_score"] == pytest.approx(72.5)
    assert row["sample_n"] == 35
    assert row["gate"] == "soft"


def test_build_best_policies_auto_blocked_on_suspect_labels(tmp_path: Path, monkeypatch):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    config = {
        "policy_optimizer": {
            "enabled": True,
            "mode": "auto",
            "min_trades_suggest": 10,
            "min_trades_soft": 30,
            "min_trades_auto": 50,
        },
    }
    trades = [_trade(pnl=-1.0, result="loss", exit_reason="take_profit") for _ in range(55)]
    doc = build_best_policies(_policy_scores_doc(), trades, config)
    assert doc["auto_blocked"] is True
    cell = policy_cell_key("XAUUSDm", "pullback", "rollover")
    assert doc["policies"][cell]["gate"] == "soft"


def test_run_writes_json_and_sqlite(tmp_path: Path, monkeypatch):
    from core import state_store, utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)
    monkeypatch.setattr(state_store, "STATE_DIR", state_dir)

    (state_dir / POLICY_SCORES_FILE).write_text(json.dumps(_policy_scores_doc()), encoding="utf-8")
    (state_dir / "paper_trades.json").write_text(
        json.dumps({"trades": [_trade() for _ in range(15)]}),
        encoding="utf-8",
    )

    config = {
        "policy_optimizer": {"enabled": True, "mode": "shadow"},
        "state_store": {"enabled": True, "dual_write_json": True, "read_from_sqlite": True},
    }
    db_path = state_dir / "test.db"
    config["state_store"]["db_path"] = str(db_path.name)

    doc = run(config)
    assert doc is not None
    assert (state_dir / BEST_POLICIES_FILE).exists()
    out = json.loads((state_dir / BEST_POLICIES_FILE).read_text(encoding="utf-8"))
    assert out["cells_selected"] == 1

    store = StateStore(db_path)
    kv = store.read_best_policies()
    assert kv.get("policies")


def test_merge_best_policy_biases_session(tmp_path: Path, monkeypatch):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    cell = policy_cell_key("XAUUSDm", "pullback", "rollover")
    (state_dir / BEST_POLICIES_FILE).write_text(
        json.dumps({
            "policies": {
                cell: {
                    "entry_type": "market",
                    "policy_score": 70.0,
                    "sample_n": 20,
                    "gate": "suggest",
                    "management_profile": {
                        "entry_type": "market",
                        "break_even_trigger_r": 0.55,
                        "trail_start_r": 0.8,
                    },
                },
            },
        }),
        encoding="utf-8",
    )

    base = session_entry_bias("rollover")
    merged = merge_best_policy(base, symbol="XAUUSDm", setup="pullback", session="rollover")
    assert merged["entry_type"] == "market"
    assert merged["break_even_trigger_r"] == pytest.approx(0.55)
    assert merged["best_policy_gate"] == "suggest"


def test_merge_best_policy_skips_observe_gate(tmp_path: Path, monkeypatch):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    cell = policy_cell_key("XAUUSDm", "pullback", "rollover")
    (state_dir / BEST_POLICIES_FILE).write_text(
        json.dumps({
            "policies": {
                cell: {
                    "entry_type": "market",
                    "sample_n": 5,
                    "gate": "observe",
                    "management_profile": {"break_even_trigger_r": 0.99},
                },
            },
        }),
        encoding="utf-8",
    )

    base = session_entry_bias("rollover")
    merged = merge_best_policy(base, symbol="XAUUSDm", setup="pullback", session="rollover")
    assert merged == base