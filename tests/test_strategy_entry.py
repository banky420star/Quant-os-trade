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