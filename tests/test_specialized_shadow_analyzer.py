"""Tests for the shadow fire-ledger analyzer (iteration 9, 2026-08-01)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.specialized_shadow_analyzer import (
    analyze_fires,
    read_shadow_ledger,
    session_from_utc_hour,
)


def _rec(symbol, setup, side="BUY", conf=0.7, hour=14, fired_at="2026-08-01T14:00:00Z"):
    return {
        "fired_at": fired_at,
        "utc_hour": hour,
        "symbol": symbol,
        "setup_type": setup,
        "side": side,
        "confidence": conf,
        "reason": "x",
        "feat": {"price": 100.0},
    }


# --- session mapping ---

def test_session_from_utc_hour_killzones():
    assert session_from_utc_hour(1) == "tokyo"
    assert session_from_utc_hour(8) == "london_open"
    assert session_from_utc_hour(12) == "london_morning"
    assert session_from_utc_hour(15) == "ny_rth"
    assert session_from_utc_hour(18) == "ny_lunch"
    assert session_from_utc_hour(22) == "sydney"
    assert session_from_utc_hour(3) == "off_hours"
    assert session_from_utc_hour(-1) == "unknown"


# --- read_shadow_ledger ---

def test_read_shadow_ledger_missing_file(tmp_path):
    assert read_shadow_ledger(tmp_path / "nope.jsonl") == []


def test_read_shadow_ledger_parses_and_skips_bad(tmp_path):
    p = tmp_path / "ledger.jsonl"
    p.write_text(
        json.dumps(_rec("XAUUSDm", "silver_bullet")) + "\n"
        "\n"
        "not-json\n"
        + json.dumps(_rec("EURUSDm", "london_judas", hour=8)) + "\n",
        encoding="utf-8",
    )
    recs = read_shadow_ledger(p)
    assert len(recs) == 2
    assert recs[0]["symbol"] == "XAUUSDm"
    assert recs[1]["setup_type"] == "london_judas"


# --- analyze_fires ---

def test_analyze_empty():
    r = analyze_fires([])
    assert r["total_fires"] == 0
    assert r["distinct_cells"] == 0
    assert r["cells"] == []


def test_analyze_buckets_per_cell_and_flags_thin():
    # 8 silver_bullet fires on XAUUSDm at 14 UTC (ny_rth? no — 14 is ny_rth) -> 1 cell, n=8, not thin
    # 3 london_judas fires on EURUSDm at 8 UTC -> 1 cell, n=3, thin
    recs = (
        [_rec("XAUUSDm", "silver_bullet", hour=14) for _ in range(8)]
        + [_rec("EURUSDm", "london_judas", hour=8) for _ in range(3)]
    )
    r = analyze_fires(recs, min_n=8)
    assert r["total_fires"] == 11
    assert r["distinct_cells"] == 2
    assert r["thin_cells"] == 1
    # the silver_bullet cell is NOT thin (n=8), london_judas IS thin (n=3)
    sb = next(c for c in r["cells"] if c["setup_type"] == "silver_bullet")
    assert sb["n"] == 8
    assert sb["thin"] is False
    assert sb["symbol"] == "XAUUSDm"
    assert sb["session"] == "ny_rth"
    lj = next(c for c in r["cells"] if c["setup_type"] == "london_judas")
    assert lj["n"] == 3
    assert lj["thin"] is True
    assert lj["session"] == "london_open"
    assert r["thin_cells_detail"][0]["setup_type"] == "london_judas"


def test_analyze_per_symbol_per_setup_per_session():
    recs = [
        _rec("XAUUSDm", "silver_bullet", hour=14),
        _rec("XAUUSDm", "london_judas", hour=8),
        _rec("EURUSDm", "london_judas", hour=8),
    ]
    r = analyze_fires(recs, min_n=8)
    assert r["per_symbol"] == {"XAUUSDm": 2, "EURUSDm": 1}
    assert r["per_setup"] == {"london_judas": 2, "silver_bullet": 1}
    assert r["per_session"]["london_open"] == 2
    assert r["per_session"]["ny_rth"] == 1


def test_analyze_side_balance_and_avg_confidence():
    recs = [
        _rec("XAUUSDm", "silver_bullet", side="BUY", conf=0.6, hour=14),
        _rec("XAUUSDm", "silver_bullet", side="BUY", conf=0.8, hour=14),
        _rec("XAUUSDm", "silver_bullet", side="SELL", conf=0.9, hour=14),
    ]
    r = analyze_fires(recs, min_n=8)
    cell = r["cells"][0]
    assert cell["buy"] == 2
    assert cell["sell"] == 1
    assert cell["avg_confidence"] == round((0.6 + 0.8 + 0.9) / 3, 3)


def test_analyze_skips_records_missing_symbol_or_setup():
    recs = [
        _rec("XAUUSDm", "silver_bullet"),
        {"fired_at": "t", "utc_hour": 14, "symbol": None, "setup_type": "x", "side": "BUY", "confidence": 0.5},
        {"fired_at": "t", "utc_hour": 14, "symbol": "EURUSDm", "setup_type": None, "side": "BUY", "confidence": 0.5},
    ]
    r = analyze_fires(recs, min_n=8)
    assert r["total_fires"] == 1
    assert r["skipped_bad"] == 2


def test_analyze_first_last_fire_window():
    recs = [
        _rec("XAUUSDm", "silver_bullet", fired_at="2026-08-01T14:05:00Z"),
        _rec("XAUUSDm", "silver_bullet", fired_at="2026-08-01T14:30:00Z"),
        _rec("XAUUSDm", "silver_bullet", fired_at="2026-08-01T14:15:00Z"),
    ]
    r = analyze_fires(recs, min_n=8)
    cell = r["cells"][0]
    assert cell["first_fire"] == "2026-08-01T14:05:00Z"
    assert cell["last_fire"] == "2026-08-01T14:30:00Z"


# --- end-to-end via run() with a real tmp ledger ---

def test_run_reads_analyzes_and_writes_report(tmp_path, monkeypatch):
    from core import specialized_shadow_analyzer as mod
    ledger = tmp_path / "specialized_shadow_ledger.jsonl"
    ledger.write_text(
        json.dumps(_rec("XAUUSDm", "silver_bullet", hour=14)) + "\n"
        + json.dumps(_rec("AUDUSDm", "sydney_open_orb", hour=22)) + "\n",
        encoding="utf-8",
    )
    # redirect the report writer to tmp_path so we don't touch real state dir
    written = {}
    monkeypatch.setattr(mod, "write_json_state",
                        lambda name, data: written.setdefault(name, data))
    report = mod.run(path=ledger)
    assert report["total_fires"] == 2
    assert "specialized_shadow_report.json" in written
    assert written["specialized_shadow_report.json"]["total_fires"] == 2