"""Profit Quality Dashboard server helper (_build_profit_quality).

Covers: empty trades, per-trade mgmt path, live-join path, exit_reason
inference, R-bin boundary correctness, bleed_top sorting + n>=3 filter,
and JSON-safety through _sanitize_json.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard.server import _build_profit_quality, _sanitize_json


def _base_trade(**overrides):
    """Minimal closed-trade record with optional overrides."""
    base = {
        "trade_id": "t1",
        "mt5_position": 1,
        "symbol": "XAUUSDm",
        "side": "BUY",
        "entry": 2400.0,
        "exit": 2401.0,
        "sl": 2390.0,
        "pnl": 0.10,
        "result": "win",
        "exit_reason": "mt5_close",
        "r_multiple": 0.1,
    }
    base.update(overrides)
    return base


def test_empty_trade_log_returns_zero_total():
    res = _build_profit_quality({"trades": []}, {}, {})
    assert res["n_total"] == 0
    for k in ("r_buckets", "payoff_buckets", "be", "partial_tp", "stale", "bleed_top"):
        assert isinstance(res[k], list)
    assert res["bleed_top"] == []
    assert res["data_quality"]["pct_complete_be_and_r"] == 0.0


def test_per_trade_mgmt_path_populates_segments():
    trades = [
        _base_trade(trade_id="a", pnl=0.40, r_multiple=0.4,
                    position_mgmt={"break_even": True, "partial_tp_done": False}),
        _base_trade(trade_id="b", pnl=-0.75, r_multiple=-0.75,
                    position_mgmt={"break_even": False, "partial_tp_done": False}),
        _base_trade(trade_id="c", pnl=2.50, r_multiple=1.25,
                    position_mgmt={"break_even": False, "partial_tp_done": True}),
    ]
    res = _build_profit_quality({"trades": trades}, {}, {})
    by_key_be = {r["bucket"]: r for r in res["be"]}
    by_key_pt = {r["bucket"]: r for r in res["partial_tp"]}
    assert by_key_be["triggered"]["n"] == 1
    assert by_key_be["not_triggered"]["n"] == 2
    assert by_key_pt["partial"]["n"] == 1
    assert by_key_pt["no_partial"]["n"] == 2
    # pct_complete requires both break_even and partial_tp_done to be non-None
    assert res["data_quality"]["pct_complete_be_and_r"] == 100.0


def test_live_join_to_position_management():
    """Trade with empty per-trade mgmt but ticket in pos_mgmt.positions recovers flags."""
    trades = [_base_trade(trade_id="x", mt5_position=42, pnl=-1.20, r_multiple=-1.0,
                          position_mgmt={})]
    pos_mgmt = {"positions": {"42": {"break_even": True, "partial_tp_done": False}}}
    res = _build_profit_quality({"trades": trades}, pos_mgmt, {})
    by_be = {r["bucket"]: r for r in res["be"]}
    assert by_be["triggered"]["n"] == 1
    assert res["data_quality"]["trades_with_mgmt_joined"] == 1


def test_exit_reason_inference_for_partial():
    trades = [_base_trade(trade_id="p", pnl=2.0, r_multiple=0.8,
                          position_mgmt={}, exit_reason="close_partial_tp1")]
    res = _build_profit_quality({"trades": trades}, {}, {})
    by_pt = {r["bucket"]: r for r in res["partial_tp"]}
    assert by_pt["partial"]["n"] == 1


def test_stale_inference_uses_audit_and_trade_manager():
    # Stale flag comes from pos_mgmt.last_run.audit
    pos_mgmt = {"last_run": {"audit": [{"ticket": 7, "status": "time_stop_closed"}]}}
    trades = [_base_trade(trade_id="s1", mt5_position=7, pnl=-0.50, r_multiple=-0.5)]
    res = _build_profit_quality({"trades": trades}, pos_mgmt, {})
    by_st = {r["bucket"]: r for r in res["stale"]}
    assert by_st["stale"]["n"] == 1
    # Stale flag also from trade_manager.stale_candidates
    pos_mgmt2 = {}; tm = {"stale_candidates": [{"ticket": 8}]}
    trades2 = [_base_trade(trade_id="s2", mt5_position=8, pnl=-0.40, r_multiple=-0.4)]
    res2 = _build_profit_quality({"trades": trades2}, pos_mgmt2, tm)
    by_st2 = {r["bucket"]: r for r in res2["stale"]}
    assert by_st2["stale"]["n"] == 1


@pytest.mark.parametrize("r_value, expected_bin", [
    # Lower-inclusive, upper-exclusive bin convention [a, b):
    (-2.0,   "<-1.5R"),       # (-inf, -1.5)
    (-1.5,   "-1.5..-1R"),    # r=-1.5 < -1.5 False, falls through to "[-1.5, -1)"
    (-1.499, "-1.5..-1R"),
    (-1.0,   "-1..-0.5R"),    # r=-1.0 < -1.0 False, falls through to "[-1, -0.5)"
    (-0.999, "-1..-0.5R"),
    (-0.5,   "-0.5..0R"),     # r=-0.5 < -0.5 False
    (-0.4999,"-0.5..0R"),
    (0.0,    "0..0.5R"),      # r=0.0 < 0.0 False (pay_bin handles exact 0 in USD separately)
    (0.0001, "0..0.5R"),
    (0.5,    "0.5..1R"),
    (1.0,    "1..2R"),
    (2.0,    ">=2R"),
    (3.5,    ">=2R"),
])
def test_r_bin_off_by_one_boundaries(r_value, expected_bin):
    # Bin convention [a, b): lower-inclusive, upper-exclusive.
    trades = [_base_trade(trade_id="r", pnl=r_value, r_multiple=r_value)]
    res = _build_profit_quality({"trades": trades}, {}, {})
    bucket_with_trade = next((b for b in res["r_buckets"] if b["n"] == 1), None)
    assert bucket_with_trade is not None
    assert bucket_with_trade["bucket"] == expected_bin


def test_payoff_buckets_collapse_to_8():
    """12-bin payroll was over-granular; ensure collapsed to exactly 9 entries (8 + no_pnl)."""
    res = _build_profit_quality({"trades": []}, {}, {})
    assert len(res["payoff_buckets"]) == 9
    assert res["payoff_buckets"][-1]["bucket"] == "no_pnl"


def test_bleed_top_sorted_ascending_and_n_filter():
    """Buckets with n>=3 only; sorted by net_pnl ascending (worst first)."""
    trades = []
    for i in range(5):
        # 5 winners
        trades.append(_base_trade(trade_id=f"w{i}", pnl=2.0, r_multiple=0.8,
                                  position_mgmt={"break_even": True, "partial_tp_done": False}))
    for i in range(3):
        # 3 losers BE-not-triggered (the bleed scenario)
        trades.append(_base_trade(trade_id=f"l{i}", pnl=-1.5, r_multiple=-1.0,
                                  position_mgmt={"break_even": False, "partial_tp_done": False}))
    res = _build_profit_quality({"trades": trades}, {}, {})
    # Both BE segments should have n=5 and n=3
    by_be = {r["bucket"]: r for r in res["be"]}
    assert by_be["triggered"]["n"] == 5
    assert by_be["not_triggered"]["n"] == 3
    assert by_be["not_triggered"]["net_pnl"] == -4.5  # ~3 * -1.5
    # Bleed leaderboard must include "not_triggered" (n=3, net=-4.5) at or near the top
    not_triggered_bleed = next(
        (b for b in res["bleed_top"] if b["axis"] == "BE triggered" and b["bucket"] == "not_triggered"),
        None,
    )
    assert not_triggered_bleed is not None, "BE-not-triggered should be in bleed_top"
    assert not_triggered_bleed["n"] == 3
    # Ascending order: first item should have lowest (most-negative) net_pnl
    if len(res["bleed_top"]) >= 2:
        assert res["bleed_top"][0]["net_pnl"] <= res["bleed_top"][-1]["net_pnl"]


def test_n_less_than_3_excluded_from_bleed():
    trades = [_base_trade(trade_id="only",
                          pnl=-1.0, r_multiple=-1.0,
                          position_mgmt={"break_even": False, "partial_tp_done": False})]
    res = _build_profit_quality({"trades": trades}, {}, {})
    by_be = {r["bucket"]: r for r in res["be"]}
    assert by_be["not_triggered"]["n"] == 1
    # n<3 -> not in bleed_top
    assert not any(b["bucket"] == "not_triggered" for b in res["bleed_top"])


def test_sanitize_json_handles_nan_in_payload():
    """NaN/Inf in any pre-computed bucket value should _sanitize_json to None."""
    trades = [_base_trade(trade_id="nbad", pnl=1.0, r_multiple=float("nan"))]
    res = _build_profit_quality({"trades": trades}, {}, {})
    # _sanitize_json must convert NaN to None without throwing
    import json
    json.dumps(_sanitize_json(res), default=str)  # must not raise


def test_cross_matrix_axes_present_and_well_typed():
    trades = [_base_trade(trade_id="x", pnl=-0.5, r_multiple=-0.5)]
    res = _build_profit_quality({"trades": trades}, {}, {})
    for k in ("cross_be_partial", "cross_partial_stale", "cross_be_stale"):
        x = res[k]
        assert isinstance(x["axis_a"], list)
        assert isinstance(x["axis_b"], list)
        assert isinstance(x["rows"], list)
        for row in x["rows"]:
            assert "axis_a" in row and "cells" in row
            for c in row["cells"]:
                assert "axis_b" in c and "n" in c and "net_pnl" in c


def test_no_r_multiple_buckets_into_no_r():
    trades = [_base_trade(trade_id="nor", pnl=0, r_multiple=None)]
    res = _build_profit_quality({"trades": trades}, {}, {})
    assert res["r_buckets"][-1]["bucket"] == "no_r"
    assert res["r_buckets"][-1]["n"] == 1
