"""Regression tests for PaperBroker intrabar exit resolution.

Locks in two safety-critical properties of ``core/paper_broker.py::_check_exits``:

1. **Stop-first intrabar resolution**: when a bar's adverse extreme touches the
   pre-step SL, the exit is resolved at the SL (the honest lower bound) — never
   at the TP, even when the favourable extreme also reaches the TP. The broker
   cannot know the intra-bar order of high/low, so it resolves pessimistically.
   This is the fix for the close-to-close exit-model bias (memory
   exit-model-bias-found-2026-06-27).

2. **Entry-bar (``just_opened``) guard**: a position opened on the current step
   fills at the bar close, so that same bar's intrabar high/low are FUTURE
   relative to the fill — using them would be look-ahead. The guard forces
   close-to-close resolution for the opening step and defers any intrabar
   SL/TP breach to a later step.

Both paths are where a future refactor could silently reintroduce a look-ahead
leak; these tests pin the current (fixed) behaviour.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.paper_broker import PaperBroker
from core.utils import load_config


@pytest.fixture
def broker_cfg():
    """Paper-mode config with BE/trailing OFF to isolate SL/TP resolution."""
    cfg = copy.deepcopy(load_config())
    cfg["execution"]["mode"] = "paper"
    # Disable dynamic exits so _check_exits is the only exit path under test.
    cfg["trading"]["break_even"]["enabled"] = False
    cfg["trading"]["trailing"]["enabled"] = False
    return cfg


def _long_position(entry=100.0, sl=95.0, tp=110.0, *, just_opened=False):
    return {
        "position_id": "pos-1",
        "signal_id": "sig-1",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "entry": entry,
        "sl": sl,
        "tp1": tp,
        "size": 1.0,
        "atr": abs(entry - sl) / 1.5,
        "be_triggered": False,
        "trail_active": False,
        "peak": entry,
        "just_opened": just_opened,
    }


def _short_position(entry=100.0, sl=105.0, tp=90.0, *, just_opened=False):
    return {
        "position_id": "pos-2",
        "signal_id": "sig-2",
        "symbol": "XAUUSDm",
        "side": "SELL",
        "entry": entry,
        "sl": sl,
        "tp1": tp,
        "size": 1.0,
        "atr": abs(entry - sl) / 1.5,
        "be_triggered": False,
        "trail_active": False,
        "peak": entry,
        "just_opened": just_opened,
    }


# --------------------------------------------------------------------------- #
# 1. Stop-first intrabar resolution.                                          #
# --------------------------------------------------------------------------- #
def test_long_both_touched_exits_at_stop(broker_cfg):
    """Long: bar low touches SL AND high touches TP -> exit at SL (stop-first).

    The broker cannot know whether low or high printed first, so the honest
    lower bound is to assume the adverse extreme (SL) hit first. The TP is
    NOT credited even though it was also reached.
    """
    broker = PaperBroker(broker_cfg)
    pos = _long_position(entry=100.0, sl=95.0, tp=110.0)
    # close sits between SL and TP; high reaches TP, low reaches SL.
    prices = {"XAUUSDm": 100.0}
    bar_highlow = {"XAUUSDm": (111.0, 94.0)}  # (high, low)

    closed, trades, realized = broker._check_exits([pos], prices, bar_highlow)

    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "stop_loss"
    assert closed[0]["exit_price"] == pytest.approx(95.0)
    # Loss = (95 - 100) * 1.0 = -5
    assert closed[0]["pnl"] == pytest.approx(-5.0)
    assert realized == pytest.approx(-5.0)


def test_long_only_tp_touched_exits_at_tp(broker_cfg):
    """Long: high reaches TP but low stays above SL -> exit at TP.

    This is the symmetric (TP-only) case: when the adverse extreme never
    touched the SL, the stop-first rule does not fire and the TP is credited.
    """
    broker = PaperBroker(broker_cfg)
    pos = _long_position(entry=100.0, sl=95.0, tp=110.0)
    prices = {"XAUUSDm": 108.0}
    bar_highlow = {"XAUUSDm": (111.0, 98.0)}  # low 98 > SL 95 -> SL not touched

    closed, trades, realized = broker._check_exits([pos], prices, bar_highlow)

    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "take_profit"
    assert closed[0]["exit_price"] == pytest.approx(110.0)
    assert closed[0]["pnl"] == pytest.approx(10.0)


def test_short_both_touched_exits_at_stop(broker_cfg):
    """Short: high touches SL AND low touches TP -> exit at SL (stop-first)."""
    broker = PaperBroker(broker_cfg)
    pos = _short_position(entry=100.0, sl=105.0, tp=90.0)
    prices = {"XAUUSDm": 100.0}
    # For a short, adverse = high (toward SL=105), favourable = low (toward TP=90).
    bar_highlow = {"XAUUSDm": (106.0, 89.0)}  # both touched

    closed, trades, realized = broker._check_exits([pos], prices, bar_highlow)

    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "stop_loss"
    assert closed[0]["exit_price"] == pytest.approx(105.0)
    # Short PnL = -(exit - entry) = -(105 - 100) = -5
    assert closed[0]["pnl"] == pytest.approx(-5.0)


def test_short_only_tp_touched_exits_at_tp(broker_cfg):
    """Short: low reaches TP but high stays below SL -> exit at TP."""
    broker = PaperBroker(broker_cfg)
    pos = _short_position(entry=100.0, sl=105.0, tp=90.0)
    prices = {"XAUUSDm": 92.0}
    bar_highlow = {"XAUUSDm": (102.0, 89.0)}  # high 102 < SL 105 -> SL not touched

    closed, trades, realized = broker._check_exits([pos], prices, bar_highlow)

    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "take_profit"
    assert closed[0]["exit_price"] == pytest.approx(90.0)
    assert closed[0]["pnl"] == pytest.approx(10.0)


def test_neither_touched_no_exit(broker_cfg):
    """Long: bar stays inside (SL, TP) -> position remains open."""
    broker = PaperBroker(broker_cfg)
    pos = _long_position(entry=100.0, sl=95.0, tp=110.0)
    prices = {"XAUUSDm": 102.0}
    bar_highlow = {"XAUUSDm": (104.0, 98.0)}  # inside the range

    closed, trades, realized = broker._check_exits([pos], prices, bar_highlow)

    assert closed == []
    assert realized == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# 2. just_opened entry-bar guard (look-ahead prevention).                    #
# --------------------------------------------------------------------------- #
def test_just_opened_blocks_intrabar_exit_on_entry_bar(broker_cfg):
    """A position opened this step (just_opened=True) must NOT be exited on
    its entry bar's own high/low even if both SL and TP are touched — that
    range is future relative to a close-time fill (look-ahead)."""
    broker = PaperBroker(broker_cfg)
    pos = _long_position(entry=100.0, sl=95.0, tp=110.0, just_opened=True)
    prices = {"XAUUSDm": 100.0}  # close == entry
    bar_highlow = {"XAUUSDm": (111.0, 94.0)}  # both SL and TP touched intrabar

    closed, trades, realized = broker._check_exits([pos], prices, bar_highlow)

    assert closed == [], "entry bar must not trigger an intrabar SL/TP exit"
    assert realized == pytest.approx(0.0)
    # The guard is one-shot: it must be cleared after the first pass so the
    # next step uses the real intrabar extremes.
    assert pos["just_opened"] is False


def test_just_opened_exits_on_next_bar(broker_cfg):
    """After the entry-bar guard clears, a subsequent bar touching SL exits
    normally — the guard only defers, it does not suppress forever."""
    broker = PaperBroker(broker_cfg)

    # Step 1: entry bar — guard active, no exit even with extreme bar.
    pos = _long_position(entry=100.0, sl=95.0, tp=110.0, just_opened=True)
    prices = {"XAUUSDm": 100.0}
    bar_highlow = {"XAUUSDm": (111.0, 94.0)}
    closed, _, _ = broker._check_exits([pos], prices, bar_highlow)
    assert closed == []
    assert pos["just_opened"] is False

    # Step 2: next bar — guard cleared, SL touched -> stop-first exit fires.
    prices = {"XAUUSDm": 100.0}
    bar_highlow = {"XAUUSDm": (101.0, 94.0)}  # low 94 <= SL 95
    closed, trades, realized = broker._check_exits([pos], prices, bar_highlow)

    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "stop_loss"
    assert closed[0]["exit_price"] == pytest.approx(95.0)
    assert closed[0]["pnl"] == pytest.approx(-5.0)


def test_just_opened_close_itself_can_still_exit(broker_cfg):
    """If the close itself breaches SL/TP on the entry bar, that IS allowed
    (close-to-close resolution is not look-ahead). The guard only suppresses
    the intrabar high/low, not a close that genuinely crossed the level."""
    broker = PaperBroker(broker_cfg)
    # Close below SL -> stop_loss even on the entry bar (close-to-close path).
    pos = _long_position(entry=100.0, sl=95.0, tp=110.0, just_opened=True)
    prices = {"XAUUSDm": 93.0}  # close < SL
    bar_highlow = {"XAUUSDm": (111.0, 94.0)}  # would also be intrabar stop

    closed, trades, realized = broker._check_exits([pos], prices, bar_highlow)

    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "stop_loss"
    # just_opened path uses adv=close=93 <= SL 95 -> stop at SL 95
    assert closed[0]["exit_price"] == pytest.approx(95.0)