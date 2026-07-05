"""Coordination gate unit tests."""

from __future__ import annotations

from core.coordination import CANONICAL_LOOPS, check_coordination


def test_canonical_loops_include_adaptation_not_forward_test():
    assert "adaptation_loop" in CANONICAL_LOOPS
    assert "forward_test_loop" not in CANONICAL_LOOPS


def test_coordination_report_structure():
    report = check_coordination(max_state_age_seconds=86400.0)
    assert "coordinated" in report
    assert "checks" in report
    assert isinstance(report["checks"], list)
    names = {c["name"] for c in report["checks"]}
    assert "single_bot_instance" in names
    assert "canonical_loops_present" in names
    assert "arena_14_symbols" in names