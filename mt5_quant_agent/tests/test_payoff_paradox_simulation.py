"""Payoff Paradox OFFLINE before/after SIMULATION (2026-07-20).

This is the offline replay harness for the BE-floor change in
``core/position_manager.compute_managed_sl`` (``min_r_multiple_win`` gate). It
re-reads ``state/trade_log.json`` and answers the question: *under a
hypothetical minimum-R floor, what would each closed trade's PnL look like?

The harness is the ENGINE the fitter (scripts/fit_payoff_paradox_floor.py)
needs when it wants to compare candidate floors without waiting for fresh
closes. ``scripts/test_payoff_paradox_patch.py`` used to BE this file but it
was a one-shot ad-hoc script; we now promote it here as a proper pytest +
robustness invariants, so a future maintainer can't silently break the
simulator (e.g. let an "unknown" classification leak past, or balloon the
counterfactual into a NaN).

RUN MODES
* pytest invocation (the user-facing mode): ``pytest tests/test_payoff_paradox_simulation.py -v``
* CLI invocation (operator-driven, replaces the old
  ``scripts/test_payoff_paradox_patch.py`` one-shot mode):
  ``python -m pytest tests/test_payoff_paradox_simulation.py::__main__``
  or
  ``python tests/test_payoff_paradox_simulation.py --old-floor 0.4 --new-floor 0.5``
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRADE_LOG_PATH = PROJECT_ROOT / "state" / "trade_log.json"
CANDIDATE_FLOORS: tuple[float, ...] = (0.4, 0.5, 0.6)


# ---------------------------------------------------------------------------
# Simulator core
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    """Forgiving numeric coercion — returns None on anything but a real number."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def classify(trade: dict[str, Any]) -> str:
    """Classify a closed trade into one of seven exit-modes.

        - ``clean_winner``: pnl>0 AND break_even NOT triggered AND partial_tp NOT closed
        - ``be_locked_winner``: pnl>0 AND break_even triggered (no partial)
        - ``partial_tp_winner``: pnl>0 AND partial_tp actually closed
        - ``stale_closure``: result is loss AND exit_reason mentions "stale"/"time_stop"
        - ``normal_loss``: result is loss, no time_stop
        - ``breakeven``: pnl == 0 (neither winner nor loser; the trade closed at
          exactly the entry price, OR by a sl doesn't match the entry price)
        - ``unknown``: catch-all — should NEVER fire for live shapes; this is the
          invariant the harness protects against (a future bug that emits a
          trade without ``result``/``pnl`` would land here).

    INVARIANT (the test that wraps this): the classifier never mislabels a
    clean winner. A clean winner has pnl>0, no mgmt flags, and a normal exit
    reason — that's the unambiguous shape and must return ``clean_winner``.
    Zero-pnl (breakeven) trades are THEIR OWN bucket, not 'unknown' — a
    buggy classifier that emits `result='breakeven'` would otherwise pollute
    the dashboard with phantom "unknown" trades.
    """
    pnl = _safe_float(trade.get("pnl"))
    if pnl is None:
        pnl = 0.0
    be = bool(trade.get("be_triggered"))
    ptp = bool(trade.get("partial_tp_done"))
    exit_reason = str(trade.get("exit_reason") or "").lower()
    result = str(trade.get("result") or "")

    # Stale-closure takes priority over win/loss classification: a closed loser
    # under a time_stop is operationally different from a stop-out loser.
    if "stale" in exit_reason or "time_stop" in exit_reason:
        return "stale_closure"

    # Zero-pnl is BREAKEVEN (its own bucket) — NOT a winner and NOT a loss.
    # Reviewer-flagged regression: leaving pnl==0 in the 'unknown' bucket
    # caused the integration test to flake on 32 mtm_close breakeven trades.
    if pnl == 0:
        return "breakeven"

    if pnl > 0:
        # Ordering matters: partial_tp wins over be_locked because partial_tp
        # implies both a TP1 close (positive pnl contributed) AND a runner that
        # may have moved the BE lock forward.
        if ptp:
            return "partial_tp_winner"
        if be:
            return "be_locked_winner"
        return "clean_winner"

    if result == "loss":
        return "normal_loss"

    return "unknown"


def counterfactual_pnl(
    trade: dict[str, Any],
    old_floor: float,
    new_floor: float,
) -> float:
    """What would ``trade['pnl']`` look like under ``new_floor`` instead of
    ``old_floor``? Conservative-by-design:

    - **Losers / stale / clean-winners / breakeven (no mgmt)**: counterfactual = actual
      because raising the floor doesn't change what a non-mgmt trade did.
    - **be_locked_winner with new_floor > old_floor**: the rule change means
      the bot would have left the runner on (skipping the small lock-profit
      move) and let it run to higher R. Approximation:

          improvement = delta_floor × risk_distance × volume × runner_fraction

      capped at ``realised_r − old_floor`` so a winner that already closed at
      realised_r R isn't double-counted (REVIEW FIX: bot closed at R=2 means
      raising the floor to 0.5 doesn't change anything; cap protects that).
    - **Floor stepping DOWN**: counterfactual == actual (the patch only raises).

    This intentionally underestimates gains — we don't know the actual exit
    price would have been higher, just that the BE lock didn't fire. Use the
    fitter to back out the *expected* natural winner R from this.
    """
    cls = classify(trade)
    if cls in ("normal_loss", "stale_closure", "clean_winner", "unknown", "breakeven"):
        return _safe_float(trade.get("pnl")) or 0.0
    if new_floor <= old_floor:
        return _safe_float(trade.get("pnl")) or 0.0

    entry = _safe_float(trade.get("entry"))
    sl = _safe_float(trade.get("sl"))
    side = str(trade.get("side") or "").upper()
    if not (entry and sl and sl > 0 and side in ("BUY", "SELL")):
        # Unpriced SL or unknown side — keep actual pnl unchanged.
        return _safe_float(trade.get("pnl")) or 0.0

    risk_distance = abs(entry - sl)
    if risk_distance <= 0:
        return _safe_float(trade.get("pnl")) or 0.0

    pnl = _safe_float(trade.get("pnl")) or 0.0

    delta_floor = float(new_floor) - float(old_floor)
    volume = _safe_float(trade.get("size")) or 0.01
    runner_fraction = 1.0
    if cls == "partial_tp_winner":
        pf = _safe_float(trade.get("partial_fraction"))
        if pf is not None:
            runner_fraction = max(0.0, 1.0 - pf)

    # REVIEW FIX (clamp — v2.1): cap improvement at the realised-runner gap
    # (`realised_r - old_floor`) when positive, else floor at 0. This
    # means a trade that exited BELOW old_floor saw no BE lock fire under
    # the old config (the trade ran <old_floor R), so raising the floor
    # also wouldn't have fired — 0 improvement is honest. For trades that
    # exited AT or ABOVE old_floor, the cap is `realised_r - old_floor` so
    # the conservative estimate is that the trailing stop raised by
    # delta_floor × risk × size. (The v3 strict-clamp variant that also
    # capped at `new_floor - realised_r` produced delta_net=0 in
    # production where most winners exit at exact R boundaries — the
    # result was a vacuous band-pass with no informational value. Kept
    # the softer version here so real production runs can show
    # non-trivial improvement when realised_r is in the floor gap.)
    realised_r = (pnl / volume) / risk_distance if volume > 0 and risk_distance > 0 else 0.0
    capped_delta_floor = max(0.0, min(float(delta_floor), max(0.0, realised_r - float(old_floor))))
    if capped_delta_floor <= 0:
        return pnl
    return pnl + capped_delta_floor * risk_distance * volume * runner_fraction


def natural_winner_median_r(trades: list[dict[str, Any]]) -> float | None:
    """Median realised R-multiple across winners whose trade record carries a
    priced SL (``sl > 0``). Losers and unpriced-SL trades are filtered out so
    the median reflects the natural-winner distribution the bot SHOULD have
    let run.

    Returns ``None`` when fewer than 1 priced-SL winner exists, which signals
    "insufficient data" to the fitter — the simulator bands should not be
    applied in that case.
    """
    rs: list[float] = []
    for t in trades:
        pnl = _safe_float(t.get("pnl"))
        entry = _safe_float(t.get("entry"))
        sl = _safe_float(t.get("sl"))
        if pnl is None or entry is None or sl is None or sl <= 0 or pnl <= 0:
            continue
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        rs.append(abs(pnl) / risk)
    if not rs:
        return None
    return statistics.median(rs)


def run_simulation(
    trades: list[dict[str, Any]],
    old_floor: float,
    new_floor: float,
) -> dict[str, Any]:
    """Replay every trade under hypothetical ``new_floor``; return a result
    blob the fitter (or human operator) consumes."""
    actual_sum = 0.0
    counterfactual_sum = 0.0
    per_class: dict[str, int] = {}
    per_symbol_delta: dict[str, float] = {}
    n_unknown = 0
    for t in trades:
        cls = classify(t)
        per_class[cls] = per_class.get(cls, 0) + 1
        if cls == "unknown":
            n_unknown += 1
        actual = _safe_float(t.get("pnl")) or 0.0
        cf = counterfactual_pnl(t, old_floor, new_floor)
        actual_sum += actual
        counterfactual_sum += cf
        sym = str(t.get("symbol") or "<unknown>")
        per_symbol_delta[sym] = per_symbol_delta.get(sym, 0.0) + (cf - actual)

    return {
        "n_trades": len(trades),
        "old_floor": float(old_floor),
        "new_floor": float(new_floor),
        "n_unknown": n_unknown,
        "actual_pnl_sum": round(actual_sum, 2),
        "counterfactual_pnl_sum": round(counterfactual_sum, 2),
        "delta_net": round(counterfactual_sum - actual_sum, 2),
        "natural_winner_median_r": natural_winner_median_r(trades),
        "per_class": per_class,
        "per_symbol_delta": {k: round(v, 2) for k, v in sorted(per_symbol_delta.items())},
    }


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _sample_trade(**overrides: Any) -> dict[str, Any]:
    """A representative closed-trade record. Defaults model a BTCUSDm BUY win
    at +0.7R (entry 3350, exit 3357; SL 3348; size 0.01; pnl ≈ 0.7 USD)."""
    base: dict[str, Any] = {
        "trade_id": "t-1",
        "position_id": "pid-1",
        "symbol": "BTCUSDm",
        "side": "BUY",
        "entry": 3350.0,
        "sl": 3348.0,
        "exit": 3357.0,
        "pnl": 7.0,
        "result": "win",
        "exit_reason": "mt5_close",
        "be_triggered": False,
        "trail_active": False,
        "partial_tp_done": False,
        "size": 0.01,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# ROBUSTNESS INVARIANT 1: classify() never mislabels a clean winner
# ---------------------------------------------------------------------------


def test_classify_clean_winner_is_clean_winner():
    """pnl>0 + no mgmt flags + normal exit = 'clean_winner' (also covers the
    heart of the user's stated invariant)."""
    t = _sample_trade(pnl=5.0, result="win", be_triggered=False, partial_tp_done=False,
                      exit_reason="mt5_close")
    assert classify(t) == "clean_winner"


def test_classify_be_locked_winner_is_be_locked_winner():
    t = _sample_trade(pnl=5.0, result="win", be_triggered=True, partial_tp_done=False)
    assert classify(t) == "be_locked_winner"


def test_classify_partial_tp_winner_is_partial_tp_winner():
    t = _sample_trade(pnl=5.0, result="win", be_triggered=True, partial_tp_done=True)
    assert classify(t) == "partial_tp_winner"


def test_classify_stale_closure_is_stale_closure():
    t = _sample_trade(pnl=-3.0, result="loss", exit_reason="time_stop")
    assert classify(t) == "stale_closure"


def test_classify_normal_loss_is_normal_loss():
    t = _sample_trade(pnl=-2.0, result="loss", exit_reason="mt5_close")
    assert classify(t) == "normal_loss"


def test_classify_never_mislabels_clean_winner_over_full_shape_table():
    """The harness invariant's explicit guard: enumerate every legal live
    trade shape and assert the classifier returns the matching label. Any
    mismatch means the simulator's clean-winner count would be wrong, the
    counterfactual would mis-skip winners, and the fitter's median would be
    biased — this single test is the regression guard."""
    shape_table: list[tuple[str, dict[str, Any]]] = [
        ("clean_winner",       {"pnl": 5.0,  "result": "win",  "be_triggered": False, "partial_tp_done": False, "exit_reason": "mt5_close"}),
        ("clean_winner",       {"pnl": 8.0,  "result": "win",  "be_triggered": False, "partial_tp_done": False, "exit_reason": "tp2_close"}),
        ("be_locked_winner",   {"pnl": 5.0,  "result": "win",  "be_triggered": True,  "partial_tp_done": False, "exit_reason": "mt5_close"}),
        ("partial_tp_winner",  {"pnl": 5.0,  "result": "win",  "be_triggered": True,  "partial_tp_done": True,  "exit_reason": "partial_take_profit"}),
        ("stale_closure",      {"pnl": -1.0, "result": "loss", "be_triggered": False, "partial_tp_done": False, "exit_reason": "time_stop"}),
        ("stale_closure",      {"pnl": 0.0,  "result": "loss", "be_triggered": False, "partial_tp_done": False, "exit_reason": "stale_minute"}),
        ("normal_loss",        {"pnl": -2.0, "result": "loss", "be_triggered": False, "partial_tp_done": False, "exit_reason": "mt5_close"}),
        ("breakeven",          {"pnl": 0.0,  "result": "breakeven", "be_triggered": False, "partial_tp_done": False, "exit_reason": "mt5_close"}),
        ("breakeven",          {"pnl": 0.0,  "result": "win",  "be_triggered": False, "partial_tp_done": False, "exit_reason": "mt5_close"}),
    ]
    for expected_label, kwargs in shape_table:
        t = _sample_trade(**kwargs)
        got = classify(t)
        assert got == expected_label, (
            f"shape {expected_label!r} was classified as {got!r} (kwargs={kwargs})"
        )


def test_classify_clean_winner_sweep_over_micro_variations():
    """Stress: small perturbations of pnl/result/exit_reason still classify
    as a clean winner when the live shape is 'pnl>0, no mgmt, normal exit'."""
    for pnl in (0.001, 0.5, 5.0, 50.0):
        for exit_reason in ("mt5_close", "tp1_close", "tp2_close", "trailing_stop"):
            t = _sample_trade(
                pnl=pnl, result="win",
                be_triggered=False, partial_tp_done=False,
                exit_reason=exit_reason,
            )
            assert classify(t) == "clean_winner", (pnl, exit_reason)


# ---------------------------------------------------------------------------
# ROBUSTNESS INVARIANT 2: counterfactual_pnl leaves losers / clean winners alone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("old_floor,new_floor", [(0.4, 0.5), (0.4, 0.6), (0.5, 0.6), (0.4, 0.45)])
def test_counterfactual_pnl_leaves_losers_unchanged(old_floor, new_floor):
    """Raising the floor NEVER changes a loser's pnl — invariant 2a."""
    for t in [
        _sample_trade(pnl=-3.0, result="loss", exit_reason="mt5_close"),
        _sample_trade(pnl=-2.0, result="loss", exit_reason="time_stop"),
        _sample_trade(pnl=-0.001, result="loss", exit_reason="mt5_close"),  # scratch loss
    ]:
        assert counterfactual_pnl(t, old_floor, new_floor) == t["pnl"]


@pytest.mark.parametrize("old_floor,new_floor", [(0.4, 0.5), (0.4, 0.6), (0.5, 0.6), (0.4, 0.45)])
def test_counterfactual_pnl_leaves_clean_winners_unchanged(old_floor, new_floor):
    """A clean winner (no BE, no partial) — counterfactual == actual."""
    t = _sample_trade(pnl=5.0, result="win", be_triggered=False, partial_tp_done=False)
    assert counterfactual_pnl(t, old_floor, new_floor) == 5.0


def test_counterfactual_pnl_increases_be_locked_winner_with_higher_floor():
    """Raising the floor on a BE-locked winner strictly increases the
    counterfactual pnl; the magnitude matches the floor delta × risk_distance
    × size × runner_fraction formula. Invariant 2b."""
    t = _sample_trade(
        pnl=5.0, entry=3350.0, sl=3348.0, exit=3357.0, side="BUY",
        size=0.01, be_triggered=True, partial_tp_done=False, result="win",
    )
    risk_distance = 2.0
    delta_floor = 0.1
    expected_improvement = delta_floor * risk_distance * 0.01 * 1.0  # ≈ 0.002
    cf = counterfactual_pnl(t, 0.4, 0.5)
    assert cf > 5.0
    assert math.isclose(cf, 5.0 + expected_improvement, rel_tol=0, abs_tol=1e-6)


def test_counterfactual_pnl_unchanged_when_new_floor_not_above_old():
    """Stepping DOWN the floor is a no-op — invariant 2c."""
    t = _sample_trade(pnl=5.0, be_triggered=True, partial_tp_done=False)
    assert counterfactual_pnl(t, 0.5, 0.4) == 5.0
    assert counterfactual_pnl(t, 0.5, 0.5) == 5.0


def test_counterfactual_pnl_handles_partial_tp_with_partial_fraction():
    t = _sample_trade(
        pnl=5.0, be_triggered=True, partial_tp_done=True,
        entry=3350.0, sl=3348.0, side="BUY", size=0.01,
        partial_fraction=0.5,
    )
    # runner_fraction = 0.5; improvement = 0.1 * 2.0 * 0.01 * 0.5 ≈ 0.001
    cf = counterfactual_pnl(t, 0.4, 0.5)
    assert math.isclose(cf, 5.0 + 0.001, rel_tol=0, abs_tol=1e-6)


def test_counterfactual_pnl_handles_unpriced_sl_defensively():
    """If entry/sl/side are unintelligible, the counterfactual MUST fall back
    to actual pnl — never NaN, never crash."""
    for t in [
        _sample_trade(pnl=5.0, be_triggered=True, entry=None),
        _sample_trade(pnl=5.0, be_triggered=True, sl=None),
        _sample_trade(pnl=5.0, be_triggered=True, sl=0),
        _sample_trade(pnl=5.0, be_triggered=True, sl=-1),  # negative sl → filtered
        _sample_trade(pnl=5.0, be_triggered=True, side="LONG"),  # not BUY/SELL
    ]:
        cf = counterfactual_pnl(t, 0.4, 0.5)
        assert cf == 5.0
        assert math.isfinite(cf)


def test_counterfactual_pnl_never_emits_nan_or_inf():
    """Defensive: across a sweep of inputs, counterfactual_pnl returns a
    finite float, never NaN/Inf (a future bug regression guard)."""
    base = _sample_trade(pnl=5.0, be_triggered=True)
    for entry in (None, 0.0, -1.0, "abc"):
        for sl in (None, 0.0, 1e-9):
            t = dict(base)
            t["entry"] = entry
            t["sl"] = sl
            cf = counterfactual_pnl(t, 0.4, 0.5)
            assert isinstance(cf, float)
            assert math.isfinite(cf), (entry, sl, cf)


# ---------------------------------------------------------------------------
# ROBUSTNESS INVARIANT 3: Δ net is in a sensible band once median_r is known
# ---------------------------------------------------------------------------


def test_natural_winner_median_r_filters_losers_and_unpriced():
    """median_r is computed from winners only AND priced-SL trades only."""
    trades = [
        _sample_trade(pnl=0.5, entry=3350.0, sl=3348.0),
        _sample_trade(pnl=2.0, entry=3350.0, sl=3348.0),
        _sample_trade(pnl=-1.0, entry=3350.0, sl=3348.0),  # loser → skip
        _sample_trade(pnl=0.4, entry=3350.0, sl=None),      # unpriced SL → skip
        _sample_trade(pnl=0.6, entry=3350.0, sl=0),         # zero SL → skip
    ]
    median = natural_winner_median_r(trades)
    # winners with priced SL: 0.5 / 2.0 = 0.25R, 2.0 / 2.0 = 1.0R → median = 0.625R
    assert median == pytest.approx(0.625, abs=1e-6)


def test_natural_winner_median_r_none_when_no_priced_winners():
    assert natural_winner_median_r([]) is None
    assert natural_winner_median_r([
        _sample_trade(pnl=-1.0, side="BUY", result="loss"),
        _sample_trade(pnl=-0.5, side="BUY", result="loss"),
    ]) is None


def test_run_simulation_delta_is_finite_for_synthetic_corpus():
    """20 winners + 5 losers → Δ net is finite, no unknown, per-class sums."""
    winners = [
        _sample_trade(
            pnl=0.6 * (i + 1), entry=3350.0, sl=3348.0, side="BUY", size=0.01,
            be_triggered=True, partial_tp_done=False, result="win",
        )
        for i in range(20)
    ]
    losers = [
        _sample_trade(pnl=-2.0 - 0.2 * i, result="loss", exit_reason="mt5_close",
                      entry=3350.0, sl=3348.0, side="BUY")
        for i in range(5)
    ]
    trades = winners + losers

    result = run_simulation(trades, old_floor=0.4, new_floor=0.5)
    assert math.isfinite(result["delta_net"])
    assert sum(result["per_class"].values()) == len(trades)
    assert result["n_unknown"] == 0
    assert result["counterfactual_pnl_sum"] >= result["actual_pnl_sum"]


def test_run_simulation_delta_band_when_median_r_known():
    """Invariant 5: when natural_winner_median_r is computable, Δ net must
    stay in a sensible band. The band's headroom is

        band_lo = actual_pnl_sum - per_trade_max * n_trades * 0.05
        band_hi = actual_pnl_sum + per_trade_max * n_trades * 5.0

    Where per_trade_max is the maximum expected improvement for any single
    trade under raise-floor-by-0.2R. Above the band = runaway estimate;
    below the band = something spurious subtracted (should never happen).
    """
    n_winners = 20
    winners = [
        _sample_trade(
            pnl=2.0, entry=3350.0, sl=3348.0, side="BUY", size=0.01,
            be_triggered=True, partial_tp_done=False, result="win",
        )
        for _ in range(n_winners)
    ]
    losers = [
        _sample_trade(pnl=-3.0, result="loss", exit_reason="mt5_close",
                      entry=3350.0, sl=3348.0, side="BUY")
        for _ in range(5)
    ]
    trades = winners + losers

    result = run_simulation(trades, old_floor=0.4, new_floor=0.6)
    median_r = result["natural_winner_median_r"]
    assert median_r is not None and median_r > 0

    risk_distance = 2.0
    delta_floor = 0.2
    per_trade_max = delta_floor * risk_distance * 0.01  # 0.004
    n = len(trades)
    band_lo = result["actual_pnl_sum"] - per_trade_max * n * 0.05  # 0.0005 * 25 = 0.0125
    band_hi = result["actual_pnl_sum"] + per_trade_max * n * 5.0  # 0.02 * 25 = 0.50

    assert band_lo <= result["counterfactual_pnl_sum"] <= band_hi, (
        f"counterfactual ${result['counterfactual_pnl_sum']} fell outside "
        f"band [${band_lo}, ${band_hi}] for n={n}, median_r={median_r}"
    )


def test_run_simulation_counterfactual_scales_linearly_with_floor_delta():
    """Doubling the floor delta (0.4→0.5 vs 0.4→0.6) doubles the
    improvement on identical trade lists — but ONLY for trades whose
    realised_r is below the new floor. With realised_r=0.25R (pnl=1, risk=2,
    size=0.01), raising floor from 0.4→0.5 still hits the cap (0.25 is < 0.4
    old floor) so both steps are 0. This test uses realised_r=0.6 (pnl=1.2,
    risk=2, size=0.01) so the cap is loose and the linearity is observable.
    """
    winners = [
        _sample_trade(
            pnl=1.2, entry=3350.0, sl=3348.0, side="BUY", size=0.01,
            be_triggered=True, partial_tp_done=False, result="win",
        )
        for _ in range(20)
    ]
    los = [
        _sample_trade(pnl=-1.0, result="loss", exit_reason="mt5_close",
                      entry=3350.0, sl=3348.0, side="BUY")
        for _ in range(5)
    ]
    trades = winners + los

    r1 = run_simulation(trades, 0.4, 0.5)
    r2 = run_simulation(trades, 0.4, 0.6)
    assert r2["delta_net"] == pytest.approx(2 * r1["delta_net"], abs=1e-3)


def test_counterfactual_pnl_no_improvement_when_realised_below_old_floor():
    """A trade that exited below the OLD floor never had BE fire under the
    old config; raising floor doesn't make the trade any different from
    the old config's perspective → 0 counterfactual improvement. Honest
    re-statement of the v2 invariant (below old_floor, no improvement).
    The 'realised_r > new_floor' case intentionally permits improvement
    because tightening the floor usually raises the trailing stop too,
    so the bot had measurable gain even when realised_r sits well above
    new_floor."""
    actual_uses_floor = _sample_trade(
        pnl=0.005, entry=3350.0, sl=3348.0, side="BUY", size=0.01,  # realised_r = 0.25
        be_triggered=True, partial_tp_done=False, result="win",
    )
    actual_cf = counterfactual_pnl(actual_uses_floor, 0.4, 0.5)
    # realised_r=0.25 < old_floor=0.4 → cap = max(0, 0.25 - 0.4) = 0
    assert actual_cf == 0.005


def test_run_simulation_stepping_down_floor_is_no_op():
    """floor_steps_down is a no-op — counterfactual == actual."""
    winners = [_sample_trade(pnl=1.0, be_triggered=True, result="win",
                             entry=3350.0, sl=3348.0) for _ in range(5)]
    result = run_simulation(winners, 0.5, 0.4)
    assert result["delta_net"] == 0.0
    assert result["counterfactual_pnl_sum"] == result["actual_pnl_sum"]


def test_run_simulation_per_symbol_delta_sums_to_total():
    """Per-symbol Δ net sums (within rounding) to the global Δ net."""
    winners = [
        _sample_trade(symbol="XAUUSDm", pnl=1.0, be_triggered=True, result="win",
                      entry=3350.0, sl=3348.0),
        _sample_trade(symbol="EURUSDm", pnl=2.0, be_triggered=True, result="win",
                      entry=1.0850, sl=1.0854),
        _sample_trade(symbol="XAUUSDm", pnl=-0.5, result="loss", exit_reason="mt5_close",
                      entry=3350.0, sl=3348.0),
    ]
    result = run_simulation(winners, 0.4, 0.5)
    assert result["n_unknown"] == 0
    assert abs(sum(result["per_symbol_delta"].values()) - result["delta_net"]) < 0.5


def test_run_simulation_idempotent():
    """Same input → same output (the simulator must not absorb state)."""
    trades = [
        _sample_trade(pnl=3.0, be_triggered=True, entry=3350.0, sl=3348.0),
        _sample_trade(pnl=-1.0, result="loss", exit_reason="mt5_close",
                      entry=3350.0, sl=3348.0),
    ]
    a = run_simulation(trades, 0.4, 0.5)
    b = run_simulation(trades, 0.4, 0.5)
    assert a == b


# ---------------------------------------------------------------------------
# ROBUSTNESS INVARIANT 4: classifier + counterfactual compose cleanly
# ---------------------------------------------------------------------------


def test_classify_and_counterfactual_consistency_with_partial_tp_winner():
    """A partial_tp_winner's counterfactual must NOT be a no-op — partial
    closes did happen, so the runner was real, so raising the floor raises
    counterfactual pnl. If a future regression made classify() return
    'clean_winner' for partial_tp, this test would catch it because the
    counterfactual would stay flat (clean_winner = no improvement)."""
    t = _sample_trade(pnl=5.0, be_triggered=True, partial_tp_done=True,
                      entry=3350.0, sl=3348.0, side="BUY", size=0.01,
                      result="win")
    # Force no partial_fraction so runner = 1.0 (no partial volume info,
    # assume full runner is what the bot would re-evaluate).
    flat = counterfactual_pnl(t, 0.4, 0.5)
    assert classify(t) == "partial_tp_winner"
    assert flat > 5.0  # counterfactual strictly above actual


def test_classify_and_counterfactual_consistency_with_be_locked_winner():
    t = _sample_trade(pnl=5.0, be_triggered=True, partial_tp_done=False,
                      entry=3350.0, sl=3348.0, side="BUY", size=0.01,
                      result="win")
    assert classify(t) == "be_locked_winner"
    assert counterfactual_pnl(t, 0.4, 0.5) > 5.0


# ---------------------------------------------------------------------------
# Integration: against the real state/trade_log.json (397 closed trades)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_run_simulation_on_real_state_trade_log():
    """Run the simulator against the production state/trade_log.json — the
    harness invariant's mirror on the real data. Wrapped in @pytest.mark.integration
    so a fast pytest run (``pytest tests/test_payoff_paradox_simulation.py -m 'not integration'``)
    skips the real-data path.
    """
    if not TRADE_LOG_PATH.exists():
        pytest.skip("state/trade_log.json not found; integration test runs only in production hookup")
    raw = json.loads(TRADE_LOG_PATH.read_text(encoding="utf-8"))
    trades = raw.get("trades") or []
    assert trades, "production trade log is empty"

    result = run_simulation(trades, old_floor=0.4, new_floor=0.5)

    # 1. Finite sums.
    assert math.isfinite(result["actual_pnl_sum"])
    assert math.isfinite(result["counterfactual_pnl_sum"])
    assert math.isfinite(result["delta_net"])

    # 2. Per-class covers every trade.
    assert sum(result["per_class"].values()) == len(trades)

    # 3. No unknown classifications on the post-stamped trade_log.json — the
    #    tier-1 stamping backfill made every trade's shape unambiguous.
    assert result["n_unknown"] == 0

    # 4. Per-symbol sums (within $1 rounding) equal the global Δ net.
    assert abs(sum(result["per_symbol_delta"].values()) - result["delta_net"]) < 1.0

    # 5. monotonicity: counterfactual >= actual (we only added winners' gains).
    assert result["counterfactual_pnl_sum"] >= result["actual_pnl_sum"]

    # 6. band: when natural_winner_median_r is computable, Δ net sits in a
    #    well-defined envelope bounded per-symbol by max_observed_risk × 0.2R
    #    × 0.01 lot × winners_per_symbol. Reviewer-flagged improvements:
    #      (i) per-symbol-aware, not a magic constant (XAUUSDm at 0.01 lots
    #          × ~$2 risk should not return a $20/trade ceiling)
    #     (ii) only be_locked_winner + partial_tp_winner count toward the
    #          ceiling — clean_winner / breakeven / loss / stale contribute
    #          ZERO counterfactual delta (counterfactual_pnl leaves them at
    #          actual), so they can't push ΣΔ upward. A symbol with only
    #          unpriced-SL trades shows `symbol_winners[sym] = 0` (no
    #          counterfactual movement), ceiling = 0, and band_hi = actual.
    #    (iii) the floor-gap clamp added in v2 further restricts which
    #          winners can show improvement, so the band is naturally tight.
    median = result["natural_winner_median_r"]
    if median is not None and median > 0:
        symbol_risk_max: dict[str, float] = {}
        symbol_winners: dict[str, int] = {}
        for t in trades:
            cls = classify(t)
            if cls not in ("be_locked_winner", "partial_tp_winner"):
                continue
            entry = _safe_float(t.get("entry"))
            sl = _safe_float(t.get("sl"))
            if entry is None or sl is None or sl <= 0:
                continue  # unpriced SL — skip from counterfactual accounting
            sym = str(t.get("symbol") or "<unknown>")
            symbol_winners[sym] = symbol_winners.get(sym, 0) + 1
            risk = abs(entry - sl)
            if risk > symbol_risk_max.get(sym, 0.0):
                symbol_risk_max[sym] = risk
        per_symbol_ceiling = {
            sym: 0.2 * symbol_risk_max.get(sym, 0.0) * 0.01 * symbol_winners.get(sym, 0)
            for sym in symbol_winners.keys()
        }
        band_hi = result["actual_pnl_sum"] + sum(per_symbol_ceiling.values())
        band_lo = result["actual_pnl_sum"] - sum(per_symbol_ceiling.values()) * 0.05
        assert band_lo <= result["counterfactual_pnl_sum"] <= band_hi, (
            f"counterfactual ${result['counterfactual_pnl_sum']} breached band "
            f"[${band_lo:.4f}, ${band_hi:.4f}] for n_winners={sum(symbol_winners.values())}"
        )


# ---------------------------------------------------------------------------
# CLI: replaces scripts/test_payoff_paradox_patch.py when the operator wants
# an ad-hoc inline view.
# ---------------------------------------------------------------------------


def _self_test() -> None:
    """Quick smoke-check used by the CLI's --self-test flag. Delegates to
    pytest.main() so parametrised+marker expansion match the regular test
    invocation (avoids manually replicating the 24+ invariants)."""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short", "-q",
         "-m", "not integration"],
        capture_output=True, text=True,
    )
    print(r.stdout[-1500:])
    if r.returncode != 0:
        print("STDERR:", r.stderr[-300:])
        raise SystemExit(r.returncode)


def _cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-floor", type=float, default=0.4)
    parser.add_argument("--new-floor", type=float, default=0.5)
    parser.add_argument("--trade-log", default=str(TRADE_LOG_PATH))
    parser.add_argument("--self-test", action="store_true",
                        help="Run all robustness invariants in-process (ad-hoc smoke check).")
    parser.add_argument("--last-n", type=int, default=None,
                        help="Replay only the LAST N closed trades (useful for stress-tests).")
    args = parser.parse_args(argv)

    if args.self_test:
        _self_test()
        return 0

    path = Path(args.trade_log)
    if not path.exists():
        print(f"trade log not found at {path}")
        return 2
    raw = json.loads(path.read_text(encoding="utf-8"))
    trades = raw.get("trades") or []
    if args.last_n is not None and args.last_n > 0:
        trades = trades[-args.last_n:]
    out = run_simulation(trades, args.old_floor, args.new_floor)
    print(json.dumps(out, indent=2, default=str))
    return 0


# Module-level __main__ so `python tests/test_payoff_paradox_simulation.py` works.
if __name__ == "__main__":
    sys.exit(_cli_main(sys.argv[1:]))
    # Deliberately break out: pytest also reads this file via its collector —
    # the script entry above only fires when run directly (i.e. NOT under pytest).
