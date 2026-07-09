"""Strategy-pinned entry price tests."""

from __future__ import annotations

import pytest

from core.strategy_entry import pin_strategy_entry, resolve_entry_mode


@pytest.fixture
def config():
    return {
        "trading": {
            "strategy_entries": {
                "enabled": True,
                "market_if_within_atr": 0.15,
                "max_entry_wait_atr": 2.0,
                "entry_buffer_atr": 0.05,
            }
        }
    }


def test_pullback_buy_pins_near_support(config):
    feat = {
        "price": 4040.0,
        "atr": 10.0,
        "support": 4020.0,
        "resistance": 4055.0,
        "bb_middle": 4030.0,
        "bb_lower": 4010.0,
        "bb_upper": 4050.0,
        "breakout": "breakout_retest",
    }
    ctx = {"move_type": "pullback", "trend_strength": "strong"}

    levels = pin_strategy_entry("pullback", "BUY", feat, ctx, config)

    assert levels["entry"] < feat["price"]
    assert levels["entry"] >= feat["support"]
    assert levels["sl"] < levels["entry"] < levels["tp1"]
    assert levels["entry_anchor"] == "support_retest"
    assert levels["entry_mode"] in ("limit", "market")


def test_limit_mode_when_entry_away_from_market(config):
    assert resolve_entry_mode(4020.0, 4040.0, 10.0, config) == "limit"
    assert resolve_entry_mode(4039.0, 4040.0, 10.0, config) == "market"


def test_use_limit_orders_false_forces_market(config):
    config["trading"]["strategy_entries"]["use_limit_orders"] = False
    assert resolve_entry_mode(4020.0, 4040.0, 10.0, config) == "market"

def test_market_snap_recalculates_sl_tp_for_valid_rr(config):
    """When entry snaps to market, SL/TP must be recomputed — not left at anchor levels."""
    feat = {
        "price": 69.98,
        "atr": 0.45,
        "support": 68.69,
        "resistance": 70.20,
        "bb_middle": 69.28,
        "bb_lower": 68.50,
        "bb_upper": 70.10,
        "breakout": "none",
    }
    ctx = {"move_type": "pullback", "trend_strength": "strong"}
    config["trading"]["strategy_entries"]["market_if_within_atr"] = 99.0

    levels = pin_strategy_entry("pullback", "BUY", feat, ctx, config)

    assert levels["entry"] == feat["price"]
    risk = levels["entry"] - levels["sl"]
    reward = levels["tp1"] - levels["entry"]
    assert risk > 0
    assert reward / risk >= 1.4


def test_trend_continuation_sell_pins_above_price(config):
    feat = {
        "price": 4040.0,
        "atr": 10.0,
        "support": 4020.0,
        "resistance": 4060.0,
        "bb_middle": 4045.0,
        "bb_lower": 4020.0,
        "bb_upper": 4065.0,
        "breakout": "none",
    }
    ctx = {"move_type": "continuation"}

    levels = pin_strategy_entry("trend_continuation", "SELL", feat, ctx, config)

    assert levels["entry"] >= feat["price"]
    assert levels["tp1"] < levels["entry"] < levels["sl"]