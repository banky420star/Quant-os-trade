"""Tests for the replay-evidence per-symbol veto (iteration 12, 2026-08-01)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.specialized_replay_veto import build_replay_veto, run


def _cell(symbol, setup, n, exp, ci_lo=None, ci_hi=None):
    return {
        "symbol": symbol, "setup_type": setup, "n": n,
        "wins": 0, "losses": 0, "win_rate_pct": 0.0,
        "expectancy_r": exp,
        "ci95_lower": ci_lo, "ci95_upper": ci_hi,
        "ci95_excludes_zero": (ci_hi is not None and ci_hi > 0),
        "thin": n < 8,
    }


def test_reliable_loser_is_vetoed():
    report = {"cells": [_cell("XAUUSDm", "london_bb_reversion", 200, -0.30, -0.42, -0.19)]}
    v = build_replay_veto(report, min_n=8, max_ci95_upper=0.0)
    assert v["per_symbol"] == {"XAUUSDm": ["london_bb_reversion"]}
    assert v["vetoed_cell_count"] == 1
    assert "RISK HYGIENE" in v["caveat"]


def test_not_reliable_loser_ci_includes_positive_is_skipped():
    # exp<0 but CI upper bound +0.05 > 0 -> not reliable enough -> NOT vetoed
    report = {"cells": [_cell("EURUSDm", "silver_bullet", 60, -0.10, -0.25, 0.05)]}
    v = build_replay_veto(report)
    assert v["per_symbol"] == {}


def test_positive_expectancy_is_skipped():
    report = {"cells": [_cell("BTCUSDm", "london_judas", 100, 0.19, 0.03, 0.34)]}
    v = build_replay_veto(report)
    assert v["per_symbol"] == {}


def test_thin_cell_is_skipped():
    # n=3 < min_n=8 -> skipped even if exp strongly negative
    report = {"cells": [_cell("FR40m", "silver_bullet", 3, -0.70, -1.2, -0.20)]}
    v = build_replay_veto(report, min_n=8)
    assert v["per_symbol"] == {}


def test_missing_ci_upper_is_skipped():
    report = {"cells": [_cell("US30m", "ny_bb_reversion", 10, -0.30, None, None)]}
    v = build_replay_veto(report)
    assert v["per_symbol"] == {}


def test_multiple_losers_grouped_per_symbol_and_sorted():
    report = {"cells": [
        _cell("US30m", "ny_bb_reversion", 300, -0.31, -0.42, -0.19),
        _cell("US30m", "london_bb_reversion", 200, -0.20, -0.30, -0.10),
        _cell("GBPUSDm", "london_bb_reversion", 250, -0.28, -0.40, -0.15),
    ]}
    v = build_replay_veto(report)
    assert v["per_symbol"]["US30m"] == ["london_bb_reversion", "ny_bb_reversion"]  # sorted
    assert v["per_symbol"]["GBPUSDm"] == ["london_bb_reversion"]
    assert v["vetoed_cell_count"] == 3
    assert v["symbols_affected"] == 2


def test_max_ci95_upper_threshold_respected():
    # with max_ci95_upper=-0.05, a cell whose ci_hi=-0.02 (between -0.05 and 0) is NOT vetoed
    report = {"cells": [_cell("XAUUSDm", "london_bb_reversion", 100, -0.10, -0.30, -0.02)]}
    v = build_replay_veto(report, max_ci95_upper=-0.05)
    assert v["per_symbol"] == {}
    # but with the default 0.0 threshold it IS vetoed
    v2 = build_replay_veto(report, max_ci95_upper=0.0)
    assert v2["per_symbol"] == {"XAUUSDm": ["london_bb_reversion"]}


def test_empty_report():
    v = build_replay_veto({"cells": []})
    assert v["per_symbol"] == {}
    assert v["vetoed_cell_count"] == 0


def test_run_writes_veto_from_report(monkeypatch):
    written = {}
    report = {"cells": [_cell("XAUUSDm", "london_bb_reversion", 200, -0.30, -0.42, -0.19)]}
    monkeypatch.setattr("core.specialized_replay_veto.read_json_state",
                        lambda name, default=None: report if name == "specialized_replay_report.json" else (default or {}))
    monkeypatch.setattr("core.specialized_replay_veto.write_json_state",
                        lambda name, data: written.setdefault(name, data))
    v = run(persist=True)
    assert written["specialized_replay_veto.json"]["per_symbol"] == {"XAUUSDm": ["london_bb_reversion"]}
    assert v["vetoed_cell_count"] == 1


def test_run_no_report_writes_empty_with_note(monkeypatch):
    written = {}
    monkeypatch.setattr("core.specialized_replay_veto.read_json_state",
                        lambda name, default=None: default or {})
    monkeypatch.setattr("core.specialized_replay_veto.write_json_state",
                        lambda name, data: written.setdefault(name, data))
    v = run(persist=True)
    assert v["per_symbol"] == {}
    assert "no replay report" in v["note"]
    assert written["specialized_replay_veto.json"]["per_symbol"] == {}