"""Thompson-sampling bandit over the culturing ledger (SHADOW, default off).

USER CONTEXT 2026-07-31 — this is the ML upgrade the user asked for, applied
where it can actually help: per-cell setup selection. The data-driven culturing
veto (loops/forward_test_loop.py) is a HARD gate — ``n >= min_n AND netR < 0 AND
wr < floor`` flips a cell to vetoed, and the verifier hard-rejects it. That is
brittle: a cell at n=8, netR=-0.05, wr=39% is vetoed forever, while a cell at
n=8, netR=-0.40, wr=20% (clearly worse) gets the same binary veto. A bandit
replaces the binary kill with a continuous posterior: each cell keeps a
posterior over its true mean R, we Thompson-sample a selection score each
round, and the ranker/verifier can use that score as a SOFT weight instead of
a hard wall — strongly down-weighting bad cells while still occasionally
exploring thin ones (exploration is automatic via posterior width).

This module is SHADOW ONLY by design. It computes posteriors + sampled scores
and writes ``state/culturing_bandit.json``. It does NOT touch
``symbol_policy_live.json`` (the hard veto the verifier reads) and does NOT
gate any trade unless an operator explicitly enables ``culturing.bandit.apply_shadow``
(default false), and even then only as a soft confidence multiplier in the
ranker, never a hard reject. The honest research verdict (VERDICT.md — no
deployable edge at retail 30bps) means a bandit cannot manufacture edge; it
can only allocate exposure toward the least-bad cells and away from the
worst. That is hygiene, not alpha.
"""

from __future__ import annotations

import math
import random
from typing import Any

from core.utils import read_json_state, utc_now_iso, write_json_state

BANDIT_FILE = "culturing_bandit.json"

# Skeptical prior: a cell with no data is assumed to have mean R = 0 (no edge),
# which is exactly what the research verdict says is the honest base rate. The
# prior weight (in effective trades) controls how fast a cell escapes the
# prior as it accumulates samples. 8 trades = the culturing min_n, so a cell
# needs roughly a full min_n of evidence before its own data dominates.
DEFAULT_PRIOR_MEAN_R = 0.0
DEFAULT_PRIOR_WEIGHT = 8.0
# Thompson samples are clipped to [-3, 3] R so a wildly thin cell can't draw an
# extreme score and dominate a round (the posterior is wide, not unbounded).
SAMPLE_CLIP_R = 3.0


def bandit_config(config: dict[str, Any]) -> dict[str, Any]:
    cfg = (config.get("culturing") or {}).get("bandit") or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "apply_shadow": bool(cfg.get("apply_shadow", False)),
        "prior_mean_r": float(cfg.get("prior_mean_r", DEFAULT_PRIOR_MEAN_R)),
        "prior_weight": float(cfg.get("prior_weight", DEFAULT_PRIOR_WEIGHT)),
        # Inflation factor on posterior variance for thin cells (n < min_n) so
        # the bandit keeps exploring under-sampled cells instead of locking in.
        "thin_exploration_scale": float(cfg.get("thin_exploration_scale", 1.0)),
        "min_n": int(cfg.get("min_n", 8)),
        # Seed pinning for reproducibility across a single run is optional;
        # None = non-deterministic (correct for a live bandit).
        "seed": cfg.get("seed"),
    }


def _posterior(cell_stats: dict[str, Any], bcfg: dict[str, Any]) -> dict[str, Any]:
    """Conjugate-normal posterior over a cell's true mean R.

    Uses the ledger's per-cell aggregates only (mean R + CI95 + n), so we do
    not need to re-read raw trades. The CI95 the evaluator emits is on the
    *mean* (bootstrap_expectancy_ci), so ``se = (hi - lo) / 3.92``.
    """
    n = int(cell_stats.get("n", 0) or 0)
    mean_r = float(cell_stats.get("expectancy_r", 0.0) or 0.0)
    prior_mean = float(bcfg["prior_mean_r"])
    prior_w = float(bcfg["prior_weight"])

    ci = cell_stats.get("ci95") or [0.0, 0.0]
    try:
        lo, hi = float(ci[0]), float(ci[1])
        se = max((hi - lo) / 3.92, 1e-6)
    except Exception:
        se = 1.0  # unknown — treat as very uncertain

    # Conjugate Normal with known per-observation variance se^2. We use the
    # "prior as pseudo-counts" form: prior_weight is the EFFECTIVE number of
    # prior trades at prior_mean_r (8 = the culturing min_n), so a cell needs
    # roughly min_n trades of its own data to escape the skeptical prior.
    #   prior:   mean ~ N(prior_mean, se^2 / prior_w)   -> precision prior_w/se^2
    #   data:    n trades at sample mean `mean_r`, each variance se^2 -> precision n/se^2
    #   post:    precision = (prior_w + n) / se^2
    #            mean = (prior_w * prior_mean + n * mean_r) / (prior_w + n)
    post_precision = max(prior_w, 1e-6) + n
    post_mean = (prior_w * prior_mean + n * mean_r) / post_precision
    post_var = (se * se) / post_precision

    # Thin-cell exploration: inflate posterior variance so under-sampled cells
    # keep getting drawn (Thompson exploration). Scaled by how far below min_n.
    thin_scale = 1.0
    min_n = int(bcfg["min_n"])
    if 0 < n < min_n:
        thin_scale = 1.0 + float(bcfg["thin_exploration_scale"]) * (1.0 - n / min_n)
    post_var_sample = post_var * (thin_scale ** 2)

    return {
        "n": n,
        "mean_r": round(mean_r, 4),
        "prior_mean_r": prior_mean,
        "posterior_mean": round(post_mean, 4),
        "posterior_se": round(math.sqrt(max(post_var, 0.0)), 4),
        "ci95": ci,
        "thin_scale": round(thin_scale, 3),
        "thin": n < min_n,
    }


def thompson_draw(post: dict[str, Any], rng: random.Random) -> float:
    """One Thompson sample of the cell's mean R from its posterior."""
    mu = float(post.get("posterior_mean", 0.0) or 0.0)
    se = float(post.get("posterior_se", 1.0) or 0.0)
    if se <= 0:
        return max(-SAMPLE_CLIP_R, min(SAMPLE_CLIP_R, mu))
    sample = rng.gauss(mu, se)
    return max(-SAMPLE_CLIP_R, min(SAMPLE_CLIP_R, sample))


def build_bandit(ledger: dict[str, Any], bcfg: dict[str, Any]) -> dict[str, Any]:
    """Compute per-cell posteriors + a Thompson-sampled selection score.

    ``ledger`` is the forward_test_ledger.json document (cells -> symbol ->
    cell_key -> stats). Returns the culturing_bandit.json payload, SHADOW only.
    """
    rng = random.Random(bcfg.get("seed")) if bcfg.get("seed") is not None else random.Random()

    cells_block = ledger.get("cells") or {}
    out_cells: dict[str, dict[str, Any]] = {}
    for symbol, cell_map in cells_block.items():
        if not isinstance(cell_map, dict):
            continue
        sym_out: dict[str, Any] = {}
        for cell_key, stats in cell_map.items():
            if not isinstance(stats, dict):
                continue
            post = _posterior(stats, bcfg)
            sample = thompson_draw(post, rng)
            sym_out[cell_key] = {
                "n": post["n"],
                "mean_r": post["mean_r"],
                "posterior_mean": post["posterior_mean"],
                "posterior_se": post["posterior_se"],
                "thin": post["thin"],
                "thin_scale": post["thin_scale"],
                "thompson_score": round(sample, 4),
            }
        out_cells[symbol] = sym_out

    # A flat leaderboard for quick human audit: top/bottom cells by posterior
    # mean (the honest point estimate), not by the sampled score.
    flat = []
    for sym, cells in out_cells.items():
        for ck, c in cells.items():
            flat.append((sym, ck, c["posterior_mean"], c["n"], c["thompson_score"]))
    flat.sort(key=lambda r: r[2])
    leaderboard = {
        "bottom10": [
            {"symbol": s, "cell": c, "posterior_mean": pm, "n": n, "thompson_score": ts}
            for s, c, pm, n, ts in flat[:10]
        ],
        "top10": [
            {"symbol": s, "cell": c, "posterior_mean": pm, "n": n, "thompson_score": ts}
            for s, c, pm, n, ts in flat[-10:][::-1]
        ],
    }

    return {
        "updated_at": utc_now_iso(),
        "mode": "shadow" if not bcfg["apply_shadow"] else "shadow_applied",
        "enabled": bcfg["enabled"],
        "apply_shadow": bcfg["apply_shadow"],
        "prior_mean_r": bcfg["prior_mean_r"],
        "prior_weight": bcfg["prior_weight"],
        "min_n": bcfg["min_n"],
        "cells": out_cells,
        "leaderboard": leaderboard,
        "total_cells": len(flat),
    }


def run(config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Recompute the bandit posteriors from the forward-test ledger. SHADOW."""
    from core.utils import load_config

    cfg = config or load_config()
    bcfg = bandit_config(cfg)
    if not bcfg["enabled"]:
        return None

    ledger = read_json_state("forward_test_ledger.json", default={}) or {}
    doc = build_bandit(ledger, bcfg)
    write_json_state(BANDIT_FILE, doc)
    return doc