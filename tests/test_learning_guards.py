"""Tests for the 5 deterministic guards (Phase 2.5).

Covers each guard in isolation + the status priority order + the
payload contract (no ``patch`` or ``ops`` keys anywhere) + the
no_config_patches invariant.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loops import learning_review_loop as L


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _cfg(**guard_overrides: Any) -> dict[str, Any]:
    return {
        "learning": {
            "mode": "review_only",
            "guards": {
                "pause_on_consecutive_losses_thr": 4,
                "mistake_fingerprint_thr": 5,
                "negative_expectancy_thr": 0.0,
                **guard_overrides,
            },
        },
    }


def _loss(symbol="XAUUSDm", r=-1.0, trade_id="l") -> dict[str, Any]:
    return {"trade_id": trade_id, "symbol": symbol, "side": "BUY",
            "result": "loss", "net_profit": float(r), "r_multiple": r,
            "rating_total": 30, "mistake_categories": []}


def _win(symbol="XAUUSDm", r=0.4, trade_id="w") -> dict[str, Any]:
    return {"trade_id": trade_id, "symbol": symbol, "side": "BUY",
            "result": "win", "net_profit": 0.2, "r_multiple": r,
            "rating_total": 70, "mistake_categories": []}


def _state(**over: Any) -> dict[str, Any]:
    base = {
        "reviewed_count": 100,
        "rolling_win_rate_pct": 50.0,
        "rolling_expectancy_r": 0.0,
        "mistake_counts": {},
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 1. pause-on-consecutive-losses
# ---------------------------------------------------------------------------
def test_pause_fires_at_or_above_threshold():
    """N>=pause_thr consecutive losses -> verdict='pause' AND status='paused'."""
    reviews = [_loss(trade_id=f"L{i}") for i in range(4)]
    state = _state(rolling_expectancy_r=0.0)
    report = L.evaluate_deterministic_guards(reviews, state, _cfg())
    g = report["guards"]["pause_on_consecutive_losses"]
    assert g["verdict"] == "pause"
    assert g["consecutive_losses"] == 4
    assert g["threshold"] == 4
    assert ">= threshold" in g["details"]
    assert report["status"] == "paused"   # highest priority


def test_pause_stays_ok_below_threshold():
    # ``_new_closes`` returns reviews in NEWEST-FIRST order, so the most
    # recent review is ``reviews[0]``. A win at index 0 must break the
    # streak of older losses, regardless of how many losses follow.
    reviews = [_win(trade_id="W1")] + [_loss(trade_id=f"L{i}") for i in range(3)]
    state = _state()
    report = L.evaluate_deterministic_guards(reviews, state, _cfg())
    g = report["guards"]["pause_on_consecutive_losses"]
    assert g["verdict"] == "ok"
    assert g["consecutive_losses"] == 0  # newest is a win
    assert report["status"] != "paused"


def test_pause_respects_r_multiple_and_result_field():
    """A review with r_multiple=-0.5 OR result=='loss' counts as a loss.

    Reviews here are NEWEST-FIRST, so A=most-recent, B=older, C=oldest.
    All three qualify as losses (A via r_multiple<0, B via result='loss',
    C via both). Three consecutive from newest → consecutive_losses=3."""
    state = _state()
    reviews = [
        {"trade_id": "A", "r_multiple": -0.5, "result": "win"},  # r<0 wins
        {"trade_id": "B", "r_multiple": 0.4, "result": "loss"},  # result=='loss' wins
        {"trade_id": "C", "r_multiple": -1.0, "result": "loss"},
    ]
    report = L.evaluate_deterministic_guards(reviews, state, _cfg())
    g = report["guards"]["pause_on_consecutive_losses"]
    assert g["consecutive_losses"] == 3
    assert g["verdict"] == "ok"


def test_pause_threshold_configurable():
    reviews = [_loss(trade_id=f"L{i}") for i in range(2)]
    state = _state()
    # Raise threshold so 2 losses triggers pause.
    report = L.evaluate_deterministic_guards(
        reviews, state, _cfg(pause_on_consecutive_losses_thr=2)
    )
    assert report["guards"]["pause_on_consecutive_losses"]["verdict"] == "pause"
    assert report["status"] == "paused"


# ---------------------------------------------------------------------------
# 2. halve-on-negative-expectancy
# ---------------------------------------------------------------------------
def test_halve_advisory_when_expectancy_negative():
    state = _state(rolling_expectancy_r=-0.30)
    report = L.evaluate_deterministic_guards([], state, _cfg())
    g = report["guards"]["halve_on_negative_expectancy"]
    assert g["verdict"] == "halve"
    assert g["expectancy_r"] == -0.3
    assert g["advisory"] is not None
    assert "halve" in g["advisory"]
    assert report["status"] == "halve_advisory"


def test_halve_ok_when_expectancy_positive():
    state = _state(rolling_expectancy_r=0.20)
    report = L.evaluate_deterministic_guards([], state, _cfg())
    g = report["guards"]["halve_on_negative_expectancy"]
    assert g["verdict"] == "ok"
    assert g["advisory"] is None
    assert report["status"] == "active"


def test_halve_respects_custom_threshold():
    """Custom negative_expectancy_thr=-0.5: -0.3 still ok."""
    state = _state(rolling_expectancy_r=-0.30)
    report = L.evaluate_deterministic_guards(
        [], state, _cfg(negative_expectancy_thr=-0.5)
    )
    g = report["guards"]["halve_on_negative_expectancy"]
    assert g["verdict"] == "ok"
    assert g["threshold"] == -0.5


# ---------------------------------------------------------------------------
# 3. alert-on-mistake-fingerprint
# ---------------------------------------------------------------------------
def test_alert_when_top_mistake_reaches_threshold():
    state = _state(mistake_counts={"tp_too_early": 7, "sl_too_tight": 2})
    report = L.evaluate_deterministic_guards([], state, _cfg())
    g = report["guards"]["alert_on_mistake_fingerprint"]
    assert g["verdict"] == "alert"
    assert g["top_mistake"] == "tp_too_early"
    assert g["count"] == 7
    assert "tp_too_early" in g["details"]
    assert report["status"] == "alert"


def test_alert_ok_below_threshold():
    state = _state(mistake_counts={"tp_too_early": 3, "sl_too_tight": 2})
    report = L.evaluate_deterministic_guards([], state, _cfg())
    g = report["guards"]["alert_on_mistake_fingerprint"]
    assert g["verdict"] == "ok"
    assert g["top_mistake"] == "tp_too_early"
    assert g["count"] == 3
    assert g["details"] is None
    assert report["status"] != "alert"


def test_alert_handles_empty_mistake_counts_gracefully():
    state = _state(mistake_counts={})
    report = L.evaluate_deterministic_guards([], state, _cfg())
    g = report["guards"]["alert_on_mistake_fingerprint"]
    assert g["verdict"] == "ok"
    assert g["top_mistake"] is None
    assert g["count"] == 0


# ---------------------------------------------------------------------------
# 4. log-always
# ---------------------------------------------------------------------------
def test_log_always_emits_logged_verdict_every_call():
    state = _state()
    report = L.evaluate_deterministic_guards([], state, _cfg())
    g = report["guards"]["log_always"]
    assert g["verdict"] == "logged"
    assert g["log_path"].endswith("learning_guards.jsonl")
    assert g["logged_count"] == 100


def test_log_always_fires_with_zero_reviews():
    """Empty reviews list still produces a logged verdict."""
    report = L.evaluate_deterministic_guards([], _state(), _cfg())
    assert report["guards"]["log_always"]["verdict"] == "logged"
    assert report["reviewed_trades"] == 0


# ---------------------------------------------------------------------------
# 5. no-config-patches
# ---------------------------------------------------------------------------
def test_no_config_patches_always_ok_and_writes_blocked():
    state = _state()
    report = L.evaluate_deterministic_guards([], state, _cfg())
    g = report["guards"]["no_config_patches"]
    assert g["verdict"] == "ok"
    assert g["writes_blocked"] is True
    assert "learning_config_overrides.json" in g["blocked_targets"]
    assert "config_proposals.jsonl" in g["blocked_targets"]


def test_no_config_patches_blocks_both_targeted_files():
    """Verify both blocked files are listed regardless of input."""
    state = _state()
    report = L.evaluate_deterministic_guards([], state, _cfg())
    assert set(report["guards"]["no_config_patches"]["blocked_targets"]) == {
        "learning_config_overrides.json",
        "config_proposals.jsonl",
    }


# ---------------------------------------------------------------------------
# Status priority
# ---------------------------------------------------------------------------
def test_status_priority_pause_beats_alert_beats_halve_beats_active():
    """When multiple guards fire, paused wins. Highest severity priority."""
    state = _state(
        rolling_expectancy_r=-0.5,
        mistake_counts={"tp_too_early": 99},
    )
    reviews = [_loss(trade_id=f"L{i}") for i in range(10)]
    report = L.evaluate_deterministic_guards(reviews, state, _cfg())
    # All three would fire; paused wins.
    assert report["status"] == "paused"
    assert report["guards"]["pause_on_consecutive_losses"]["verdict"] == "pause"
    assert report["guards"]["halve_on_negative_expectancy"]["verdict"] == "halve"
    assert report["guards"]["alert_on_mistake_fingerprint"]["verdict"] == "alert"


def test_status_alert_when_no_pause_but_halve_and_alert():
    state = _state(rolling_expectancy_r=-0.5, mistake_counts={"tp_too_early": 9})
    reviews = [_win(trade_id="W1")]  # no consecutive losses
    report = L.evaluate_deterministic_guards(reviews, state, _cfg())
    assert report["status"] == "alert"


def test_status_halve_advisory_when_no_pause_no_alert():
    state = _state(rolling_expectancy_r=-0.3, mistake_counts={"sl_too_tight": 2})
    report = L.evaluate_deterministic_guards([], state, _cfg())
    assert report["status"] == "halve_advisory"


def test_status_active_when_all_ok():
    state = _state(rolling_expectancy_r=0.3, mistake_counts={"sl_too_tight": 2})
    report = L.evaluate_deterministic_guards([], state, _cfg())
    assert report["status"] == "active"


# ---------------------------------------------------------------------------
# Payload contract — no patch keys anywhere
# ---------------------------------------------------------------------------
def _walk_for_forbidden_keys(obj, forbidden):
    """Recursive key walker. Returns the first forbidden key found, else None.

    Used by the no_config_patches invariant test: a substring check on the
    serialized JSON would false-positive on guard names like
    'no_config_patches' (which contains 'patch' as substring). The structural
    check is the authoritative form.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in forbidden:
                return k
            hit = _walk_for_forbidden_keys(v, forbidden)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for item in obj:
            hit = _walk_for_forbidden_keys(item, forbidden)
            if hit is not None:
                return hit
    return None


def test_guard_report_has_no_patch_or_ops_keys():
    """The GuardReport must NEVER carry config-mutation keys.

    Enforces the no_config_patches invariant at the serialization boundary
    via a recursive structural key walk (NOT substring match — guard names
    like 'no_config_patches' contain 'patch' as substring, which would
    cause false positives).
    """
    FORBIDDEN = {"patch", "ops"}
    state = _state(
        rolling_expectancy_r=-0.5,
        mistake_counts={"tp_too_early": 50, "sl_too_tight": 30},
    )
    reviews = [_loss(trade_id=f"L{i}") for i in range(10)]
    report = L.evaluate_deterministic_guards(reviews, state, _cfg())
    leaked = _walk_for_forbidden_keys(report, FORBIDDEN)
    assert leaked is None, (
        f"GuardReport leaked forbidden key={leaked!r}; "
        f"serialized={json.dumps(report, default=str)[:400]}"
    )


def test_guard_report_top_level_keys():
    """Documented shape: timestamp, mode, status, reviewed_trades, rolling_*,
    guards, errors. No patch field at any level."""
    report = L.evaluate_deterministic_guards([], _state(), _cfg())
    expected = {"timestamp", "mode", "status", "reviewed_trades",
                "rolling_win_rate_pct", "rolling_expectancy_r",
                "guards", "errors"}
    assert set(report.keys()) == expected, (
        f"Top-level keys drift; got {sorted(report.keys())}"
    )


def test_guard_report_each_guard_subshape():
    """Each guard has 'verdict' + its own detail keys."""
    report = L.evaluate_deterministic_guards([], _state(), _cfg())
    assert set(report["guards"].keys()) == {
        "pause_on_consecutive_losses",
        "halve_on_negative_expectancy",
        "alert_on_mistake_fingerprint",
        "log_always",
        "no_config_patches",
    }
    for k, v in report["guards"].items():
        assert isinstance(v, dict), f"{k} must be a dict"
        assert "verdict" in v, f"{k} missing 'verdict'"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
def test_evaluate_guards_handles_none_state_gracefully():
    """state=None must NOT raise — guards return 'ok' verdicts with zeros."""
    report = L.evaluate_deterministic_guards(
        [], state=None, config=_cfg()
    )
    assert report is not None
    assert report["status"] in {"active", "halve_advisory", "alert", "paused"}
    assert report["guards"]["pause_on_consecutive_losses"]["consecutive_losses"] == 0
    assert report["guards"]["alert_on_mistake_fingerprint"]["count"] == 0


def test_evaluate_guards_handles_empty_reviews_gracefully():
    report = L.evaluate_deterministic_guards([], _state(), _cfg())
    assert report["reviewed_trades"] == 0
    assert report["status"] in {"active", "halve_advisory", "alert", "paused"}
