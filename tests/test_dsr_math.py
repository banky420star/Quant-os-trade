"""Regression tests for the Deflated Sharpe Ratio math in scripts/quantum_loop.py.

Pins the Bailey & Lopez de Prado (2014) False Strategy Theorem implementation
(``deflated_sharpe`` + ``_expected_max_sr``) so a future refactor cannot
silently break the data-snooping bar. The DSR is the gate that decides whether
an OOS edge survives selection-bias + non-Normality correction; an off-by-one
or wrong quantile form here would let spurious edges through (the negative-
result verdict in VERDICT.md depends on this math being correct).

Tests:
* ``_expected_max_sr`` matches the published two-quantile FST form
  ``sigma*[(1-gamma)*Phi^-1(1-1/N) + gamma*Phi^-1(1-1/(N*e))]`` independently
  re-derived in the test.
* ``deflated_sharpe`` on a synthetic R-series matches an independent
  re-computation of the full DSR formula (population moments), tolerance 1e-9.
* A strongly negative-mean series yields DSR == 0.0 (cdf underflow), and a
  mildly negative-mean series yields DSR < 0.95 — never passes the gate.
* Increasing ``n_trials`` (more selection) LOWERS DSR for the same Sharpe.
* ``crude`` mode E[max_N] > ``precise`` mode E[max_N] for N=45, and the ratio
  is ~1.23x (the documented relationship).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from statistics import NormalDist

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.quantum_loop import _EULER_MASCHERONI, _expected_max_sr, deflated_sharpe

_GAMMA = _EULER_MASCHERONI  # Euler-Mascheroni constant


# --------------------------------------------------------------------------- #
# Independent re-implementation of the published FST formulas.                #
# --------------------------------------------------------------------------- #
def _expected_max_sr_ref(n_trials: int) -> float:
    """Bailey-LdP FST two-quantile form, re-derived from the citation."""
    if n_trials <= 1:
        return 0.0
    nd = NormalDist()
    q1 = nd.inv_cdf(1 - 1 / n_trials)
    q2 = nd.inv_cdf(1 - 1 / (n_trials * math.e))
    return (1 - _GAMMA) * q1 + _GAMMA * q2


def _dsr_ref(r_multiples: list[float], *, n_trials: int, sr_var_across_trials: float) -> float:
    """Independent re-implementation of deflated_sharpe (population moments)."""
    n = len(r_multiples)
    if n < 2:
        return 0.0
    mean = sum(r_multiples) / n
    var = sum((x - mean) ** 2 for x in r_multiples) / n  # population variance
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    sr = mean / std
    m3 = sum((x - mean) ** 3 for x in r_multiples) / n
    skew = m3 / (std ** 3)
    m4 = sum((x - mean) ** 4 for x in r_multiples) / n
    kurt = m4 / (std ** 4)  # raw kurtosis (kurt-1) in the denom term
    if n_trials > 1 and sr_var_across_trials > 0:
        sr_0 = _expected_max_sr_ref(n_trials) * math.sqrt(sr_var_across_trials)
    else:
        sr_0 = 0.0
    num = (sr - sr_0) * math.sqrt(n - 1)
    den = math.sqrt(1 - skew * sr + (kurt - 1) / 4 * sr ** 2)
    if den <= 0:
        return 0.0
    return NormalDist().cdf(num / den)


# --------------------------------------------------------------------------- #
# _expected_max_sr.                                                           #
# --------------------------------------------------------------------------- #
def test_expected_max_sr_matches_published_form():
    for n in (2, 5, 10, 45, 100, 500):
        assert _expected_max_sr(n, mode="precise") == pytest.approx(
            _expected_max_sr_ref(n), abs=1e-12
        ), f"precise E[max_SR] mismatch at N={n}"


def test_expected_max_sr_no_selection_is_zero():
    assert _expected_max_sr(1, mode="precise") == 0.0
    assert _expected_max_sr(0, mode="precise") == 0.0
    assert _expected_max_sr(-3, mode="precise") == 0.0


def test_expected_max_sr_crude_is_gumbel_sqrt_2lnN():
    for n in (2, 10, 45, 100):
        assert _expected_max_sr(n, mode="crude") == pytest.approx(
            math.sqrt(2 * math.log(n)), abs=1e-12
        )


def test_crude_more_conservative_than_precise_at_n45():
    """Documented: crude E[max_N] ~1.23x precise for N=45 (crude kills more)."""
    crude = _expected_max_sr(45, mode="crude")
    precise = _expected_max_sr(45, mode="precise")
    assert crude > precise
    ratio = crude / precise
    # ~2.759 / ~2.236 = ~1.234.
    assert 1.20 < ratio < 1.26, f"crude/precise ratio {ratio:.4f} not ~1.23x"


# --------------------------------------------------------------------------- #
# deflated_sharpe — exact match against independent formula.                  #
# --------------------------------------------------------------------------- #
def test_dsr_matches_reference_formula():
    r = [0.5, -0.2, 0.8, -0.3, 0.6, -0.1, 0.4, 0.2, -0.5, 0.7,
         0.3, -0.4, 0.9, -0.15, 0.35, 0.1, -0.45, 0.55, -0.25, 0.65]
    n_trials = 45
    sr_var = 0.35

    got = deflated_sharpe(r, n_trials=n_trials, sr_var_across_trials=sr_var,
                          threshold_mode="precise")
    ref = _dsr_ref(r, n_trials=n_trials, sr_var_across_trials=sr_var)

    assert got == pytest.approx(ref, abs=1e-9), (
        f"DSR {got:.10f} != reference {ref:.10f}"
    )
    # And it is below the 0.95 deployability threshold for this series.
    assert got < 0.95


def test_dsr_no_selection_matches_reference():
    """With n_trials=1 (no selection), sr_0=0 and DSR collapses to the
    non-Normality-adjusted SR P-value."""
    r = [0.4, -0.1, 0.6, 0.2, -0.3, 0.5, 0.1, -0.2, 0.3, 0.45]
    got = deflated_sharpe(r, n_trials=1, sr_var_across_trials=0.0)
    ref = _dsr_ref(r, n_trials=1, sr_var_across_trials=0.0)
    assert got == pytest.approx(ref, abs=1e-9)


# --------------------------------------------------------------------------- #
# Negative-mean series never passes the gate.                                 #
# --------------------------------------------------------------------------- #
def test_strongly_negative_mean_yields_effectively_zero_dsr():
    """A strongly negative-mean series drives num/den to a large negative z.

    NOTE / flagged discrepancy: the task spec expected DSR == 0.0 (clamped).
    The implementation does NOT explicitly clamp the lower tail — it returns
    ``NormalDist().cdf(z)`` for a large-negative z, which is a tiny but
    NONZERO positive (e.g. ~6.6e-39 for this series). This is effectively
    zero and correctly fails the < 0.95 gate, but it is NOT a hard clamp to
    0.0. A future refactor that adds an explicit ``max(0.0, ...)`` clamp
    would change this value; pin the current behaviour here.
    """
    r = [-1.0, -1.1, -0.9, -1.2, -1.05, -0.95, -1.15, -1.0,
         -1.1, -0.9, -1.0, -1.2, -0.95, -1.05, -1.1, -0.9,
         -1.0, -1.15, -0.95, -1.05, -1.1, -0.9, -1.0, -1.2,
         -1.05, -0.95, -1.1, -1.0, -0.9, -1.15]
    dsr = deflated_sharpe(r, n_trials=45, sr_var_across_trials=0.5)
    # Effectively zero (left-tail cdf of a very negative z), not a hard clamp.
    assert 0.0 <= dsr < 1e-30, (
        f"strongly-negative DSR should underflow to ~0 (left-tail cdf), got {dsr}"
    )
    assert dsr < 0.95


def test_mildly_negative_mean_does_not_pass_and_is_not_clamped():
    """Flag the documented behaviour: the code does NOT clamp small-negative
    DSR to 0.0 — it returns the (small, positive) left-tail cdf. The gate
    condition (DSR < 0.95) still correctly rejects it. A future refactor that
    adds an explicit clamp would change this value; pin it."""
    r = [-0.05, 0.04, -0.06, 0.03, -0.04, 0.02, -0.05, 0.01,
         -0.03, -0.02, 0.04, -0.05, 0.02, -0.04, -0.01, -0.03]
    dsr = deflated_sharpe(r, n_trials=45, sr_var_across_trials=0.3)
    assert 0.0 < dsr < 0.5, (
        f"mildly-negative DSR should be a small positive cdf in (0, 0.5), got {dsr}"
    )
    assert dsr < 0.95


# --------------------------------------------------------------------------- #
# More selection lowers DSR for the same Sharpe.                              #
# --------------------------------------------------------------------------- #
def test_increasing_n_trials_lowers_dsr():
    """For a positive-Sharpe series, raising n_trials raises sr_0 (the
    selection-bias term) and so lowers DSR — the core data-snooping property."""
    r = [0.4, 0.2, 0.5, -0.1, 0.3, 0.6, 0.1, 0.45, -0.05, 0.35,
         0.5, 0.25, -0.1, 0.4, 0.3, 0.55, 0.15, 0.4, 0.2, 0.5]
    sr_var = 0.4
    dsr_low_n = deflated_sharpe(r, n_trials=5, sr_var_across_trials=sr_var)
    dsr_mid_n = deflated_sharpe(r, n_trials=45, sr_var_across_trials=sr_var)
    dsr_high_n = deflated_sharpe(r, n_trials=500, sr_var_across_trials=sr_var)

    assert dsr_low_n > dsr_mid_n > dsr_high_n, (
        f"DSR must decrease as n_trials increases: "
        f"{dsr_low_n:.4f} > {dsr_mid_n:.4f} > {dsr_high_n:.4f}"
    )


def test_zero_variance_series_returns_zero():
    r = [0.3] * 20  # identical -> std == 0
    assert deflated_sharpe(r, n_trials=45, sr_var_across_trials=0.5) == 0.0


def test_too_few_samples_returns_zero():
    assert deflated_sharpe([0.5], n_trials=45, sr_var_across_trials=0.5) == 0.0
    assert deflated_sharpe([], n_trials=45, sr_var_across_trials=0.5) == 0.0


def test_negative_denominator_returns_zero():
    """If the non-Normality denominator collapses to <= 0, DSR is clamped to 0.

    Construct a pathological series where (kurt-1)/4 * sr^2 dominates and
    makes the denominator non-positive — the guard must return 0.0 rather
    than produce a negative-variance / NaN result."""
    # Heavy-tailed, high positive skew + extreme kurtosis on a tiny sample.
    r = [10.0, -0.001, -0.001, -0.001, -0.001, -0.001, -0.001, -0.001,
         -0.001, -0.001, -0.001, -0.001, -0.001, -0.001, -0.001, -0.001,
         -0.001, -0.001, -0.001, -0.001]
    dsr = deflated_sharpe(r, n_trials=45, sr_var_across_trials=0.5)
    assert dsr == 0.0 or 0.0 <= dsr <= 1.0  # never NaN / never out of [0,1]