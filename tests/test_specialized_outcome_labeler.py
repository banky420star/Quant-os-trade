"""Tests for the specialized-setup outcome labeler (iteration 10, 2026-08-01)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.specialized_outcome_labeler import label_outcome, label_fires


def _bar(high, low, close, time="t"):
    return {"time": time, "high": high, "low": low, "close": close}


def _buy_fire(entry=100.0, support=99.0):
    return {"side": "BUY", "entry": entry, "feat": {"support": support}, "tp_r": 2.0,
            "symbol": "XAUUSDm", "setup_type": "silver_bullet", "utc_hour": 14,
            "confidence": 0.7}


def _sell_fire(entry=100.0, resistance=101.0):
    return {"side": "SELL", "entry": entry, "feat": {"resistance": resistance}, "tp_r": 2.0,
            "symbol": "XAUUSDm", "setup_type": "silver_bullet", "utc_hour": 14,
            "confidence": 0.7}


# --- invalid-risk skips ---

def test_buy_support_not_below_entry_is_skipped():
    # support 100 == entry -> risk 0 -> invalid -> None (not counted)
    assert label_outcome(_buy_fire(entry=100.0, support=100.0), [_bar(102, 99, 101)]) is None


def test_sell_resistance_not_above_entry_is_skipped():
    assert label_outcome(_sell_fire(entry=100.0, resistance=100.0), [_bar(101, 98, 99)]) is None


def test_missing_side_is_skipped():
    fire = {"entry": 100.0, "feat": {"support": 99.0}}
    assert label_outcome(fire, [_bar(102, 99, 101)]) is None


def test_no_forward_bars_is_skipped():
    assert label_outcome(_buy_fire(), []) is None


# --- BUY outcomes (entry 100, support 99 -> risk 1, sl 99, tp 102) ---

def test_buy_tp_hit():
    # bar1 doesn't touch sl/tp, bar2 hits tp
    bars = [_bar(101, 99.5, 100.5), _bar(102.5, 100.5, 102)]
    r = label_outcome(_buy_fire(), bars)
    assert r["r_multiple"] == 2.0
    assert r["exit_reason"] == "tp"
    assert r["bars_held"] == 2


def test_buy_sl_hit():
    # bar1 low 98.5 <= sl 99 -> immediate SL
    bars = [_bar(100.5, 98.5, 99)]
    r = label_outcome(_buy_fire(), bars)
    assert r["r_multiple"] == -1.0
    assert r["exit_reason"] == "sl"
    assert r["bars_held"] == 1


def test_buy_same_bar_sl_first_is_pessimistic():
    # same bar hits BOTH sl (low 98) and tp (high 102.5) -> SL first (honest bias)
    bars = [_bar(102.5, 98.0, 101)]
    r = label_outcome(_buy_fire(), bars)
    assert r["r_multiple"] == -1.0
    assert r["exit_reason"] == "sl"


def test_buy_time_stop_uses_last_close():
    # never hit sl 99 or tp 102 over 3 bars; last close 101 -> R = (101-100)/1 = +1
    bars = [_bar(101, 99.5, 100.5), _bar(101, 99.5, 100.7), _bar(101, 99.5, 101)]
    r = label_outcome(_buy_fire(), bars, max_bars=3)
    assert r["exit_reason"] == "time_stop"
    assert r["r_multiple"] == 1.0
    assert r["bars_held"] == 3


# --- SELL outcomes (entry 100, resistance 101 -> risk 1, sl 101, tp 98) ---

def test_sell_tp_hit():
    # bar1 low 97.5 <= tp 98 -> TP at +2R
    bars = [_bar(100.5, 97.5, 98)]
    r = label_outcome(_sell_fire(), bars)
    assert r["r_multiple"] == 2.0
    assert r["exit_reason"] == "tp"


def test_sell_sl_hit():
    # bar1 high 101.5 >= sl 101 -> SL
    bars = [_bar(101.5, 99, 100)]
    r = label_outcome(_sell_fire(), bars)
    assert r["r_multiple"] == -1.0
    assert r["exit_reason"] == "sl"


def test_sell_same_bar_sl_first_is_pessimistic():
    # same bar high 102 (sl) and low 97 (tp) -> SL first
    bars = [_bar(102.0, 97.0, 99)]
    r = label_outcome(_sell_fire(), bars)
    assert r["r_multiple"] == -1.0
    assert r["exit_reason"] == "sl"


def test_sell_time_stop_uses_last_close():
    # never hit sl 101 or tp 98; last close 99 -> R = (100-99)/1 = +1
    bars = [_bar(100.5, 99, 99.5), _bar(100.5, 99, 99.2), _bar(100.5, 99, 99)]
    r = label_outcome(_sell_fire(), bars, max_bars=3)
    assert r["exit_reason"] == "time_stop"
    assert r["r_multiple"] == 1.0


# --- max_bars cap + tp_r override ---

def test_max_bars_caps_forward_window():
    # tp would hit at bar 5, but max_bars=2 -> time-stop at bar 2 close 100.5 -> R +0.5
    bars = [
        _bar(101, 99.5, 100.2),
        _bar(101, 99.5, 100.5),
        _bar(101, 99.5, 100.8),
        _bar(101, 99.5, 101.2),
        _bar(102.5, 101, 102),  # tp here but beyond cap
    ]
    r = label_outcome(_buy_fire(), bars, max_bars=2)
    assert r["exit_reason"] == "time_stop"
    assert r["r_multiple"] == 0.5
    assert r["bars_held"] == 2


def test_tp_r_override_changes_tp_level():
    # fire's own tp_r=2.0 -> tp 102, but we pass tp_r=3.0 -> tp 103.
    # bar1 high 102.5 < tp 103 and low 99.5 > sl 99 -> no hit.
    # bar2 high 102.9 < tp 103, low 101 > sl 99 -> no hit -> time-stop at close 102.8 -> R +2.8
    bars = [_bar(102.5, 99.5, 101), _bar(102.9, 101, 102.8)]
    r = label_outcome(_buy_fire(), bars, max_bars=2, tp_r=3.0)
    assert r["exit_reason"] == "time_stop"
    assert r["r_multiple"] == 2.8
    assert r["tp"] == 103.0  # tp_r arg overrode the fire's 2.0


# --- label_fires batch ---

def test_label_fires_merges_and_skips_invalid():
    fires = [
        _buy_fire(entry=100.0, support=99.0),       # valid
        _buy_fire(entry=100.0, support=100.0),      # invalid risk -> skipped
        _sell_fire(entry=100.0, resistance=101.0),  # valid
    ]
    # provide per-symbol bars; buy hits tp, sell hits sl
    bars = {
        "XAUUSDm": [_bar(102.5, 100.5, 102)],  # buy tp @102 (high 102.5>=102); sell sl @101 (high 102.5>=101)
    }
    out = label_fires(fires, bars)
    assert len(out) == 2  # invalid one skipped
    # both see the same bar; buy first bar: low 100.5 > sl 99, high 102.5 >= tp 102 -> tp +2
    # sell first bar: high 102.5 >= sl 101 -> sl -1 (sl checked first for sell)
    reasons = {r["exit_reason"] for r in out}
    assert "tp" in reasons
    assert "sl" in reasons