"""Pipeline scheduling regression tests.

Guards the runtime architecture: m1_structure_loop is NOT part of the
sequential trading pipeline at all — it runs as a dedicated supervisor
service (start.py) on its own fast cadence so M1 structure evaluation is
never delayed behind the slow analytical loops. The slow policy/adaptation
loops remain throttled to ANALYTICS_EVERY_N cycles.
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
    """M1 is a dedicated service, so it is not an analytical pipeline loop."""
    import core.pipeline as pipeline_mod

    assert "m1_structure_loop" not in pipeline_mod.ANALYTICAL_LOOPS


def test_m1_loop_not_part_of_pipeline_loops():
    """M1 must NOT run inside the sequential pipeline — it is a separate
    supervisor service so slow analytical loops can never delay it."""
    # Importing the loop registry pulls in loops.data_loop, which transitively
    # imports pandas (core.history_manager) — absent in this sandbox, present
    # in the production env (Windows/MT5).
    pytest.importorskip("pandas")
    from core.pipeline import _init_loops

    names = [name for name, _fn in _init_loops()]
    assert "m1_structure_loop" not in names


def test_slow_loops_still_throttled():
    """Policy/adaptation loops must remain in the analytical set."""
    import core.pipeline as pipeline_mod

    assert {"policy_detection_loop", "policy_optimizer_loop", "adaptation_loop"} <= pipeline_mod.ANALYTICAL_LOOPS


def test_policy_optimizer_still_throttled_every_fifth(monkeypatch):
    """5 cycles: policy optimizer runs exactly once (cycle 5) even without M1."""
    import core.pipeline as pipeline_mod

    calls = {"policy_optimizer_loop": 0}

    def make(name: str):
        def _fn():
            calls[name] += 1

        return _fn

    monkeypatch.setattr(
        pipeline_mod,
        "PIPELINE_LOOPS",
        [
            ("policy_optimizer_loop", make("policy_optimizer_loop")),
        ],
    )

    logger = _QuietLogger()
    for _ in range(5):
        pipeline_mod.run_pipeline({}, logger)

    assert calls["policy_optimizer_loop"] == 1


def test_m1_service_exempt_from_pipeline_stale_cleanup():
    """The pipeline's own-loop cleanup must never drop the dedicated M1 loop
    from supervisor status — it belongs to its own service."""
    source = (ROOT / "core" / "supervisor.py").read_text(encoding="utf-8")
    assert "EXTERNAL_SERVICE_LOOPS" in source
    assert '"m1_structure_loop"' in source
