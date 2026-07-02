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
from core.position_manager import _clamp_sl_to_stops_level, _trail_distance_price, compute_managed_sl
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
    config["trading"]["max_open_per_symbol"] = 5
    config["trading"]["allow_pyramiding"] = True
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
    assert "exposure_limit_exceeded" in rejected[0]["failure_codes"]
    assert "Exposure limit" in rejected[0]["rejection_reason"]


def test_paper_broker_blocks_oversized_exposure(config):
    config["trading"]["dynamic_entries"] = {"enabled": False}
    config["trading"]["max_open_per_symbol"] = 5
    config["trading"]["allow_pyramiding"] = True
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


def test_session_trades_uncapped_by_default(config):
    from core.trade_limits import session_trade_capacity_available

    config["trading"]["max_session_trades_per_symbol"] = 0
    trades = [{"symbol": "XAUUSDm", "pnl": 1.0} for _ in range(20)]
    assert session_trade_capacity_available(config, "XAUUSDm", trades) is True


def test_session_trades_ignore_pre_reset_history(config, monkeypatch):
    from core import trade_limits
    from core.trade_limits import session_trade_capacity_available

    config["trading"]["max_session_trades_per_symbol"] = 3
    monkeypatch.setattr(
        trade_limits,
        "session_start_ts",
        lambda: __import__("datetime").datetime(2026, 6, 25, 16, 0, tzinfo=__import__("datetime").timezone.utc),
    )
    trades = [
        {"symbol": "XAUUSDm", "closed_at": "2026-06-25T11:00:00+00:00"},
        {"symbol": "XAUUSDm", "closed_at": "2026-06-25T11:05:00+00:00"},
        {"symbol": "XAUUSDm", "closed_at": "2026-06-25T11:10:00+00:00"},
        {"symbol": "XAUUSDm", "closed_at": "2026-06-25T16:30:00+00:00"},
    ]
    assert session_trade_capacity_available(config, "XAUUSDm", trades) is True


def test_session_trades_cap_when_configured(config, monkeypatch):
    from core import trade_limits
    from core.trade_limits import session_trade_capacity_available

    monkeypatch.setattr(trade_limits, "session_start_ts", lambda: None)
    config["trading"]["max_session_trades_per_symbol"] = 5
    trades = [{"symbol": "XAUUSDm", "pnl": 1.0, "closed_at": "2026-06-25T12:00:00+00:00"} for _ in range(5)]
    assert session_trade_capacity_available(config, "XAUUSDm", trades) is False


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
        current_price=103.0,  # +3.0 > trigger_points 280 × 0.01
        atr=2.0,
        mgmt_row={},
        point=0.01,
    )
    assert new_sl is not None
    assert new_sl > 95.0
    assert "break_even" in actions


def test_break_even_triggers_at_five_dollars_profit(config):
    config["trading"]["break_even"]["trigger_profit_usd"] = 5
    config["trading"]["break_even"]["lock_profit_usd"] = 0
    config["trading"]["trailing"]["activation_profit_usd"] = 5
    pos = {"symbol": "US500m", "side": "BUY", "entry": 7500.0, "sl": 7480.0, "profit": 5.2, "size": 0.05}
    new_sl, row, actions = compute_managed_sl(
        config,
        pos,
        current_price=7502.0,
        atr=10.0,
        mgmt_row={},
    )
    assert new_sl == 7500.0
    assert "break_even" in actions
    assert "trail" in actions


def test_per_symbol_fx_trail_needs_three_dollars(config):
    config["trading"]["break_even"]["per_symbol"]["EURUSDm"]["trigger_profit_usd"] = 3
    config["trading"]["trailing"]["per_symbol"]["EURUSDm"]["activation_profit_usd"] = 3
    # +2 pips (20 broker pts) — below 27-pt activation; USD $2.50 also below $3.
    pos = {"symbol": "EURUSDm", "side": "BUY", "entry": 1.08000, "sl": 1.07800, "profit": 2.5, "size": 0.1}
    _sl, _row, actions_low = compute_managed_sl(config, pos, 1.08020, 0.001, {}, point=1e-5)
    assert "trail" not in actions_low
    pos["profit"] = 3.1
    _sl, _row, actions_hi = compute_managed_sl(config, pos, 1.0820, 0.001, {}, point=1e-5)
    assert "break_even" in actions_hi
    assert "trail" in actions_hi


def test_trail_distance_uses_broker_points_not_atr_mult(config):
    trail_cfg = config["trading"]["trailing"]
    trail_sym = trail_cfg["per_symbol"]["US500m"]
    dist = _trail_distance_price(trail_sym, trail_cfg, atr=16.0, point=0.1)
    assert dist == pytest.approx(25.8)
    fx_sym = trail_cfg["per_symbol"]["EURUSDm"]
    fx_dist = _trail_distance_price(fx_sym, trail_cfg, atr=0.0005, point=1e-5)
    assert fx_dist == pytest.approx(0.002)


def test_trail_activates_on_points_when_usd_profit_low(config):
    """MT5 profit USD can lag price — points activation must still arm trail."""
    pos = {
        "symbol": "EURUSDm",
        "side": "BUY",
        "entry": 1.0800,
        "sl": 1.0780,
        "profit": 1.5,
        "size": 0.1,
    }
    # +20 pips favourable but only $1.50 floating profit (< $3 USD gate)
    new_sl, row, actions = compute_managed_sl(
        config, pos, current_price=1.0820, atr=0.001, mgmt_row={}, point=1e-5,
    )
    assert "trail" in actions
    assert row.get("trailing") is True
    assert new_sl is not None


def test_trail_stays_armed_after_profit_pullback(config):
    """Once trailing is latched, keep ratcheting peak/SL even if USD profit dips."""
    pos = {
        "symbol": "US500m",
        "side": "BUY",
        "entry": 7500.0,
        "sl": 7490.0,
        "profit": 3.0,
    }
    row = {"trailing": True, "peak_price": 7530.0}
    new_sl, row, actions = compute_managed_sl(
        config, pos, current_price=7510.0, atr=10.0, mgmt_row=row, point=0.1,
    )
    assert "trail" in actions
    assert row["peak_price"] == 7530.0
    assert new_sl == pytest.approx(7504.2)


def test_xau_trail_points_atr_mult_uses_broker_points(config):
    trail_cfg = config["trading"]["trailing"]
    trail_sym = trail_cfg["per_symbol"]["XAUUSDm"]
    # ATR=3.50, point=0.01 -> 350 broker pts × 2.0 mult = 700 pts = $7.00
    dist = _trail_distance_price(trail_sym, trail_cfg, atr=3.5, point=0.01)
    assert dist == pytest.approx(7.0)


def test_btc_trail_atr_mult(config):
    trail_cfg = config["trading"]["trailing"]
    trail_sym = trail_cfg["per_symbol"]["BTCUSDm"]
    dist = _trail_distance_price(trail_sym, trail_cfg, atr=150.0, point=0.01)
    # 150/0.01 * 0.45 = 6750 × 0.01 = 67.5
    assert dist == pytest.approx(67.5)


def test_trail_sl_ratcheted_from_peak(config):
    pos = {
        "symbol": "US500m",
        "side": "BUY",
        "entry": 7500.0,
        "sl": 7480.0,
        "profit": 6.0,
    }
    row = {"peak_price": 7530.0}
    new_sl, row, actions = compute_managed_sl(
        config, pos, current_price=7502.0, atr=10.0, mgmt_row=row, point=0.1,
    )
    assert "trail" in actions
    assert new_sl == pytest.approx(7504.2)  # peak 7530 - 25.8 trail_points (258 pts)


def test_nas100_trail_uses_atr_broker_points(config):
    """NAS100: trail = round(ATR/point × 0.45) × point, not a fixed 3250 pts."""
    trail_cfg = config["trading"]["trailing"]
    trail_sym = trail_cfg["per_symbol"]["NAS100m"]
    dist = _trail_distance_price(trail_sym, trail_cfg, atr=60.0, point=0.01)
    # 60/0.01 * 0.45 = 2700 broker pts × 0.01 = 27.0 index pts
    assert dist == pytest.approx(27.0)


def test_nas100_trail_sl_behind_peak_sell(config):
    pos = {
        "symbol": "NAS100m",
        "side": "SELL",
        "entry": 29380.0,
        "sl": 30100.0,
        "profit": 12.0,
    }
    row = {"peak_price": 29200.0}
    new_sl, row, actions = compute_managed_sl(
        config, pos, current_price=29210.0, atr=60.0, mgmt_row=row, point=0.01,
    )
    assert "trail" in actions
    assert new_sl == pytest.approx(29227.0)  # peak 29200 + 27 ATR trail


def test_clamp_sl_respects_mt5_stops_level():
    # NAS100 stops_level=150, point=0.01 -> min 1.5 from reference
    sl = _clamp_sl_to_stops_level(
        "SELL", 29201.0, reference=29200.0, point=0.01, stops_level=150, digits=2,
    )
    assert sl >= 29201.5
    sl_buy = _clamp_sl_to_stops_level(
        "BUY", 29199.0, reference=29200.0, point=0.01, stops_level=150, digits=2,
    )
    assert sl_buy <= 29198.5


def test_btc_trail_needs_seven_dollars(config):
    config["trading"]["break_even"]["per_symbol"]["BTCUSDm"]["trigger_profit_usd"] = 7
    config["trading"]["trailing"]["per_symbol"]["BTCUSDm"]["activation_profit_usd"] = 7
    config["trading"]["trailing"]["per_symbol"]["BTCUSDm"]["activation_atr_mult"] = 99
    config["trading"]["break_even"]["per_symbol"]["BTCUSDm"]["trigger_atr_mult"] = 99
    pos = {"symbol": "BTCUSDm", "side": "SELL", "entry": 60000.0, "sl": 61000.0, "profit": 6.0, "size": 0.01}
    _sl, _row, actions = compute_managed_sl(config, pos, 59900.0, 200.0, {})
    assert "trail" not in actions
    pos["profit"] = 7.5
    _sl, _row, actions = compute_managed_sl(config, pos, 59800.0, 200.0, {})
    assert "trail" in actions


def test_pyramiding_allows_different_signals_same_side(config):
    config["execution"]["mode"] = "mt5"
    config["trading"]["allow_pyramiding"] = True
    config["trading"]["pyramid_block_same_setup"] = False
    config["trading"]["max_open_per_symbol"] = 5
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


def test_pyramiding_blocks_same_setup_when_configured(config):
    config["execution"]["mode"] = "mt5"
    config["trading"]["allow_pyramiding"] = True
    config["trading"]["pyramid_block_same_setup"] = True
    config["trading"]["max_open_per_symbol"] = 5
    config["risk"]["unlimited_trades"] = False

    existing = [{
        "symbol": "XAUUSDm",
        "side": "SELL",
        "setup_type": "pullback",
        "signal_id": "sig-a",
        "ticket": 1001,
    }]
    repeat_setup = {
        "signal_id": "sig-b",
        "symbol": "XAUUSDm",
        "side": "SELL",
        "setup_type": "pullback",
        "entry": 2650.0,
        "sl": 2660.0,
        "tp1": 2635.0,
        "confidence": 80,
    }
    different_setup = {**repeat_setup, "signal_id": "sig-c", "setup_type": "trend_continuation"}

    assert is_duplicate_position(config, repeat_setup, existing) is True
    assert is_duplicate_position(config, different_setup, existing) is False


def test_max_three_open_positions_per_symbol(config):
    config["trading"]["max_open_per_symbol"] = 3
    positions = [
        {"symbol": "XAUUSDm", "side": "SELL", "setup_type": "pullback"},
        {"symbol": "XAUUSDm", "side": "SELL", "setup_type": "trend_continuation"},
        {"symbol": "XAUUSDm", "side": "BUY", "setup_type": "pullback"},
    ]
    assert symbol_capacity_available(config, "XAUUSDm", positions) is False
    assert symbol_capacity_available(config, "XAUUSDm", positions[:2]) is True


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