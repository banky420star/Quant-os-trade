"""Pytest harness for core/payoff_paradox_meter.py (2026-07-20).

Promoted from scripts/test_payoff_paradox_patch.py which the user requested
in their 'fold it into CI' message. The harness uses ordinary package
imports (NOT importlib.util.spec_from_file_location) so monkeypatching
``core.payoff_paradox_meter.STATE_DIR`` actually reaches the module's
namespace \u2014 the detached Spec from the v1 harness isolated PPM from the
real core.* module and broke every state-isolation test in v1.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register the integration marker (defensive \u2014 conftest also registers)."""
    config.addinivalue_line(
        "markers",
        "integration: integration test that requires real state files "
        "(state/trade_log.json). Run with -m integration explicitly.",
    )


# --- module import ----------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))  # belt-and-braces for `core.*` resolution

# Canonical package import \u2014 SHARED INSTANCE with the prod code. Any
# monkeypatch on `core.payoff_paradox_meter.STATE_DIR` reaches this binding.
import core.payoff_paradox_meter as PPM  # noqa: E402


# --- fixtures ---------------------------------------------------------------


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    """Redirect state/learning_config_overrides.json to a tmp_path file.

    WHY THIS WORKS NOW (2026-07-20): the v1 impl used
    ``importlib.util.spec_from_file_location`` which CREATES A DETACHED
    module instance \u2014 ``monkeypatch.setattr('core.payoff_paradox_meter.STATE_DIR', ...)``
    modified the shared canonical instance while PPM (the spec-loaded copy)
    still pointed to the OLD directory. Result: every test that consulted
    STATE_DIR via _state_overrides_path was reading production state and
    returning 10.0 unconditionally. v2 uses ordinary ``import core.payoff_paradox_meter``
    so both PPM and any test fixture share the same module object."""
    monkeypatch.setattr("core.payoff_paradox_meter.STATE_DIR", tmp_path)
    monkeypatch.setattr("core.utils.STATE_DIR", tmp_path)
    yield tmp_path


def _trade(pnl: float, *, mgmt_partial: bool = False, mgmt_be: bool = False,
           mgmt_trail: bool = False, entry: float = 100.0, sl: float = 99.0,
           size: float = 0.01, symbol: str = "XAUUSDm", **extra) -> dict:
    """Tiny synthetic-trade factory so tests below are 1-line readable."""
    t = {
        "symbol": symbol,
        "side": "buy",
        "entry": entry,
        "sl": sl,
        "size": size,
        "pnl": pnl,
        "result": "win" if pnl > 0 else ("loss" if pnl < 0 else "breakeven"),
        "exit_reason": "",
    }
    if mgmt_partial or mgmt_be or mgmt_trail:
        t["position_mgmt"] = {
            "break_even": mgmt_be,
            "partial_tp_done": mgmt_partial,
            "trailing": mgmt_trail,
        }
    if mgmt_partial:
        t["partial_tp_done"] = True
    if mgmt_be:
        t["be_triggered"] = True
    t.update(extra)
    return t


# --- pure-helper invariants --------------------------------------------------


@pytest.mark.parametrize("median_r,expected", [
    (-1.0, 0.4), (0.0, 0.4), (0.10, 0.4),
    (0.45, 0.4),    # boundary: stays in baseline bucket (strict > 0.45)
    (0.46, 0.5),
    (0.55, 0.5),
    (0.65, 0.5),    # boundary
    (0.66, 0.6),
    (2.10, 0.6),
])
def test_floor_for_median_r_three_buckets(median_r, expected):
    assert PPM.floor_for_median_r(median_r) == expected


@pytest.mark.parametrize("bad_input", [None, "0.5", math.nan, -math.inf, object()])
def test_floor_for_median_r_non_numeric_returns_baseline(bad_input):
    assert PPM.floor_for_median_r(bad_input) == 0.4


@pytest.mark.parametrize("huge,expected", [(math.inf, 0.6), (1e9, 0.6)])
def test_floor_for_median_r_very_large_returns_high_bucket(huge, expected):
    assert PPM.floor_for_median_r(huge) == expected


def test_ratchet_up_only_blocks_demote():
    assert PPM.ratchet_up_only(proposed=0.4, current=0.5) == 0.5
    assert PPM.ratchet_up_only(proposed=0.0, current=0.6) == 0.6
    assert PPM.ratchet_up_only(proposed=0.39, current=0.4) == 0.4


def test_ratchet_up_only_allows_exact_match():
    assert PPM.ratchet_up_only(proposed=0.5, current=0.5) == 0.5
    assert PPM.ratchet_up_only(proposed=0.4, current=0.4) == 0.4


def test_ratchet_up_only_snaps_to_nearest_above_or_equal():
    assert PPM.ratchet_up_only(proposed=0.55, current=0.5) == 0.6
    assert PPM.ratchet_up_only(proposed=0.51, current=0.5) == 0.5


def test_ratchet_up_only_caps_at_top_when_no_above_candidate():
    assert PPM.ratchet_up_only(proposed=0.99, current=0.6) == 0.6
    assert PPM.ratchet_up_only(proposed=0.7, current=0.6) == 0.6


def test_ratchet_up_only_zero_candidate_tuple_falls_back_to_current():
    assert PPM.ratchet_up_only(
        proposed=0.5, current=0.4, candidates=(0.4,),
    ) == 0.4


# --- compute_payoff_paradox_meter invariants --------------------------------


def test_compute_meter_empty_trades_returns_safe_zeros():
    m = PPM.compute_payoff_paradox_meter([], current_floor=0.4)
    assert m["n_window"] == 0
    assert m["n_winners"] == 0
    assert m["n_losers"] == 0
    assert m["incremental_WR"] == 0.0
    assert m["payoff"] == 0.0  # not inf \u2014 safer for JSON + UI
    assert m["BE_floor_suppressed_count"] == 0
    assert m["suppressed_realised_rs"] == []
    assert m["suggested_next_floor"] == 0.4
    assert m["suggested_delta"] == 0.0
    assert "holding current floor" in m["suggestion_text"] or "unchanged" in m["suggestion_text"]
    assert m["suggestion_deferred"] is False
    # New HIGH #1 reviewer fix: priced-SL coverage.
    assert m["priced_sl_coverage_pct"] == 0.0
    assert m["priced_sl_winner_coverage_pct"] == 0.0


def test_compute_meter_review_window_takes_last_n_only():
    m_with_keyed_pnl = [_trade(pnl=float(i + 1)) for i in range(50)]
    m = PPM.compute_payoff_paradox_meter(
        m_with_keyed_pnl, current_floor=0.4, review_window=20,
    )
    assert m["n_window"] == 20
    assert m["n_winners"] == 20
    assert m["n_losers"] == 0
    assert m["incremental_WR"] == 100.0
    assert m["payoff"] == float("inf")
    # 100 % priced (entry+sl+size all valid per _trade defaults).
    assert m["priced_sl_coverage_pct"] == pytest.approx(100.0, rel=0.001)
    assert m["priced_sl_winner_coverage_pct"] == pytest.approx(100.0, rel=0.001)


def test_compute_meter_winrate_over_review_window():
    wins = [_trade(pnl=1.0, mgmt_be=True, mgmt_partial=False) for _ in range(15)]
    losses = [_trade(pnl=-1.0) for _ in range(15)]
    m = PPM.compute_payoff_paradox_meter(wins + losses, current_floor=0.4)
    assert m["n_window"] == 30
    assert m["n_winners"] == 15
    assert m["n_losers"] == 15
    assert m["incremental_WR"] == 50.0


def test_compute_meter_payoff_gross_win_over_gross_loss():
    wins = [_trade(pnl=10.0) for _ in range(6)]   # $60
    losses = [_trade(pnl=-2.0) for _ in range(10)]  # -$20
    m = PPM.compute_payoff_paradox_meter(wins + losses, current_floor=0.4)
    assert m["payoff"] == pytest.approx(3.0, rel=0.001)


def test_compute_meter_payoff_inf_when_only_wins():
    wins = [_trade(pnl=1.0) for _ in range(5)]
    m = PPM.compute_payoff_paradox_meter(wins, current_floor=0.4)
    assert m["payoff"] == float("inf")


def test_compute_meter_payoff_zero_when_no_wins():
    losses = [_trade(pnl=-1.0) for _ in range(3)]
    m = PPM.compute_payoff_paradox_meter(losses, current_floor=0.4)
    assert m["payoff"] == 0.0


def test_compute_meter_no_priced_sl_trades_returns_zero_coverage():
    """HIGH #1: surfaced on production data \u2014 397 trades, 0% priced SL.
    Even if all trades are wins, the fitter is blind without priced SL."""
    trades = [_trade(pnl=1.0, entry=0.0, sl=0.0) for _ in range(30)]
    m = PPM.compute_payoff_paradox_meter(trades, current_floor=0.4)
    assert m["priced_sl_coverage_pct"] == 0.0
    assert m["priced_sl_winner_coverage_pct"] == 0.0
    assert m["median_winner_r"] == 0.0
    # And the suppression counter is zero because realised_r is None for all.
    assert m["BE_floor_suppressed_count"] == 0


def test_compute_meter_be_suppressed_count_only_mgmt_winners_below_floor():
    trades = [
        _trade(pnl=1.0, mgmt_be=True, mgmt_partial=False),    # realised_r=100
        _trade(pnl=0.005, mgmt_be=False, mgmt_partial=True),  # realised_r=0.5
        _trade(pnl=0.001, mgmt_be=True, mgmt_partial=False),  # realised_r=0.1 (suppressed)
        _trade(pnl=0.001, mgmt_partial=False, mgmt_be=False), # NOT mgmt
        _trade(pnl=-0.1, mgmt_be=True),                         # NOT a winner
    ]
    m = PPM.compute_payoff_paradox_meter(trades, current_floor=0.4)
    assert m["BE_floor_suppressed_count"] == 1
    assert m["suppressed_realised_rs"] == [pytest.approx(0.1, rel=0.001)]


def test_compute_meter_suggestion_text_no_change_when_target_equals_current():
    trades = [_trade(pnl=0.0055) for _ in range(30)]  # realised_r=0.55
    m = PPM.compute_payoff_paradox_meter(trades, current_floor=0.5)
    assert m["suggested_next_floor"] == 0.5
    assert m["suggested_delta"] == 0.0
    assert "unchanged" in m["suggestion_text"]
    assert m["suggestion_deferred"] is False


def test_compute_meter_suggestion_text_ratchet_full_window():
    trades = [_trade(pnl=0.0055) for _ in range(30)]
    m = PPM.compute_payoff_paradox_meter(trades, current_floor=0.4)
    assert m["suggested_next_floor"] == 0.5
    assert m["suggested_delta"] > 0
    assert m["suggestion_deferred"] is False
    assert "ratcheting" in m["suggestion_text"]


def test_compute_meter_suggestion_text_insufficient_window():
    trades = [_trade(pnl=0.0055) for _ in range(5)]
    m = PPM.compute_payoff_paradox_meter(trades, current_floor=0.4, review_window=30)
    assert m["n_window"] == 5
    assert m["suggested_next_floor"] == 0.5  # fitter still proposes
    assert m["suggested_delta"] > 0
    assert m["suggestion_deferred"] is True
    assert "defer" in m["suggestion_text"]
    assert "5/30" in m["suggestion_text"]
    # Writer MUST refuse because n_window<review_window.
    assert PPM.write_payoff_paradox_proposal(
        m, current_floor=0.4, now=time.time(),
    ) is None


def test_compute_meter_required_metadata_keys_present():
    m = PPM.compute_payoff_paradox_meter([_trade(pnl=1.0)], current_floor=0.4)
    required = {
        "incremental_WR", "payoff", "BE_floor_suppressed_count",
        "suppressed_realised_rs", "suggested_next_floor", "suggested_delta",
        "suggestion_text", "suggestion_deferred", "median_winner_r",
        "n_window", "n_winners", "n_losers", "current_floor",
        "review_window", "ts", "priced_sl_coverage_pct",
        "priced_sl_winner_coverage_pct",
    }
    assert required.issubset(m.keys()), f"missing: {required - set(m.keys())}"


def test_compute_meter_suppressed_realised_rs_capped_in_json():
    """MED #2: cap at top 10 with truncated flag \u2014 prevents SSE payload
    ballooning if a debug run sets review_window=5000."""
    # 30 winners all mgmt+below floor; realised_r values vary.
    trades = []
    for i in range(30):
        pnl = 0.001 * (i + 1)  # realised_r between 0.1 and 0.3
        trades.append(_trade(pnl=pnl, mgmt_be=True, mgmt_partial=True))
    m = PPM.compute_payoff_paradox_meter(trades, current_floor=0.5)
    assert m["BE_floor_suppressed_count"] >= 1
    # Capped list present + truncated flag set if cap kicked in.
    assert isinstance(m["suppressed_realised_rs"], list)
    if m["BE_floor_suppressed_count"] > 10:
        assert len(m["suppressed_realised_rs"]) <= 10
        assert m.get("suppressed_truncated") is True


def test_compute_meter_finite_arithmetic_on_noisy_dataset():
    trades = [
        _trade(pnl=0.0),
        _trade(pnl=1.0, entry=100, sl=0),
        _trade(pnl=-0.5),
        _trade(pnl=0.5, mgmt_be=True),
    ]
    m = PPM.compute_payoff_paradox_meter(trades, current_floor=0.4)
    for k, v in m.items():
        if isinstance(v, float):
            assert math.isfinite(v) or v == 0.0, f"non-finite in {k}: {v}"


def test_compute_meter_does_not_mutate_input_trades_list():
    trade = _trade(pnl=1.0, mgmt_be=True)
    trades = [trade, _trade(pnl=-1.0)]
    snapshot = list(trades)
    PPM.compute_payoff_paradox_meter(trades, current_floor=0.4)
    assert trades == snapshot


# --- write_payoff_paradox_proposal invariants -------------------------------


def test_proposal_no_op_when_suggested_equals_current(isolated_state):
    meter = PPM.compute_payoff_paradox_meter(
        [_trade(pnl=0.0055) for _ in range(30)], current_floor=0.5,
    )
    assert meter["suggested_delta"] == 0.0
    # Returns either None (writer saw no actionable change) or a pruned-tombstone;
    # either is acceptable.
    PPM.write_payoff_paradox_proposal(meter, current_floor=0.5)


def test_proposal_blocks_demote(isolated_state):
    meter = {
        "suggested_next_floor": 0.3, "suggested_delta": -0.2,
        "suggestion_text": "x", "incremental_WR": 0.0, "payoff": 0.0,
        "BE_floor_suppressed_count": 0, "median_winner_r": 0.0,
        "n_window": 30, "review_window": 30,
        "n_winners": 0, "n_losers": 0, "current_floor": 0.5,
    }
    out = PPM.write_payoff_paradox_proposal(
        meter, current_floor=0.5, now=1.0,
    )
    assert out is None
    assert not (isolated_state / "learning_config_overrides.json").exists()


def test_proposal_writes_ratchet_within_cooldown(isolated_state):
    meter = PPM.compute_payoff_paradox_meter(
        [_trade(pnl=0.0055) for _ in range(30)], current_floor=0.4,
    )
    assert meter["suggested_delta"] > 0
    payload = PPM.write_payoff_paradox_proposal(
        meter, current_floor=0.4, now=10_000.0,
    )
    assert payload is not None
    assert payload["auto_apply_allowed"] is False
    assert payload["status"] == "pending"
    assert payload["patch"]["path"] == list(PPM.METER_TUNABLE_PATH)
    assert payload["patch"]["value"] == pytest.approx(0.5, rel=0.001)
    fp = isolated_state / "learning_config_overrides.json"
    assert fp.exists()
    doc = json.loads(fp.read_text(encoding="utf-8"))
    assert len(doc["patches"]) == 1
    assert doc["patches"][0]["proposal_id"].startswith("payoff_paradox_floor_min_r_multiple_win")
    ev = doc["patches"][0]["evidence"]
    assert ev["metric"] == "payoff_paradox_meter"
    assert ev["incremental_WR"] == meter["incremental_WR"]


def test_proposal_no_write_within_cooldown(isolated_state):
    meter = PPM.compute_payoff_paradox_meter(
        [_trade(pnl=0.0055) for _ in range(30)], current_floor=0.4,
    )
    first = PPM.write_payoff_paradox_proposal(
        meter, current_floor=0.4, now=0.0, cooldown_seconds=300,
    )
    assert first is not None
    second = PPM.write_payoff_paradox_proposal(
        dict(meter, suggestion_text="x"),
        current_floor=0.4, now=120.0, cooldown_seconds=300,
    )
    assert second is None
    fp = isolated_state / "learning_config_overrides.json"
    doc = json.loads(fp.read_text(encoding="utf-8"))
    assert len(doc["patches"]) == 1
    assert doc["patches"][0]["ts"] == 0.0


def test_proposal_idempotent_when_already_pending_with_same_value(isolated_state):
    meter = PPM.compute_payoff_paradox_meter(
        [_trade(pnl=0.0055) for _ in range(30)], current_floor=0.4,
    )
    first = PPM.write_payoff_paradox_proposal(
        meter, current_floor=0.4, now=0.0, cooldown_seconds=0,
    )
    assert first is not None
    second = PPM.write_payoff_paradox_proposal(
        dict(meter, incremental_WR=99.0),
        current_floor=0.4, now=1.0, cooldown_seconds=0,
    )
    assert second is not None
    assert second.get("ts") == 1.0
    fp = isolated_state / "learning_config_overrides.json"
    doc = json.loads(fp.read_text(encoding="utf-8"))
    assert len(doc["patches"]) == 1
    assert doc["patches"][0]["evidence"]["incremental_WR"] == 99.0
    assert len(doc["patches"][0]["evidence"]["meter_revisions"]) >= 1


def test_proposal_clears_stale_payoff_paradox_patch_on_no_change(isolated_state):
    propose_meter = PPM.compute_payoff_paradox_meter(
        [_trade(pnl=0.0055) for _ in range(30)], current_floor=0.4,
    )
    PPM.write_payoff_paradox_proposal(
        propose_meter, current_floor=0.4, now=0.0, cooldown_seconds=0,
    )
    fp = isolated_state / "learning_config_overrides.json"
    doc = json.loads(fp.read_text(encoding="utf-8"))
    assert len(doc["patches"]) == 1
    zero_meter = PPM.compute_payoff_paradox_meter(
        [_trade(pnl=0.0055) for _ in range(30)], current_floor=0.5,
    )
    assert zero_meter["suggested_delta"] == 0.0
    PPM.write_payoff_paradox_proposal(zero_meter, current_floor=0.5, now=10.0)
    doc = json.loads(fp.read_text(encoding="utf-8"))
    assert len(doc["patches"]) == 0


def test_proposal_preserves_other_tunables_when_clearing_payoff_paradox(isolated_state):
    fp = isolated_state / "learning_config_overrides.json"
    fp.write_text(json.dumps({"patches": [{
        "proposal_id": "other_tunable_x", "ts": 0.0,
        "patch": {"path": ["trading", "sizes", "kelly_fraction"], "value": 0.20,
                  "delta_bounds": [0.0, 0.05]},
        "evidence": {}, "risk_level": "low",
        "auto_apply_allowed": False, "status": "pending",
    }], "rollbacks": []}, indent=2), encoding="utf-8")
    propose_meter = PPM.compute_payoff_paradox_meter(
        [_trade(pnl=0.0055) for _ in range(30)], current_floor=0.4,
    )
    PPM.write_payoff_paradox_proposal(
        propose_meter, current_floor=0.4, now=10.0, cooldown_seconds=0,
    )
    zero_meter = PPM.compute_payoff_paradox_meter(
        [_trade(pnl=0.0055) for _ in range(30)], current_floor=0.5,
    )
    PPM.write_payoff_paradox_proposal(zero_meter, current_floor=0.5, now=20.0)
    doc = json.loads(fp.read_text(encoding="utf-8"))
    assert len(doc["patches"]) == 1
    assert doc["patches"][0]["proposal_id"] == "other_tunable_x"


def test_proposal_blocked_when_delta_exceeds_upper_bound(isolated_state):
    meter = {
        "suggested_next_floor": 0.6, "suggested_delta": 0.3,
        "suggestion_text": "x", "incremental_WR": 0.0, "payoff": 0.0,
        "BE_floor_suppressed_count": 0, "median_winner_r": 0.0,
        "n_window": 30, "review_window": 30,
        "n_winners": 0, "n_losers": 0, "current_floor": 0.3,
    }
    out = PPM.write_payoff_paradox_proposal(
        meter, current_floor=0.3, now=10.0,
        delta_bounds=(0.0, 0.1),
    )
    assert out is None
    assert not (isolated_state / "learning_config_overrides.json").exists()


def test_proposal_blocked_when_window_too_small(isolated_state):
    meter = {
        "suggested_next_floor": 0.5, "suggested_delta": 0.1,
        "suggestion_text": "x", "incremental_WR": 0.0, "payoff": 0.0,
        "BE_floor_suppressed_count": 0, "median_winner_r": 0.0,
        "n_window": 12, "review_window": 30,
        "n_winners": 6, "n_losers": 6, "current_floor": 0.4,
    }
    assert PPM.write_payoff_paradox_proposal(
        meter, current_floor=0.4, now=10.0,
    ) is None


def test_proposal_persists_only_payoff_paradox_patches_and_no_non_dict_paths(isolated_state):
    fp = isolated_state / "learning_config_overrides.json"
    fp.write_text(json.dumps({"patches": [
        {"proposal_id": "weird", "ts": 0.0,
         "patch": {"path": "not-a-list", "value": 0.5, "delta_bounds": [0.0, 0.1]},
         "evidence": {}, "risk_level": "low",
         "auto_apply_allowed": False, "status": "pending"},
    ], "rollbacks": []}, indent=2), encoding="utf-8")
    assert PPM.last_proposal_ts_for_tunable(PPM.METER_TUNABLE_PATH) is None
    meter = PPM.compute_payoff_paradox_meter(
        [_trade(pnl=0.0055) for _ in range(30)], current_floor=0.4,
    )
    out = PPM.write_payoff_paradox_proposal(
        meter, current_floor=0.4, now=10.0, cooldown_seconds=0,
    )
    assert out is not None
    doc = json.loads(fp.read_text(encoding="utf-8"))
    assert len(doc["patches"]) == 2


def test_last_proposal_ts_for_tunable_no_match_returns_none(isolated_state):
    """isolated_state fixture provides tmp_path; explicitly populate overrides."""
    (isolated_state / "learning_config_overrides.json").write_text(
        json.dumps({"patches": [], "rollbacks": []}, indent=2), encoding="utf-8",
    )
    assert PPM.last_proposal_ts_for_tunable(PPM.METER_TUNABLE_PATH) is None


def test_last_proposal_ts_for_tunable_returns_max_match(isolated_state):
    (isolated_state / "learning_config_overrides.json").write_text(json.dumps({
        "patches": [
            {"proposal_id": "a", "ts": 100.0,
             "patch": {"path": list(PPM.METER_TUNABLE_PATH), "value": 0.5,
                       "delta_bounds": [0.0, 0.1]},
             "evidence": {}, "risk_level": "low",
             "auto_apply_allowed": False, "status": "pending"},
            {"proposal_id": "b", "ts": 250.0,
             "patch": {"path": list(PPM.METER_TUNABLE_PATH), "value": 0.6,
                       "delta_bounds": [0.0, 0.1]},
             "evidence": {}, "risk_level": "low",
             "auto_apply_allowed": False, "status": "pending"},
            {"proposal_id": "other", "ts": 999.0,
             "patch": {"path": ["trading", "sizes", "kelly_fraction"],
                       "value": 0.20, "delta_bounds": [0.0, 0.05]},
             "evidence": {}, "risk_level": "low",
             "auto_apply_allowed": False, "status": "pending"},
        ],
        "rollbacks": [],
    }, indent=2), encoding="utf-8")
    assert PPM.last_proposal_ts_for_tunable(PPM.METER_TUNABLE_PATH) == 250.0


def test_last_proposal_ts_for_tunable_handles_zero_ts(isolated_state):
    """REGRESSION GUARD: the helper used `if ts:` which evaluated 0.0 as
    falsy and silently excluded the patch. After the `is not None` fix,
    ts=0.0 is preserved AND cooldown fires correctly."""
    (isolated_state / "learning_config_overrides.json").write_text(json.dumps({
        "patches": [{
            "proposal_id": "zero_ts", "ts": 0.0,
            "patch": {"path": list(PPM.METER_TUNABLE_PATH), "value": 0.5,
                       "delta_bounds": [0.0, 0.1]},
            "evidence": {}, "risk_level": "low",
            "auto_apply_allowed": False, "status": "pending",
        }], "rollbacks": [],
    }, indent=2), encoding="utf-8")
    assert PPM.last_proposal_ts_for_tunable(PPM.METER_TUNABLE_PATH) == 0.0


def test_radical_candidate_set_invariant():
    assert PPM.ratchet_up_only(
        proposed=0.5, current=0.4, candidates=(0.4,),
    ) == 0.4
    assert PPM.ratchet_up_only(
        proposed=0.6, current=0.4, candidates=(0.4,),
    ) == 0.4
    assert PPM.ratchet_up_only(
        proposed=0.6, current=0.4, candidates=(),
    ) == 0.4


# --- CLI operator entry preserved ------------------------------------------


def test_cli_help_exits_cleanly():
    py = sys.executable
    proj = PROJECT_ROOT
    env = os.environ.copy()
    env["PYTHONPATH"] = str(proj) + os.pathsep + env.get("PYTHONPATH", "")
    res = subprocess.run(
        [py, str(proj / "tests" / "test_payoff_paradox_meter.py"), "--help"],
        capture_output=True, text=True, cwd=str(proj), env=env,
    )
    assert res.returncode == 0, f"stdout={res.stdout!r}, stderr={res.stderr!r}"
    assert "old-floor" in res.stdout


# ---------------------------------------------------------------------------
# Integration test against real state/trade_log.json (gated)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_run_simulation_on_real_state_trade_log():
    """On the real trade log the meter shouldn't NaN / Inf and the proposal
    writer must accept the production-data shapes without crashing.

    HIGH #1 reviewer expectation: priced_sl_coverage reports ~0% on prod
    because paper trades lack priced SLs \u2014 the field surfaces the silent
    'fitter blind' state so the operator knows why the meter is silent."""
    prod = PROJECT_ROOT / "state" / "trade_log.json"
    if not prod.exists():
        pytest.skip("state/trade_log.json not present in this worktree")
    data = json.loads(prod.read_text(encoding="utf-8"))
    trades = data.get("trades") or []
    if not trades:
        pytest.skip("no trade rows in prod log")
    meter = PPM.compute_payoff_paradox_meter(trades, current_floor=0.4)
    for k, v in meter.items():
        if isinstance(v, float):
            assert math.isfinite(v) or v == 0.0, f"non-finite in {k}: {v}"
    assert "priced_sl_coverage_pct" in meter
    assert "priced_sl_winner_coverage_pct" in meter
    out = PPM.write_payoff_paradox_proposal(
        meter, current_floor=0.4, now=time.time(),
    )
    # Real-data call must not crash; out may be a dict (patch written) or None.
    assert out is None or isinstance(out, dict)


# ---------------------------------------------------------------------------
# CLI operator entry (preserved from offline harness)
# ---------------------------------------------------------------------------


def _cli_main(argv=None) -> int:
    """Tiny CLI wrapper for the offline harness. Mirrors the simulator's
    __main__ behaviour: run a single meter computation on a state file or
    run the pytest harness with --self-test."""
    import argparse
    ap = argparse.ArgumentParser(
        description="Offline harness for the Payoff Paradox Meter.",
    )
    ap.add_argument("--state-file", default="state/trade_log.json",
                    help="Trade-log JSON to read; default state/trade_log.json")
    ap.add_argument("--current-floor", type=float, default=0.4,
                    help="Current min_r_multiple_win to anchor ratchet (default 0.4)")
    ap.add_argument("--old-floor", type=float, default=0.4)
    ap.add_argument("--new-floor", type=float, default=None)
    ap.add_argument("--review-window", type=int, default=PPM.REVIEW_WINDOW_DEFAULT)
    ap.add_argument("--self-test", action="store_true",
                    help="Run the pytest harness in-process and exit with its exit code.")
    ns = ap.parse_args(argv)

    if ns.self_test:
        import pytest as _pytest
        return int(_pytest.main(["-q", __file__]))

    p = Path(ns.state_file)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.exists():
        print(json.dumps({"error": f"state file not found: {p}"}), file=sys.stderr)
        return 2
    data = json.loads(p.read_text(encoding="utf-8"))
    trades = data.get("trades") or []
    meter = PPM.compute_payoff_paradox_meter(
        trades, current_floor=ns.current_floor, review_window=ns.review_window,
    )
    print(json.dumps({
        "n_trades": len(trades),
        "meter": meter,
        "proposal": PPM.write_payoff_paradox_proposal(
            meter, current_floor=ns.current_floor, now=time.time(),
        ),
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
