"""Tests for the specialized-setup shadow ledger analyzer loop.

The loop is read-only: it aggregates state/specialized_shadow_ledger.jsonl into
state/specialized_shadow_report.json and never places orders or touches MT5.
"""

from __future__ import annotations

import logging

import loops.specialized_shadow_loop as loop


def _config(shadow: bool = True) -> dict:
    return {"signals": {"specialized_setups": {"shadow": shadow}}}


def test_shadow_off_is_noop_without_reading_ledger(monkeypatch):
    monkeypatch.setattr(
        loop, "load_config", lambda: _config(shadow=False),
    )
    monkeypatch.setattr(
        loop, "setup_logger", lambda *a, **k: logging.getLogger("test-shadow-loop"),
    )
    monkeypatch.setattr(
        loop, "_analyze_shadow",
        lambda: (_ for _ in ()).throw(AssertionError("analyzer must not run when shadow=false")),
    )

    assert loop.run() is None


def test_shadow_on_returns_report_and_logs_counts(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="test-shadow-loop")
    monkeypatch.setattr(
        loop, "load_config", lambda: _config(shadow=True),
    )
    monkeypatch.setattr(
        loop, "setup_logger", lambda *a, **k: logging.getLogger("test-shadow-loop"),
    )

    def fake_analyze():
        return {
            "total_fires": 5013,
            "distinct_cells": 359,
            "thin_cells": 194,
            "per_symbol": {"XAUUSDm": 794, "US30m": 670},
        }

    monkeypatch.setattr(loop, "_analyze_shadow", fake_analyze)

    doc = loop.run()

    assert doc is not None
    assert doc["total_fires"] == 5013
    assert doc["distinct_cells"] == 359
    assert doc["thin_cells"] == 194
    assert "5013 fires" in caplog.text


def test_analyzer_failure_never_breaks_pipeline(monkeypatch):
    monkeypatch.setattr(
        loop, "load_config", lambda: _config(shadow=True),
    )
    monkeypatch.setattr(
        loop, "setup_logger", lambda *a, **k: logging.getLogger("test-shadow-loop"),
    )
    monkeypatch.setattr(
        loop, "_analyze_shadow",
        lambda: (_ for _ in ()).throw(RuntimeError("ledger read exploded")),
    )

    assert loop.run() is None  # swallowed, no exception into the pipeline
