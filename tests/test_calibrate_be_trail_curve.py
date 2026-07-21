"""Unit tests for scripts/calibrate_be_trail_curve.py — v3 signatures.

`_project(trades, lock_usd, baseline)` and `_global_flip(trades, log, baseline)`
now require the caller to pre-compute baseline; tests pass it explicitly.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "calibrate_be_trail_curve.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("calibrate_be_trail_curve", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


def _trade(symbol, pnl, **kwargs):
    return {"symbol": symbol, "pnl": pnl, **kwargs}


def _fake_log():
    return logging.getLogger("test_calibrate_curve")


def _baseline_of(trades):
    return mod._baseline(trades)


def _project(trades, lock_usd):
    """Test helper: hoist baseline, then call the canonical signature."""
    return mod._project(trades, lock_usd, _baseline_of(trades))


def _global(trades):
    return mod._global_flip(trades, _fake_log(), _baseline_of(trades))


# ---------------------------------------------------------------- <<< _baseline


def test_baseline_empty_returns_clean_zeroes():
    base = mod._baseline([])
    assert base["n"] == 0
    assert base["n_wins"] == 0
    assert base["n_losses"] == 0
    assert base["total_pnl_usd"] == 0
    assert base["by_symbol"] == {}


def test_baseline_classifies_win_loss_and_totals():
    trades = [
        _trade("XAUUSDm", 0.10),
        _trade("XAUUSDm", -0.50),
        _trade("XAUUSDm", 0.40),
        _trade("USOILm", -0.30),
        _trade("USOILm", 0.05),
    ]
    base = mod._baseline(trades)
    assert base["n"] == 5
    assert base["n_wins"] == 3
    assert base["n_losses"] == 2
    assert base["wr_pct"] == pytest.approx(60.0)
    assert base["avg_win_usd"] == pytest.approx((0.10 + 0.40 + 0.05) / 3)
    assert base["avg_loss_usd"] == pytest.approx((-0.50 + -0.30) / 2)
    assert base["total_pnl_usd"] == pytest.approx(-0.25)
    assert set(base["by_symbol"].keys()) == {"XAUUSDm", "USOILm"}
    assert base["by_symbol"]["XAUUSDm"]["n"] == 3
    assert base["by_symbol"]["USOILm"]["n"] == 2
    # breakeven_wr_pct field exists and is in [0,100]
    for sym, d in base["by_symbol"].items():
        assert 0.0 <= d["breakeven_wr_pct"] <= 100.0


def test_baseline_breakeven_wr_pct_for_winner_only_symbol():
    """No-loss symbol: breakeven_wr_pct is the artificial 100.0 fallback."""
    trades = [_trade("XAUUSDm", 0.10), _trade("XAUUSDm", 0.20)]
    base = mod._baseline(trades)
    assert base["by_symbol"]["XAUUSDm"]["breakeven_wr_pct"] == 100.0


def test_baseline_handles_missing_pnl_as_zero():
    trades = [{"symbol": "XAUUSDm"}, {"symbol": "XAUUSDm", "pnl": 0.10}]
    base = mod._baseline(trades)
    assert base["n_wins"] == 1
    assert base["n_losses"] == 1
    assert base["total_pnl_usd"] == pytest.approx(0.10)


# ---------------------------------------------------------------- <<< _project


def test_project_floors_dust_winners_only():
    trades = [
        _trade("XAUUSDm", 0.05),   # dust -> floor to 0.40
        _trade("XAUUSDm", 0.20),   # dust -> floor to 0.40
        _trade("XAUUSDm", 0.80),   # not dust, untouched
        _trade("XAUUSDm", 0.40),   # exactly at floor (boundary: NOT boosted, strict-less)
        _trade("XAUUSDm", -0.50),  # loser untouched
        _trade("XAUUSDm", -0.90),  # loser untouched
    ]
    proj, series = _project(trades, 0.40)
    assert series == [0.40, 0.40, 0.80, 0.40, -0.50, -0.90]
    assert proj["n_dust_boosted"] == 2
    assert proj["boost_sum_usd"] == pytest.approx((0.40 - 0.05) + (0.40 - 0.20))
    assert proj["projected_total_pnl_usd"] == pytest.approx(0.60)
    assert proj["delta_vs_baseline_usd"] == pytest.approx(0.60 - 0.05)


def test_project_zero_lock_changes_nothing():
    trades = [_trade("XAUUSDm", 0.05), _trade("XAUUSDm", -0.50)]
    proj, series = _project(trades, 0.0)
    assert proj["n_dust_boosted"] == 0
    assert proj["delta_vs_baseline_usd"] == 0.0
    assert series == [0.05, -0.50]


def test_project_monotonic_non_decreasing_with_lock():
    trades = [
        _trade("XAUUSDm", 0.05),
        _trade("XAUUSDm", 0.20),
        _trade("XAUUSDm", 0.40),
        _trade("XAUUSDm", -0.50),
        _trade("XAUUSDm", -0.80),
    ]
    totals = []
    for lock_usd in mod.LOCK_GRID_USD:
        proj, _ = _project(trades, lock_usd)
        totals.append(proj["projected_total_pnl_usd"])
    for i in range(1, len(totals)):
        assert totals[i] >= totals[i - 1] - 1e-9, (
            f"projected total decreased from lock={mod.LOCK_GRID_USD[i-1]} ({totals[i-1]}) "
            f"to lock={mod.LOCK_GRID_USD[i]} ({totals[i]}) - should be monotonic"
        )


def test_project_handles_zero_pnl_as_loser():
    """pnl == 0 is treated as a loser (never boosted)."""
    trades = [_trade("XAUUSDm", 0.0), _trade("XAUUSDm", 0.01)]
    proj, series = _project(trades, 0.10)
    assert series == [0.0, 0.10]
    assert proj["n_dust_boosted"] == 1


def test_project_never_makes_losers_positive():
    losers = [_trade("XAUUSDm", p) for p in (-0.50, -1.20, -0.05, -3.40)]
    for lock_usd in (0.05, 0.20, 0.50, 1.00, 1.50):
        _, series = _project(losers, lock_usd)
        for orig, new in zip(losers, series):
            assert new <= 0, f"lock_usd={lock_usd} turned a loser positive: {orig['pnl']} -> {new}"


def test_project_actually_flipped_only_when_baseline_negative_to_positive():
    """actually_flipped mirrors baseline sign -> projected sign, not just '>0'."""
    # Baseline = 0.10 dust + (-0.05) loss = +0.05 (already positive), NOT a flip test.
    # Use a fixture that flips: 0.10 dust + (-0.05) loss. Baseline = +0.05.
    # At lock=0.20: 0.10 < 0.20 -> 0.20. New: 0.20 - 0.05 = +0.15 (still positive, no flip).
    # So already-positive stays positive: actually_flipped=False. Use a clean negative fixture:
    # Baseline = 0.10 dust + (-0.30) loss = -0.20  -> at lock=0.20 becomes +0.20-0.30=-0.10 (still negative, no flip)
    # Baseline = 0.10 dust + (-0.05) loss = +0.05 (already positive) -> no flip possible
    # Build a NEGATIVE-to-POSITIVE: 0.05 dust + (-0.04) loss. Baseline = +0.01.
    #                                                                            ^ already positive.
    # Fixture that ACTUALLY FLIPS: 0.10 dust + 0.20 winner + (-0.30) loss = 0.00 (already breakeven).
    # Cleanest: -0.30 loss + 0.20 dust winner. Baseline = -0.10. At lock=0.30: 0.30 + (-0.30) = 0.00
    # (still not positive). At lock=0.50: 0.50 + (-0.30) = +0.20 (FLIPPED).
    base_neg = [_trade("XAUUSDm", 0.20), _trade("XAUUSDm", -0.30)]  # baseline -0.10
    proj_neg, _ = _project(base_neg, 0.0)
    assert proj_neg["actually_flipped"] is False  # baseline negative, no dust boosted
    proj_flip, _ = _project(base_neg, 0.50)
    assert proj_flip["actually_flipped"] is True  # boosted beyond loss -> sign actually changed

    base_pos = [_trade("XAUUSDm", 0.50), _trade("XAUUSDm", -0.20)]  # baseline +0.30 (already positive)
    proj_pos, _ = _project(base_pos, 0.0)
    assert proj_pos["actually_flipped"] is False  # was already positive
    proj_pos_more, _ = _project(base_pos, 0.30)
    assert proj_pos_more["actually_flipped"] is False  # still didn't flip sign (was positive, still positive)


# ---------------------------------------------------------------- <<< _global_flip


def test_global_flip_finds_smallest_lock_to_positive_exact_pin():
    """8 x $0.05 dust winners + 2 x -$0.50 losers. Math is deterministic.
    lock=0.05 -> no boost (0<0.05 strict-less fails) -> -$0.60
    lock=0.10 -> boost 8 to $0.10 -> 0.80 - 1.00 = -$0.20
    lock=0.15 -> boost 8 to $0.15 -> 1.20 - 1.00 = +$0.20  ==> FLIP
    """
    rng_trades = [_trade("XAUUSDm", 0.05)] * 8 + [_trade("XAUUSDm", -0.50)] * 2
    g = _global(rng_trades)
    assert g["baseline_total_usd"] < 0
    assert g["smallest_flip_lock_usd"] == 0.15
    assert g["actually_flipped"] is True


def test_global_flip_already_positive_picks_lowest_lock_with_tie_preference():
    """When baseline is already positive, best_lock_usd is the LOWEST grid value
    that produces the same projected total (since projected is monotonic
    non-decreasing). For [0.50, -0.20] baseline +0.30: at lock<0.50 nothing
    boosted (pnl=0.50 > lock), projected stays +0.30. At lock=0.50: 0.50 < 0.50
    is False (strict less) -> NOT boosted (tx stays at 0.50 since equal). At
    lock=0.60: 0.50 < 0.60 -> boosted to 0.60. So best_lock_usd = 0.60 in the
    v3 monotonic sense (we pick the first that TIES the maximum).
    """
    rng_trades = [_trade("XAUUSDm", 0.50), _trade("XAUUSDm", -0.20)]
    g = _global(rng_trades)
    assert g["baseline_total_usd"] == pytest.approx(0.30)
    # best_lock = the smallest grid value that achieves the MAX projected total.
    # Lock=0.60 boosts the 0.50 winner to 0.60 -> +0.40. Subsequent locks
    # either match or fall back to max-projection tracker using strict >.
    assert g["best_lock_usd"] >= 0.60
    assert g["projected_total_usd"] >= 0.30  # never below baseline
    assert g["smallest_flip_lock_usd"] is None  # baseline wasn't negative
    assert g["actually_flipped"] is False


def test_global_flip_no_grid_value_flips():
    """Heavy-loss window — no grid value flips it positive."""
    rng_trades = [_trade("XAUUSDm", -5.00)] * 10 + [_trade("XAUUSDm", 0.05)] * 4
    g = _global(rng_trades)
    assert g["baseline_total_usd"] < 0
    assert g["smallest_flip_lock_usd"] is None
    assert g["actually_flipped"] is False


# ---------------------------------------------------------------- <<< _per_symbol_proposal


def test_per_symbol_proposal_excludes_thin_symbols():
    trades = (
        [_trade("XAUUSDm", 0.10)] * 8
        + [_trade("XAUUSDm", -0.50)] * 4
        + [_trade("US500m", 0.05)] * 3  # below MIN_N=8
    )
    proposals = mod._per_symbol_proposal(trades, _fake_log())
    symbols = {p["symbol"] for p in proposals}
    assert "XAUUSDm" in symbols
    assert "US500m" not in symbols
    for p in proposals:
        assert p["n"] >= 8
        assert p["best_lock_usd"] >= 0.0
        assert isinstance(p["flipped_positive"], bool)
        assert isinstance(p["actually_flipped"], bool)


def test_per_symbol_proposal_reports_floor_above_loss():
    trades = (
        [_trade("XAUUSDm", 0.05)] * 10
        + [_trade("XAUUSDm", -0.50)] * 5
    )
    proposals = mod._per_symbol_proposal(trades, _fake_log())
    xau = next(p for p in proposals if p["symbol"] == "XAUUSDm")
    if xau["best_lock_usd"] >= abs(xau["avg_loss_usd"]):
        assert xau["floor_above_loss"] is False
    else:
        assert xau["floor_above_loss"] is True


def test_per_symbol_min_n_can_be_lowered():
    trades = [_trade("US500m", -0.10)] * 3 + [_trade("US500m", 0.05)] * 2
    proposals_default = mod._per_symbol_proposal(trades, _fake_log())
    assert proposals_default == []  # MIN_N=8 excludes n=5
    proposals_low = mod._per_symbol_proposal(trades, _fake_log(), min_n=3)
    assert any(p["symbol"] == "US500m" for p in proposals_low)


def test_per_symbol_actually_flipped_distinct_from_flipped_positive():
    """When baseline is already positive, flipped_positive=True but actually_flipped=False.
    n must be >= MIN_N_PER_SYMBOL (8) for the symbol to appear in proposals."""
    rng = [_trade("XAUUSDm", p) for p in [0.50, -0.10, 0.80, 0.05, 0.40, 0.30, -0.20, 0.40, 0.15]]
    # baseline = 0.50-0.10+0.80+0.05+0.40+0.30-0.20+0.40+0.15 = +2.30 (already positive)
    assert sum(t["pnl"] for t in rng) > 0
    proposals = mod._per_symbol_proposal(rng, _fake_log())
    assert len(proposals) == 1
    xau = proposals[0]
    assert xau["symbol"] == "XAUUSDm"
    assert xau["actually_flipped"] is False  # baseline already positive
    assert xau["flipped_positive"] is True


# ---------------------------------------------------------------- <<< _ascii_curve


def test_ascii_curve_returns_frame_with_markers():
    base = [0.10, -0.20, 0.15, -0.30, 0.25, -0.10]
    proj = [0.40, -0.20, 0.15, -0.30, 0.25, -0.05]
    out = mod._ascii_curve(base, proj, width=40, height=10)
    assert isinstance(out, str)
    assert out.startswith("+")
    assert "B = baseline" in out and "P = projected" in out
    assert "B" in out and "P" in out


def test_ascii_curve_handles_too_few_points():
    out = mod._ascii_curve([1.0], [1.0])
    assert out == "<too few points>"


# ---------------------------------------------------------------- <<< end-to-end run


def test_end_to_end_call_via_main_wrapper_does_not_block():
    """Smoke: import-and-call without invoking SystemExit. We just assert the
    helper-pair (baseline + projection) works on a tiny synthetic window."""
    tiny = [_trade("XAUUSDm", p) for p in (0.05, -0.30, 0.10, -0.20, 0.04, -0.10, 0.03, -0.40)]
    base = mod._baseline(tiny)
    proj, _ = _project(tiny, 0.05)
    # baseline total = 0.05+(-0.30)+0.10+(-0.20)+0.04+(-0.10)+0.03+(-0.40) = -0.78
    assert base["total_pnl_usd"] == pytest.approx(-0.78)
    # dust winners (0<x<0.05): 0.04 and 0.03 -> boosted to 0.05
    # new: 0.05, -0.30, 0.10, -0.20, 0.05, -0.10, 0.05, -0.40 = -0.75
    assert proj["projected_total_pnl_usd"] == pytest.approx(-0.75)
    assert proj["n_dust_boosted"] == 2
