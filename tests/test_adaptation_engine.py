"""Adaptation engine — veto and edge diff helpers."""

from __future__ import annotations

from core.adaptation_engine import diff_be_trail, diff_edge_stats, diff_vetoes


def test_diff_vetoes_added_and_removed():
    old = {"symbols": {"XAUUSDm": {"vetoed_cells": ["a|b|c|d"]}}}
    new = {"symbols": {"XAUUSDm": {"vetoed_cells": ["a|b|c|d", "e|f|g|h"]}}}
    ledger = {"XAUUSDm": {"e|f|g|h": {"n": 10, "win_rate_pct": 30, "expectancy_net_r": -0.2}}}
    d = diff_vetoes(old, new, ledger)
    assert len(d["added"]) == 1
    assert d["added"][0]["cell"] == "e|f|g|h"
    assert len(d["removed"]) == 0


def test_diff_be_trail_trusted_change():
    old = {"symbols": {"USOILm": {"trusted": False, "expectancy_r": 0.1}}}
    new = {"symbols": {"USOILm": {"trusted": True, "expectancy_r": 0.2, "break_even": {"trigger_atr_mult": 0.5}, "trailing": {"trail_atr_mult": 0.3}, "reason": "data-driven"}}}
    changes = diff_be_trail(old, new)
    assert len(changes) == 1
    assert changes[0]["trusted"] is True


def test_diff_edge_stats_detects_shift():
    old = {"by_symbol": {"XAUUSDm": {"pullback": {"total": 5, "win_rate_pct": 60}}}}
    new = {"by_symbol": {"XAUUSDm": {"pullback": {"total": 8, "win_rate_pct": 50}}}}
    shifts = diff_edge_stats(old, new)
    assert shifts and shifts[0]["symbol"] == "XAUUSDm"