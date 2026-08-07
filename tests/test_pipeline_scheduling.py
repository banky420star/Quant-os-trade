"""Pipeline scheduling regression tests.

Guards the live market-state layer: m1_structure_loop must execute on EVERY
pipeline cycle (so the current FORMING M1 candle stays fresh), while the slow
policy/adaptation loops remain throttled to ANALYTICS_EVERY_N cycles.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _QuietLogger:
    def info(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


@pytest.fixture(autouse=True)
def _reset_cycle_counter(monkeypatch):
    """Each test starts from a clean cycle counter."""
    import core.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "_cycle_counter", 0)


def test_m1_structure_loop_not_in_analytical_set():
    """M1 is live market-state, not a slow analytical loop."""
    import core.pipeline as pipeline_mod

    assert "m1_structure_loop" not in pipeline_mod.ANALYTICAL_LOOPS


def test_slow_loops_still_throttled():
    """Policy/adaptation loops must remain in the analytical set."""
    import core.pipeline as pipeline_mod

    assert {"policy_detection_loop", "policy_optimizer_loop", "adaptation_loop"} <= pipeline_mod.ANALYTICAL_LOOPS


def test_m1_runs_every_cycle_policy_only_every_fifth(monkeypatch):
    """5 cycles: M1 runs 5x, policy optimizer runs exactly once (cycle 5)."""
    import core.pipeline as pipeline_mod

    calls = {"m1_structure_loop": 0, "policy_optimizer_loop": 0}

    def make(name: str):
        def _fn():
            calls[name] += 1

        return _fn

    monkeypatch.setattr(
        pipeline_mod,
        "PIPELINE_LOOPS",
        [
            ("m1_structure_loop", make("m1_structure_loop")),
            ("policy_optimizer_loop", make("policy_optimizer_loop")),
        ],
    )

    logger = _QuietLogger()
    for _ in range(5):
        pipeline_mod.run_pipeline({}, logger)

    assert calls["m1_structure_loop"] == 5
    assert calls["policy_optimizer_loop"] == 1


def test_m1_runs_on_first_cycle_before_analytics_kick_in(monkeypatch):
    """Cycle 1 (analytics still skipped) must still run M1."""
    import core.pipeline as pipeline_mod

    calls = {"m1_structure_loop": 0, "policy_optimizer_loop": 0}

    def make(name: str):
        def _fn():
            calls[name] += 1

        return _fn

    monkeypatch.setattr(
        pipeline_mod,
        "PIPELINE_LOOPS",
        [
            ("m1_structure_loop", make("m1_structure_loop")),
            ("policy_optimizer_loop", make("policy_optimizer_loop")),
        ],
    )

    pipeline_mod.run_pipeline({}, _QuietLogger())

    assert calls["m1_structure_loop"] == 1
    assert calls["policy_optimizer_loop"] == 0
