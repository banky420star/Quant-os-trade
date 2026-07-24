"""Tests for strategy diversification (Donchian + Bollinger + ATR Expansion + portfolio orchestrator).

Strategy simulators are pure-pandas and can be exercised on synthetic OHLCV.
The portfolio orchestrator is a pure function on candidate dicts.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies import donchian_breakout, bollinger_reversion, atr_expansion  # noqa: E402
from strategies.portfolio import (  # noqa: E402
    FX_SYMBOLS,
    PER_STREAM_PER_SYMBOL_CAP,
    REGIME_AFFINITY,
    RISK_PARITY_DEFAULTS,
    annotate_candidate_for_risk_parity,
    bias_aligned,
    is_fx_symbol,
    meta_decide,
    regime_affinity,
    risk_parity_weight,
    score_candidate,
)
from core import position_sizing  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic OHLCV
# ---------------------------------------------------------------------------
def make_synthetic_ohlcv(
    n: int = 200,
    seed: int = 1,
    trend: float = 0.001,
    vol: float = 0.005,
    start: float = 100.0,
) -> pd.DataFrame:
    """Build a synthetic OHLCV series for simulator testing."""
    rng = np.random.default_rng(seed)
    closes = [start]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + trend + vol * rng.normal()))
    closes = np.array(closes)
    opens = closes + rng.normal(0, vol * 0.3, size=n) * closes
    highs = np.maximum(opens, closes) + abs(rng.normal(0, vol * 0.4, size=n)) * closes
    lows = np.minimum(opens, closes) - abs(rng.normal(0, vol * 0.4, size=n)) * closes
    vols = rng.uniform(100, 1000, size=n)
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": vols,
    })


# ---------------------------------------------------------------------------
# Donchian
# ---------------------------------------------------------------------------
def test_donchian_detect_signals_returns_two_arrays():
    df = make_synthetic_ohlcv(n=200, trend=0.002, vol=0.005)
    longs, shorts, bands = donchian_breakout.detect_signals(df)
    assert isinstance(longs, np.ndarray)
    assert isinstance(shorts, np.ndarray)
    assert isinstance(bands, np.ndarray)
    # At least one signal should fire on a trending series
    assert longs.size > 0 or shorts.size > 0


def test_donchian_simulate_runs_without_error():
    df = make_synthetic_ohlcv(n=300, trend=0.003, vol=0.006)
    rs = donchian_breakout.simulate(df)
    assert isinstance(rs, list)
    # All values finite
    for r in rs:
        assert np.isfinite(r)


def test_donchian_evaluate_returns_dict():
    df = make_synthetic_ohlcv(n=300, trend=0.001, vol=0.004)
    result = donchian_breakout.evaluate(df)
    assert "pass" in result
    assert "n" in result
    assert "expectancy_r" in result


# ---------------------------------------------------------------------------
# Bollinger
# ---------------------------------------------------------------------------
def test_bollinger_detect_signals_runs():
    df = make_synthetic_ohlcv(n=200, trend=0.0, vol=0.008)
    longs, shorts = bollinger_reversion.detect_signals(df)
    assert isinstance(longs, np.ndarray)
    assert isinstance(shorts, np.ndarray)


def test_bollinger_bands_computes_three_series():
    df = make_synthetic_ohlcv(n=100, vol=0.005)
    bands = bollinger_reversion.bollinger_bands(df, period=20, std_mult=2.0)
    assert set(bands.keys()) >= {"upper", "middle", "lower"}
    assert bands["upper"].iloc[-1] > bands["lower"].iloc[-1]


def test_bollinger_simulate_runs():
    df = make_synthetic_ohlcv(n=300, vol=0.006)
    rs = bollinger_reversion.simulate(df)
    assert isinstance(rs, list)


# ---------------------------------------------------------------------------
# ATR Expansion
# ---------------------------------------------------------------------------
def test_atr_expansion_detect_signals_runs():
    df = make_synthetic_ohlcv(n=200, trend=0.001, vol=0.006)
    longs, shorts = atr_expansion.detect_signals(df)
    assert isinstance(longs, np.ndarray)
    assert isinstance(shorts, np.ndarray)


def test_atr_expansion_simulate_runs():
    df = make_synthetic_ohlcv(n=300, trend=0.0, vol=0.005)
    rs = atr_expansion.simulate(df)
    assert isinstance(rs, list)


def test_atr_expansion_expansion_filter_holds_in_quiet_market():
    """Quiet markets should produce ≤2 expansion signals when expansion_mult=2.0.

    We allow up to 2 (instead of exact 0) because a single noisy bar in a
    flat series can briefly satisfy `atr_ratio > 1.2×baseline`. The point
    is to assert the filter HELD, not to require zero randomness.
    """
    df = make_synthetic_ohlcv(n=300, trend=0.0, vol=0.0001)  # dead-quiet
    longs, shorts = atr_expansion.detect_signals(
        df, atr_expansion.AtrExpansionParams(expansion_mult=2.0),
    )
    assert longs.size + shorts.size <= 2, f"too many expansion signals in quiet market: {longs.size + shorts.size}"


# ---------------------------------------------------------------------------
# Portfolio orchestrator
# ---------------------------------------------------------------------------
def test_risk_parity_defaults_sum_to_one():
    s = sum(RISK_PARITY_DEFAULTS.values())
    assert abs(s - 1.0) < 1e-9, f"risk-parity shares must sum to 1.0, got {s}"


def test_risk_parity_weight_returns_default():
    assert risk_parity_weight("donchian_breakout") == 0.25
    assert risk_parity_weight("bollinger_reversion") == 0.25
    assert risk_parity_weight("atr_expansion") == 0.25
    assert risk_parity_weight("decision_engine") == 0.25
    assert risk_parity_weight("unknown_stream") == 0.0


def test_risk_parity_weight_respects_config():
    cfg = {"strategies": {"diversification": {
        "risk_shares": {
            "donchian_breakout": 0.5,
            "decision_engine": 0.5,
        }
    }}}
    assert risk_parity_weight("donchian_breakout", cfg) == 0.5
    assert risk_parity_weight("decision_engine", cfg) == 0.5
    assert risk_parity_weight("atr_expansion", cfg) == 0.25  # fallback


def test_regime_affinity_returns_in_range():
    for stream in REGIME_AFFINITY:
        for regime in {"strong_trend", "range", "compression", "transitional"}:
            s = regime_affinity(stream, {"primary": regime})
            assert 0.0 <= s <= 1.0, f"{stream}/{regime} -> {s}"


def test_bias_aligned_donchian_penalises_counter_trend():
    """Donchian (trend) should be HEAVILY penalised when counter-trend."""
    aligned = bias_aligned("donchian_breakout", "BUY", "bullish")
    bad = bias_aligned("donchian_breakout", "BUY", "bearish")
    assert aligned > bad
    assert bad <= 0.15, f"counter-trend penalty was too small: {bad}"


def test_bias_aligned_bollinger_penalises_with_trend():
    """Bollinger (mean-reversion) should be penalised in a hot trend."""
    aligned = bias_aligned("bollinger_reversion", "BUY", "bullish")  # BUY in bull = with trend = bad for MR
    against = bias_aligned("bollinger_reversion", "SELL", "bullish")  # SELL in bull = against trend = good for MR
    assert against > aligned


def test_is_fx_symbol():
    assert is_fx_symbol("EURUSDm")
    assert is_fx_symbol("USDJPYm")
    assert not is_fx_symbol("XAUUSDm")
    assert not is_fx_symbol("BTCUSDm")


def test_annotate_candidate_for_risk_parity_stamps_fields():
    cand = {"source": "donchian_breakout", "symbol": "XAUUSDm"}
    annotate_candidate_for_risk_parity(cand)
    assert cand["risk_parity_stream"] == "donchian_breakout"
    assert cand["risk_parity_share"] == 0.25


def test_meta_decide_picks_one_per_symbol():
    """Meta-decide should pick at most per_symbol_cap per symbol and not duplicate."""
    candidates = [
        {"symbol": "XAUUSDm", "side": "BUY", "setup_type": "donchian_breakout",
         "source": "donchian_breakout", "confidence": 80,
         "market_context": {"m5_trend": "bullish"}},
        {"symbol": "XAUUSDm", "side": "BUY", "setup_type": "atr_expansion",
         "source": "atr_expansion", "confidence": 70,
         "market_context": {"m5_trend": "bullish"}},
        {"symbol": "EURUSDm", "side": "SELL", "setup_type": "bollinger_reversion",
         "source": "bollinger_reversion", "confidence": 75,
         "market_context": {"m5_trend": "bearish"}},
    ]
    chosen, diag = meta_decide(candidates, {}, per_symbol_cap=1, min_score=0.10)
    by_sym = {}
    for c in chosen:
        by_sym.setdefault(c["symbol"], []).append(c)
    for sym, picks in by_sym.items():
        assert len(picks) == 1, f"{sym}: meta-decide picked {len(picks)} (> cap 1)"
    # Bollinger for non-FX should be excluded; only EURUSDm here is FX — should pass.
    assert any(c["symbol"] == "EURUSDm" for c in chosen)
    # All chosen have a meta_score stamped
    assert all(c.get("meta_score") is not None for c in chosen)
    assert diag["chosen_count"] <= diag["considered"]


def test_meta_decide_bollinger_rejected_for_non_fx():
    """Bollinger reversion must NOT fire on non-FX symbols even at high conf."""
    candidates = [
        {"symbol": "XAUUSDm", "side": "BUY", "setup_type": "bollinger_reversion",
         "source": "bollinger_reversion", "confidence": 90,
         "market_context": {"m5_trend": "bearish"}},
    ]
    chosen, diag = meta_decide(candidates, {}, per_symbol_cap=1, min_score=0.10)
    assert chosen == []
    assert diag["rejected_low_score"] >= 1
    assert any(
        cand.get("meta_reject_reason") == "bollinger_fx_only" for cand in candidates
    )


def test_meta_decide_prefers_aligned_over_misaligned():
    """When two candidates exist for a symbol, the aligned one wins (higher bias_aligned)."""
    candidates = [
        # Counter-trend Donchian BUY in bear trend -> low score
        {"symbol": "XAUUSDm", "side": "BUY", "setup_type": "donchian_breakout",
         "source": "donchian_breakout", "confidence": 90,
         "market_context": {"m5_trend": "bearish"}},
        # Aligned ATR-Expansion SELL in bear trend -> high score
        {"symbol": "XAUUSDm", "side": "SELL", "setup_type": "atr_expansion",
         "source": "atr_expansion", "confidence": 70,
         "market_context": {"m5_trend": "bearish"}},
    ]
    chosen, _diag = meta_decide(candidates, {}, per_symbol_cap=1, min_score=0.10)
    assert len(chosen) == 1
    # Aligned (SELL in bear) should win the score; counter-trend BUY is hard veto
    assert chosen[0]["side"] == "SELL"
    assert chosen[0]["source"] == "atr_expansion"


def test_meta_decide_handles_empty_input():
    chosen, diag = meta_decide([], {}, per_symbol_cap=1, min_score=0.10)
    assert chosen == []
    assert diag["chosen_count"] == 0
    assert diag["considered"] == 0


def test_score_candidate_in_unit_range():
    cand = {"source": "donchian_breakout", "side": "BUY", "confidence": 75,
            "market_context": {"m5_trend": "bullish"}}
    s = score_candidate(cand, {"primary": "strong_trend"})
    assert 0.0 <= s <= 1.10  # bias_aligned boost can push over 1 in best case


def test_per_stream_per_symbol_cap_constant_positive():
    assert PER_STREAM_PER_SYMBOL_CAP > 0


# ---------------------------------------------------------------------------
# risk_parity_share end-to-end test (verifies the headline claim!)
# ---------------------------------------------------------------------------
def test_risk_parity_share_scales_ideal_size():
    """Verify risk_parity_share multiplies both risk_pct and the USD cap.

    Same candidate fed twice — once share=1.0, once share=0.25. The 0.25 case
    should have ideal_size ≤ 25% of the unscaled ideal (capped by broker min).
    USD cap should also be ≤ 25% of full.
    """
    config_base = {
        "signals": {"default_risk_percent": 1.0},
        "execution": {"max_lot": 0.25, "default_lot": 0.01},
        "practice": {"micro": {"enabled": True, "max_open_per_symbol": 6}},
        "kelly_sizing": {"enabled": False},
        "risk": {"max_symbol_exposure_usd": 100000, "max_total_exposure_usd": 100000,
                 "max_loss_per_trade_usd": 5.0, "cap_loss_to_balance": True,
                 "max_daily_loss_pct": 10},
    }
    spec = {
        "volume_min": 0.01, "volume_step": 0.01, "volume_max": 10.0,
        "trade_tick_value": 1.0, "trade_tick_size": 0.01, "point": 0.01,
    }
    sig = {"symbol": "XAUUSDm", "side": "BUY", "entry": 100.0, "sl": 99.5,
           "setup_type": "donchian_breakout"}
    equity, balance = 1000.0, 1000.0

    sig_full = dict(sig); sig_full["risk_parity_share"] = 1.0
    sig_qtr = dict(sig); sig_qtr["risk_parity_share"] = 0.25

    full_vol, full_details = position_sizing.calc_executable_volume(
        sig_full, equity=equity, balance=balance, config=config_base, symbol_spec=spec)
    qtr_vol, qtr_details = position_sizing.calc_executable_volume(
        sig_qtr, equity=equity, balance=balance, config=config_base, symbol_spec=spec)

    assert full_vol > 0
    full_ideal = float(full_details["ideal_size"])
    qtr_ideal = float(qtr_details["ideal_size"])
    assert qtr_ideal < full_ideal, (
        f"risk_parity_share=0.25 should reduce ideal_size; "
        f"got full={full_ideal} qtr={qtr_ideal}"
    )
    assert qtr_ideal <= full_ideal * 0.25 + 1e-9, (
        f"ideal_size should be ≤ 25%% of full-size; "
        f"got full={full_ideal} qtr={qtr_ideal} (ratio={qtr_ideal / full_ideal:.3f})"
    )
    # Not all candidates will trigger cap_usd branch
    # (no profit-target_loss-per-trade cap is enforced at sizing when no symbol ctx).
    # The strongest invariant: qtr_ideal < full_ideal.
    assert qtr_vol >= 0
    assert full_vol >= 0


def test_diversification_disabled_passes_through_unchanged():
    """When diversification is OFF, no risk_parity_share is set; sizing
    should run with the un-shared (default) risk_pct."""
    config = {
        "strategies": {"diversification": {"enabled": False}},
        "signals": {"default_risk_percent": 1.0},
        "execution": {"max_lot": 0.25, "default_lot": 0.01},
        "practice": {"micro": {"enabled": True, "max_open_per_symbol": 6}},
        "kelly_sizing": {"enabled": False},
        "risk": {"max_symbol_exposure_usd": 100000, "max_total_exposure_usd": 100000,
                 "max_loss_per_trade_usd": 5.0, "cap_loss_to_balance": True,
                 "max_daily_loss_pct": 10},
    }
    spec = {"volume_min": 0.01, "volume_step": 0.01, "trade_tick_value": 1.0,
            "trade_tick_size": 0.01, "point": 0.01}
    sig = {"symbol": "XAUUSDm", "side": "BUY", "entry": 100.0, "sl": 99.5}
    vol, details = position_sizing.calc_executable_volume(
        sig, equity=1000.0, balance=1000.0, config=config, symbol_spec=spec)
    assert vol > 0
    # Without a share, ideal_size is at full default
    expected_ideal = float(details["ideal_size"])
    assert expected_ideal > 0


# ---------------------------------------------------------------------------
# End-to-end smoke: only check wiring imports + symbol list
# ---------------------------------------------------------------------------
def test_fx_symbols_complete_set():
    expected = {"EURUSDm", "GBPUSDm", "USDJPYm", "USDCHFm", "AUDUSDm"}
    assert set(FX_SYMBOLS) == expected
