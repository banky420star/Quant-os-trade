"""Causal Bayesian Online Change-Point Detection (BOCPD) gate.

Implements the Adams & MacKay (2007) BOCPD algorithm as a *pre-registered*
change-point GATE for the MT5 quant agent. This is NOT a strategy swap — it
is a single causal filter that drops candidate signals generated on bars the
detector flags as a changepoint / transition. The deep-research verdict
(2026-06-27) was that regime×strategy swaps inflate the multiple-comparison
count K and fail DSR, while ONE pre-registered change-point gate is the sole
defensible regime candidate (P(survive DSR/SPA) 15-25%).

Pre-registration (NOT tuned on this data)
-----------------------------------------
All hyperparameters below are fixed by citation or by a principled default
and are NOT optimised on the walk-forward windows the gate is evaluated on.
This means the gate counts as ~1 trial for DSR (the selection-bias correction
in ``scripts/quantum_loop.py``), not K extra trials.

* hazard rate lambda = 430 bars  — the YQTS calibration cited in the research
  (constant geometric hazard H(r) = 1/lambda; expected run length ~7 days of
  M5 trading). With a constant hazard, P(r=0) is bounded above by ~1/lambda,
  so the posterior-probability changepoint criterion is NOT the detection
  signal; the signal is the MAP run length dropping to a small value (the
  posterior's most likely segment just reset).
* predictive model: Gaussian with KNOWN observation variance, Normal-Normal
  conjugate posterior on the segment mean (prior precision k0=1 pseudo-obs at
  mean 0). Posterior-predictive variance = var * (1 + 1/(k0+n)): broad for a
  young/new segment (r=0, n=0 -> 2*var) and tight for a long one. This is the
  mechanism that resets the MAP run length at a regime shift — long segments'
  tight predictive collapses when the mean moves, while the broad r=0
  prior-predictive absorbs the outlier. The observation variance is estimated
  once from a warmup window of the FIRST bars seen and then frozen — NOT
  re-estimated per bar, so no in-data tuning leaks.
* changepoint criterion cp_max_run = 1  — flag a bar as a changepoint when the
  MAP run length is <= 1 (the segment just reset this step). Pre-registered,
  not swept.
* transition threshold transition_max_run = 5  — flag a bar as a transition
  when the MAP run length is in [2, 5] (recently-formed, unsettled segment).
* run-length truncation max_run = 2000  — the posterior is truncated to the
  last 2000 run lengths for O(n) compute; this is a standard practical
  modification (the posterior mass beyond 2000 bars is negligible given
  lambda=430).

Causality
---------
The detector is updated strictly online: ``update(x_t)`` only ever sees x_t
and the prior posterior. No future bars are consulted. Calling
``is_changepoint()`` / ``is_transition()`` after ``update`` answers whether
the CURRENT bar (the last one fed) is a regime boundary — causally.

References
----------
Adams, R.P. & MacKay, D.J.C. (2007). "Bayesian Online Changepoint Detection."
  arXiv:0710.3742.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any


class BocpdDetector:
    """Adams-MacKay BOCPD with a Gaussian known-variance predictive.

    State is the run-length posterior ``r_posterior`` (a list whose index r
    holds P(run_length = r | data so far)). After each ``update(x_t)`` the
    posterior is renormalised and truncated to ``max_run`` entries.
    """

    def __init__(
        self,
        *,
        hazard_lambda: float = 430.0,
        cp_max_run: int = 1,
        transition_max_run: int = 5,
        max_run: int = 2000,
        warmup_bars: int = 200,
        prior_var: float | None = None,
    ) -> None:
        self.hazard_lambda = float(hazard_lambda)
        self.hazard = 1.0 / self.hazard_lambda  # constant geometric hazard H(r)
        self.cp_max_run = int(cp_max_run)
        self.transition_max_run = int(transition_max_run)
        self.max_run = int(max_run)
        self.warmup_bars = int(warmup_bars)
        # Frozen predictive variance (set after warmup from the running
        # log-return variance; until then use a safe fallback so the first
        # warmup bars do not crash).
        self.predictive_var: float = prior_var if prior_var is not None else 1e-6
        self._warmup_returns: list[float] = []
        self._var_frozen: bool = prior_var is not None

        # Run-length posterior. Start with all mass on r=0 (no history).
        self.r_posterior: list[float] = [1.0]
        # Per-run-length sufficient statistics for the Gaussian known-variance
        # predictive: running mean of the log-returns in the current segment
        # implied by run length r. For run length r, the segment is the last
        # r+1 observations; we store the running mean and count. The initial
        # entry (r=0, before any data) uses the uninformative prior mean 0.0
        # with count 0 so the first observation's predictive is the prior.
        self._run_means: list[float] = [0.0]
        self._run_counts: list[int] = [0]
        self._steps: int = 0
        self._last_cp_prob: float = 0.0
        self._last_map_run: int = 0

    # ------------------------------------------------------------------ #
    # Online update.                                                     #
    # ------------------------------------------------------------------ #
    def update(self, x_t: float) -> None:
        """Feed the next observation (e.g. M5 log-return) and update state."""
        if not math.isfinite(x_t):
            return
        self._steps += 1

        # --- Warmup: collect the first `warmup_bars` returns to estimate the
        # --- predictive variance, then freeze it. During warmup we still run
        # --- the recursion with the fallback variance so a state exists, but
        # --- changepoint queries are suppressed (see is_changepoint).
        if not self._var_frozen:
            self._warmup_returns.append(x_t)
            if len(self._warmup_returns) >= self.warmup_bars:
                m = sum(self._warmup_returns) / len(self._warmup_returns)
                var = sum((v - m) ** 2 for v in self._warmup_returns) / len(self._warmup_returns)
                # Floor to avoid degenerate zero variance (which would make the
                # Gaussian predictive explode for any non-identical return).
                self.predictive_var = max(var, 1e-10)
                self._var_frozen = True

        var = self.predictive_var
        # --- Predictive probabilities P(x_t | run_length = r).
        # Gaussian with known observation variance ``var`` and a Normal prior
        # on the segment mean (Normal-Normal conjugate, prior precision k0=1
        # pseudo-observation at mean 0). The posterior-predictive variance for
        # a segment with n observations is ``var * (1 + 1/(k0 + n))``: broad
        # for a young / new segment (r=0, n=0 -> 2*var) and tight for a long
        # one (n large -> var). This is the mechanism that makes P(r=0) spike
        # at a regime shift: long segments have a tight predictive that
        # collapses when the mean moves, while the broad r=0 prior-predictive
        # absorbs the outlier. Pre-registered k0=1 (one prior pseudo-obs); not
        # tuned on the walk-forward windows.
        k0 = 1.0
        prev_post = self.r_posterior
        prev_means = self._run_means
        prev_counts = self._run_counts
        n = len(prev_post)

        new_post: list[float] = [0.0] * (n + 1)
        new_means: list[float] = [0.0] * (n + 1)
        new_counts: list[int] = [0] * (n + 1)

        # predictive pdf for each existing run length r.
        pdfs: list[float] = []
        for r in range(n):
            mu_r = prev_means[r]
            cnt_r = prev_counts[r]
            pred_var = var * (1.0 + 1.0 / (k0 + cnt_r))
            diff = x_t - mu_r
            p = math.exp(-0.5 * diff * diff / pred_var) / math.sqrt(2.0 * math.pi * pred_var)
            pdfs.append(p)

        # Underflow guard: if all pdfs are ~0 (a large move relative to var),
        # rescale by the max so the recursion stays numerically stable. This
        # is a standard BOCPD implementation detail and does not change the
        # posterior (it cancels in normalisation).
        max_pdf = max(pdfs) if pdfs else 0.0
        if max_pdf <= 0.0:
            max_pdf = 1e-300
        pdfs = [p / max_pdf for p in pdfs]

        # --- Changepoint prior: r_t = 0 with prob = hazard, else r_t = r_{t-1}+1.
        # --- joint[r_t = 0] = sum_r hazard * prev_post[r] * pdf[r]
        cp_joint = 0.0
        for r in range(n):
            cp_joint += self.hazard * prev_post[r] * pdfs[r]
        new_post[0] = cp_joint
        # New segment (r=0) starts with one observation x_t. Under the
        # Normal-Normal conjugate with prior mean 0 and k0=1 pseudo-obs, the
        # posterior mean after this one observation is x_t/(k0+1).
        new_means[0] = x_t / (k0 + 1.0)
        new_counts[0] = 1

        # --- Growth: r_t = r_{t-1} + 1 with prob (1 - hazard).
        for r in range(n):
            joint = (1.0 - self.hazard) * prev_post[r] * pdfs[r]
            new_post[r + 1] = joint
            # Posterior-mean update for the grown segment. ``prev_means[r]`` is
            # the posterior mean given prev_counts[r] obs; the implied sum is
            # (k0 + prev_counts[r]) * prev_means[r]. Add x_t and re-divide.
            cnt = prev_counts[r]
            mu = prev_means[r]
            new_sum = (k0 + cnt) * mu + x_t
            new_cnt = cnt + 1
            new_means[r + 1] = new_sum / (k0 + new_cnt)
            new_counts[r + 1] = new_cnt

        # --- Normalise.
        total = sum(new_post)
        if total <= 0.0:
            # Pathological (e.g. all-zero); reset to a fresh changepoint.
            new_post = [1.0] + [0.0] * n
            new_means = [x_t / 2.0] + [0.0] * n
            new_counts = [1] + [0] * n
        else:
            new_post = [p / total for p in new_post]

        # --- Truncate to max_run (keep the most recent run lengths).
        if len(new_post) > self.max_run:
            new_post = new_post[: self.max_run]
            new_means = new_means[: self.max_run]
            new_counts = new_counts[: self.max_run]
            total = sum(new_post)
            if total > 0:
                new_post = [p / total for p in new_post]

        self.r_posterior = new_post
        self._run_means = new_means
        self._run_counts = new_counts
        self._last_cp_prob = new_post[0] if new_post else 0.0
        # MAP run length
        if new_post:
            self._last_map_run = max(range(len(new_post)), key=lambda r: new_post[r])
        else:
            self._last_map_run = 0

    # ------------------------------------------------------------------ #
    # Queries.                                                           #
    # ------------------------------------------------------------------ #
    def is_warmed_up(self) -> bool:
        return self._var_frozen and self._steps >= self.warmup_bars

    def cp_probability(self) -> float:
        """Posterior probability that the current bar is a changepoint (r=0)."""
        return self._last_cp_prob

    def map_run_length(self) -> int:
        return self._last_map_run

    def is_changepoint(self) -> bool:
        """True when the MAP run length <= ``cp_max_run`` (segment just reset).

        With a constant hazard, P(r=0) is bounded by ~1/lambda, so the
        posterior-probability criterion is not the detection signal; the MAP
        run length collapsing to a small value is the principled, pre-
        registered detection that a regime shift occurred at this bar.
        """
        return self.is_warmed_up() and self._last_map_run <= self.cp_max_run

    def is_transition(self) -> bool:
        """True when the segment is young but past the changepoint bar
        (``cp_max_run`` < MAP run length <= ``transition_max_run``) — i.e. a
        recently-formed, unsettled regime. Trades are skipped here per the
        pre-registered gate."""
        return (
            self.is_warmed_up()
            and not self.is_changepoint()
            and self._last_map_run <= self.transition_max_run
        )

    def is_gated_bar(self) -> bool:
        """True when this bar should be skipped (changepoint OR transition)."""
        return self.is_changepoint() or self.is_transition()

    def regime_state(self) -> dict[str, Any]:
        return {
            "cp_probability": round(self._last_cp_prob, 4),
            "map_run_length": self._last_map_run,
            "is_changepoint": self.is_changepoint(),
            "is_transition": self.is_transition(),
            "is_gated_bar": self.is_gated_bar(),
            "warmed_up": self.is_warmed_up(),
            "steps": self._steps,
            "predictive_var": self.predictive_var,
        }


def log_return(prev_close: float, close: float) -> float:
    """Causal M5 log-return. Returns 0.0 for non-positive/missing prices."""
    if prev_close <= 0 or close <= 0:
        return 0.0
    return math.log(close / prev_close)