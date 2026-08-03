"""Tests for the shadow Thompson-sampling bandit over the culturing ledger."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.culturing_bandit import (
    bandit_config,
    build_bandit,
    thompson_draw,
    _posterior,
)

BCFG = bandit_config({})


def _cell(n, mean_r, ci_lo, ci_hi):
    return {"n": n, "expectancy_r": mean_r, "ci95": [ci_lo, ci_hi]}


def test_bandit_config_defaults_off():
    cfg = bandit_config({})
    assert cfg["enabled"] is False
    assert cfg["apply_shadow"] is False
    assert cfg["prior_mean_r"] == 0.0
    assert cfg["prior_weight"] == 8.0
    assert cfg["min_n"] == 8


def test_posterior_shrinks_thin_cell_toward_prior():
    # A thin cell with a scary-looking -1.0R mean over 2 trades should shrink
    # hard toward the skeptical prior (0.0), not trust the noisy 2-trade mean.
    thin = _cell(n=2, mean_r=-1.0, ci_lo=-2.5, ci_hi=0.5)
    post = _posterior(thin, BCFG)
    assert post["n"] == 2
    assert post["thin"] is True
    # posterior mean sits between prior (0.0) and sample mean (-1.0), closer to 0
    assert -0.7 < post["posterior_mean"] < -0.1
    assert post["posterior_mean"] > -1.0  # not trusting the 2-trade mean


def test_posterior_trusts_well_sampled_cell():
    # 80 trades at +0.3R mean should sit close to the sample mean.
    well = _cell(n=80, mean_r=0.3, ci_lo=0.15, ci_hi=0.45)
    post = _posterior(well, BCFG)
    assert post["thin"] is False
    assert abs(post["posterior_mean"] - 0.3) < 0.03
    # well-sampled posterior se should be tighter than a thin cell's
    thin = _cell(n=2, mean_r=-1.0, ci_lo=-2.5, ci_hi=0.5)
    thin_post = _posterior(thin, BCFG)
    assert post["posterior_se"] < thin_post["posterior_se"]


def test_thompson_draw_is_bounded():
    post = {"posterior_mean": 0.0, "posterior_se": 10.0}
    import random
    rng = random.Random(0)
    for _ in range(500):
        s = thompson_draw(post, rng)
        assert -3.0 <= s <= 3.0


def test_thompson_draw_zero_se_is_deterministic():
    post = {"posterior_mean": 0.42, "posterior_se": 0.0}
    import random
    assert thompson_draw(post, random.Random(1)) == 0.42


def test_build_bandit_shadow_payload_shape():
    ledger = {
        "cells": {
            "XAUUSDm": {
                "pullback|trend|align|london": _cell(40, 0.25, 0.1, 0.4),
                "range_fade|range|counter|tokyo": _cell(2, -0.8, -2.0, 0.4),
            },
            "USOILm": {
                "breakout|trend|align|new_york": _cell(20, -0.15, -0.3, 0.0),
            },
        }
    }
    doc = build_bandit(ledger, BCFG)
    assert doc["enabled"] is False
    assert doc["mode"] == "shadow"
    assert doc["total_cells"] == 3
    assert "XAUUSDm" in doc["cells"]
    assert "leaderboard" in doc
    assert len(doc["leaderboard"]["top10"]) <= 10
    assert len(doc["leaderboard"]["bottom10"]) <= 10
    # The positive XAU pullback cell should rank above the negative USOIL cell.
    pos = doc["cells"]["XAUUSDm"]["pullback|trend|align|london"]["posterior_mean"]
    neg = doc["cells"]["USOILm"]["breakout|trend|align|new_york"]["posterior_mean"]
    assert pos > neg


def test_build_bandit_handles_empty_and_malformed():
    assert build_bandit({}, BCFG)["total_cells"] == 0
    bad = {"cells": {"X": {"c": {"n": 5, "expectancy_r": 0.1}}, "Y": "notadict"}}
    doc = build_bandit(bad, BCFG)
    assert doc["total_cells"] == 1  # only the valid cell


def test_seed_reproducibility():
    ledger = {"cells": {"XAUUSDm": {"a|b|c|d": _cell(30, 0.1, -0.05, 0.25)}}}
    seeded = {**BCFG, "seed": 123}
    d1 = build_bandit(ledger, seeded)
    d2 = build_bandit(ledger, seeded)
    assert d1["cells"]["XAUUSDm"]["a|b|c|d"]["thompson_score"] == \
        d2["cells"]["XAUUSDm"]["a|b|c|d"]["thompson_score"]


def test_run_disabled_returns_none():
    from core.culturing_bandit import run
    assert run({}) is None  # default config -> enabled False