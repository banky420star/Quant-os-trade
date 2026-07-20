"""Regression tests for the BOCPD change-point gate (core/bocpd_gate.py).

Pins three causality/honesty properties of ``BocpdDetector`` so a future
refactor cannot silently break the pre-registered gate:

1. **Warmup causality**: changepoint / transition / gated-bar queries are
   always False until the detector has seen ``warmup_bars`` updates AND frozen
   its predictive variance. No detection leaks from the warmup window.

2. **Jump detection**: a large synthetic jump in the return stream (steady
   small returns then a 50x-larger return) triggers ``is_changepoint()`` True
   on that bar after warmup. A steady stream does not.

3. **NaN robustness**: ``update(NaN)`` is a no-op (does not increment the step
   counter, does not crash) — guards against missing bars poisoning the state.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.bocpd_gate import BocpdDetector


def _feed_steady(det: BocpdDetector, n: int, base: float = 1e-4) -> None:
    for _ in range(n):
        det.update(base)


# --------------------------------------------------------------------------- #
# 1. Warmup causality.                                                        #
# --------------------------------------------------------------------------- #
def test_no_detection_during_warmup():
    det = BocpdDetector(warmup_bars=20)
    for i in range(19):  # one short of warmup
        det.update(1e-4)
        assert not det.is_warmed_up(), f"warmed up early at step {i + 1}"
        assert det.is_changepoint() is False
        assert det.is_transition() is False
        assert det.is_gated_bar() is False

    # The 20th update completes warmup -> queries may now return True.
    det.update(1e-4)
    assert det.is_warmed_up() is True


def test_warmup_freezes_predictive_var():
    det = BocpdDetector(warmup_bars=10)
    for _ in range(10):
        det.update(1e-4)
    assert det.is_warmed_up() is True
    # Frozen var is the warmup variance (floored at 1e-10). Identical returns
    # -> variance 0 -> floor 1e-10.
    assert det.predictive_var == pytest.approx(1e-10)


def test_prior_var_shortcircuits_warmup_freeze():
    det = BocpdDetector(warmup_bars=50, prior_var=1e-6)
    det.update(1e-4)
    # With an explicit prior_var the variance is frozen from the start, but
    # is_warmed_up() still requires warmup_bars steps.
    assert det.predictive_var == pytest.approx(1e-6)
    assert not det.is_warmed_up()
    assert det.is_changepoint() is False


# --------------------------------------------------------------------------- #
# 2. Jump detection.                                                          #
# --------------------------------------------------------------------------- #
def test_large_jump_triggers_changepoint():
    det = BocpdDetector(warmup_bars=30, hazard_lambda=430.0)
    # Warm up on steady small returns with tiny noise so var is realistic.
    for i in range(30):
        det.update(1e-4 + 1e-9 * (i % 3))
    assert det.is_warmed_up()
    # Steady stream should not flag a changepoint.
    for _ in range(5):
        det.update(1e-4)
    assert not det.is_changepoint(), "steady stream must not flag a changepoint"

    # 50x-larger return -> MAP run length collapses -> changepoint.
    det.update(50e-4)  # 0.005 vs steady 0.0001
    assert det.is_changepoint() is True, "50x jump must trigger a changepoint"
    assert det.is_gated_bar() is True
    assert det.map_run_length() <= det.cp_max_run


def test_steady_stream_does_not_trigger_changepoint():
    det = BocpdDetector(warmup_bars=30)
    for _ in range(30):
        det.update(1e-4)
    assert det.is_warmed_up()
    # Continue the steady stream well past warmup.
    for _ in range(50):
        det.update(1e-4)
    assert not det.is_changepoint()
    assert not det.is_gated_bar()
    # MAP run length should have grown well beyond the changepoint threshold.
    assert det.map_run_length() > det.cp_max_run


def test_transition_follows_changepoint():
    """After a changepoint bar, the next bar should be a transition (young but
    past cp_max_run) before the segment matures."""
    det = BocpdDetector(warmup_bars=30, cp_max_run=1, transition_max_run=5)
    for _ in range(30):
        det.update(1e-4)
    det.update(50e-4)  # changepoint
    assert det.is_changepoint()
    # Resume steady returns — segment is now young (r small but > cp_max_run).
    for _ in range(2):
        det.update(1e-4)
    # By the second steady bar after the jump, MAP run should sit in the
    # transition band (2..5) — gated but not a fresh changepoint.
    assert det.is_gated_bar(), "young segment after a jump should be gated"


# --------------------------------------------------------------------------- #
# 3. NaN robustness.                                                          #
# --------------------------------------------------------------------------- #
def test_nan_update_is_noop():
    det = BocpdDetector(warmup_bars=20)
    for _ in range(5):
        det.update(1e-4)
    steps_before = det._steps
    posterior_before = list(det.r_posterior)
    map_before = det._last_map_run

    det.update(float("nan"))

    assert det._steps == steps_before, "NaN must not increment the step counter"
    assert det.r_posterior == posterior_before, "NaN must not mutate the posterior"
    assert det._last_map_run == map_before


def test_inf_update_is_noop():
    det = BocpdDetector(warmup_bars=20)
    for _ in range(5):
        det.update(1e-4)
    steps_before = det._steps
    det.update(float("inf"))
    det.update(float("-inf"))
    assert det._steps == steps_before


def test_nan_does_not_block_warmup_progress():
    """A NaN in the stream is skipped, so warmup_bars FINITE updates are
    required — NaNs do not count toward warmup completion."""
    det = BocpdDetector(warmup_bars=10)
    for _ in range(9):
        det.update(1e-4)
    # A NaN does NOT complete warmup (still 9 finite updates).
    det.update(float("nan"))
    assert not det.is_warmed_up()
    # The 10th finite update completes warmup.
    det.update(1e-4)
    assert det.is_warmed_up()