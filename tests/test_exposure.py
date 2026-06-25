"""Exposure limit and equity-based sizing tests."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.exposure import (
    calc_risk_based_size,
    cap_size_to_exposure_limits,
    check_exposure_limits,
    exposure_used_pct,
)
from core.paper_broker import PaperBroker
from core.risk_manager import RiskManager
from core.dynamic_entry import evaluate_dynamic_entry, symbol_capacity_available
from core.position_manager import compute_managed_sl
from core.trade_limits import is_duplicate_position
from core.verifier import Verifier


@pytest.fixture
def config():
    from core.utils import load_config
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg["execution"]["mode"] = "paper"
    cfg["risk"]["unlimited_trades"] = False
    cfg["risk"]["max_symbol_exposure_usd"] = 50
    cfg["risk"]["max_total_exposure_usd"] = 100
    cfg["signals"]["default_risk_percent"] = 1
    return cfg


def test_position_size_based_on_equity():
    equity = 10_000.0
    size = calc_risk_based_size(equity, 1.0, entry=100.0, sl=99.0)
    # 1% of 10k = $100 risk / $1 per unit = 100 units
    assert size == 100.0


def test_exposure_used_pct():
    assert exposure_used_pct(75.0, 100.0) == 75.0
    assert exposure_used_pct(150.0, 100.0) == 100.0


def test_verifier_rejects_exposure_limit_exceeded(config):
    config["trading"]["dynamic_entries"] = {"enabled": False}
    existing = [{
        "symbol": "XAUUSDm",
        "side": "BUY",
        "entry": 2650.0,
        "size": 0.04,
        "setup_type": "trend_continuation",
    }]
    signal = {
        "signal_id": "exp-test-1",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "setup_type": "pullback",
        "entry": 2650.0,
        "sl": 2640.0,
        "tp1": 2665.0,
        "confidence": 85,
    }
    features = {
        "symbols": {
            "XAUUSDm": {
                "price": 2650.0,
                "m5_trend": "bullish",
                "m15_trend": "bullish",
                "atr_ratio": 0.002,
                "volume_ratio": 1.2,
                "support": 2640.0,
                "resistance": 2665.0,
            }
        }
    }

    ok, _ = check_exposure_limits(existing, signal, equity=1000.0, config=config)
    assert ok is False

    verifier = Verifier(config)
    approved, rejected = verifier.verify_batch(
        [signal],
        features,
        active_signals=existing,
        spread_data={"XAUUSDm": 20.0},
        equity=1000.0,
    )
    assert len(approved) == 0
    assert len(rejected) == 1
    assert "exposure_limit_exceeded" in rejected[0]["failures"]
    assert rejected[0]["rejection_reason"] == "exposure_limit_exceeded"


def test_paper_broker_blocks_oversized_exposure(config):
    config["trading"]["dynamic_entries"] = {"enabled": False}
    positions = [{
        "position_id": "p1",
        "symbol": "USOILm",
        "side": "SELL",
        "entry": 70.0,
        "size": 1.4,
        "sl": 71.0,
        "tp1": 68.0,
        "setup_type": "trend_continuation",
    }]
    signal = {
        "signal_id": "exp-test-2",
        "symbol": "USOILm",
        "side": "SELL",
        "setup_type": "pullback",
        "entry": 70.0,
        "sl": 71.0,
        "tp1": 68.0,
        "confidence": 90,
        "reason": "test",
    }
    approved = [{"signal": signal, "approved": True, "signal_id": signal["signal_id"]}]
    broker = PaperBroker(config)
    result = broker.process_approved_signals(
        approved,
        {"USOILm": 70.0},
        existing_positions=positions,
        balance_state={"cash": 1000.0, "equity": 1000.0, "starting_cash": 1000.0},
    )
    rejected_orders = [o for o in result["orders"] if o.get("status") == "rejected"]
    assert len(rejected_orders) == 1
    assert rejected_orders[0]["error"] == "exposure_limit_exceeded"
    assert len(result["positions"]) == 1


def test_max_five_open_per_symbol(config):
    config["trading"]["max_open_per_symbol"] = 5
    positions = [
        {"symbol": "XAUUSDm", "side": "SELL", "ticket": i}
        for i in range(5)
    ]
    assert symbol_capacity_available(config, "XAUUSDm", positions) is False
    assert symbol_capacity_available(config, "USOILm", positions) is True


def test_dynamic_entry_requires_positive_stack(config):
    config["trading"]["dynamic_entries"] = {"enabled": True, "require_positive_stack_pnl": True}
    positions = [{
        "symbol": "XAUUSDm",
        "side": "SELL",
        "entry": 4000.0,
        "size": 0.01,
        "profit": -5.0,
    }]
    signal = {
        "signal_id": "dyn-1",
        "symbol": "XAUUSDm",
        "side": "SELL",
        "setup_type": "pullback",
        "entry": 3990.0,
        "sl": 4010.0,
        "tp1": 3960.0,
        "confidence": 80,
    }
    feat = {"price": 3985.0, "atr": 8.0}
    ok, _, reason = evaluate_dynamic_entry(config, signal, positions, feat)
    assert ok is False
    assert "stack_not_positive" in (reason or "")


def test_break_even_moves_sl_on_buy(config):
    pos = {"symbol": "XAUUSDm", "side": "BUY", "entry": 100.0, "sl": 95.0}
    new_sl, row, actions = compute_managed_sl(
        config,
        pos,
        current_price=101.0,
        atr=2.0,
        mgmt_row={},
    )
    assert new_sl is not None
    assert new_sl > 95.0
    assert "break_even" in actions


def test_pyramiding_allows_different_signals_same_side(config):
    config["execution"]["mode"] = "mt5"
    config["trading"]["allow_pyramiding"] = True
    config["risk"]["unlimited_trades"] = False

    existing = [{
        "symbol": "XAUUSDm",
        "side": "SELL",
        "setup_type": "pullback",
        "signal_id": "sig-a",
        "ticket": 1001,
    }]
    new_signal = {
        "signal_id": "sig-b",
        "symbol": "XAUUSDm",
        "side": "SELL",
        "setup_type": "trend_continuation",
        "entry": 2650.0,
        "sl": 2660.0,
        "tp1": 2635.0,
        "confidence": 80,
    }
    assert is_duplicate_position(config, new_signal, existing) is False

    same_setup_new_signal = {**new_signal, "signal_id": "sig-c", "setup_type": "pullback"}
    assert is_duplicate_position(config, same_setup_new_signal, existing) is False

    repeat_signal = {**new_signal, "signal_id": "sig-a"}
    assert is_duplicate_position(config, repeat_signal, existing) is True


def test_pyramiding_blocked_without_flag(config):
    config["execution"]["mode"] = "mt5"
    config["trading"]["allow_pyramiding"] = False
    config["risk"]["unlimited_trades"] = False

    existing = [{"symbol": "USOILm", "side": "BUY", "setup_type": "breakout"}]
    signal = {
        "signal_id": "sig-x",
        "symbol": "USOILm",
        "side": "BUY",
        "setup_type": "pullback",
        "entry": 70.0,
        "sl": 69.0,
        "tp1": 71.0,
        "confidence": 80,
    }
    assert is_duplicate_position(config, signal, existing) is True


def test_risk_state_includes_exposure_used_pct(config):
    positions = [{"symbol": "XAUUSDm", "entry": 100.0, "size": 0.5}]
    manager = RiskManager(config)
    result = manager.evaluate(
        positions,
        [],
        {"equity": 1000.0, "cash": 1000.0, "starting_cash": 1000.0},
    )
    state = result["risk_state"]
    assert "exposure_used_pct" in state
    assert state["total_exposure"] == 50.0
    assert state["exposure_used_pct"] == 50.0