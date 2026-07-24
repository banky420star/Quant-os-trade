"""Exit manager — partial TP, deferred trail, profit lock."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.exit_manager import (
    partial_close_volume,
    post_partial_sl,
    profit_rr,
    tp1_reached,
    trail_activation_allowed,
    trail_distance_multiplier,
)
from core.position_manager import compute_managed_sl, _exit_trigger_met


@pytest.fixture
def config():
    from core.utils import load_config
    return load_config()


def test_partial_close_volume_respects_min_remain():
    assert partial_close_volume(0.04, 0.5) == 0.02
    assert partial_close_volume(0.01, 0.5) == 0.0


def test_tp1_reached_buy_sell():
    assert tp1_reached("BUY", 101.0, 100.5)
    assert not tp1_reached("BUY", 100.0, 100.5)
    assert tp1_reached("SELL", 99.0, 99.5)
    assert not tp1_reached("SELL", 100.0, 99.5)


def test_trail_deferred_until_partial_or_rr(config):
    defer_cfg = config["trading"]["exits"]["defer_trail_until"]
    trail_cfg = config["trading"]["trailing"]
    trail_sym = trail_cfg["per_symbol"]["XAUUSDm"]
    row = {"trailing": False, "partial_tp_done": False}
    assert not trail_activation_allowed(
        row,
        profit_rr_value=0.2,
        profit_usd=5.0,
        profit_dist=1.0,
        trail_sym=trail_sym,
        trail_cfg=trail_cfg,
        atr=2.0,
        point=0.01,
        config=config,
        exit_trigger_met=_exit_trigger_met,
    )
    assert trail_activation_allowed(
        row,
        profit_rr_value=float(defer_cfg["min_rr"]),
        profit_usd=2.0,
        profit_dist=0.5,
        trail_sym=trail_sym,
        trail_cfg=trail_cfg,
        atr=2.0,
        point=0.01,
        config=config,
        exit_trigger_met=_exit_trigger_met,
    )
    row["partial_tp_done"] = True
    assert trail_activation_allowed(
        row,
        profit_rr_value=0.1,
        profit_usd=1.0,
        profit_dist=0.1,
        trail_sym=trail_sym,
        trail_cfg=trail_cfg,
        atr=2.0,
        point=0.01,
        config=config,
        exit_trigger_met=_exit_trigger_met,
    )


def test_trail_tightens_after_partial(config):
    row = {"partial_tp_done": True}
    mult = trail_distance_multiplier(row, config)
    assert mult == pytest.approx(config["trading"]["exits"]["runner"]["trail_tighten_mult"])


def test_be_locks_profit_not_breakeven(config):
    cfg = copy.deepcopy(config)
    pos = {
        "symbol": "XAUUSDm",
        "side": "BUY",
        "entry": 2400.0,
        "sl": 2390.0,
        "profit": 4.0,
    }
    new_sl, row, actions = compute_managed_sl(cfg, pos, 2405.0, atr=5.0, mgmt_row={}, point=0.01)
    assert new_sl is not None
    assert "break_even" in actions
    assert new_sl > pos["entry"]


def test_post_partial_sl_locks_above_entry(config):
    # The runner locks the post-partial stop at lock_profit_rr of the initial
    # risk above entry. Derive the expected R from config rather than pinning a
    # literal, so tuning lock_profit_rr doesn't make this stale.
    from core.exit_manager import runner_cfg
    expected_rr = float(runner_cfg(config).get("lock_profit_rr", 0.35))
    sl = post_partial_sl("BUY", 100.0, 98.0, atr=1.0, config=config)
    assert sl > 100.0
    assert profit_rr("BUY", 100.0, 98.0, sl) == pytest.approx(expected_rr, rel=0.05)