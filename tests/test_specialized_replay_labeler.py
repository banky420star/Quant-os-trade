"""Tests for the specialized-setup historical-replay labeler (iteration 11)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import specialized_setups
from core.specialized_replay_labeler import (
    _bar_hour_override,
    _bar_utc_hour,
    _replay_config,
    aggregate_cells,
    _bootstrap_ci95,
    replay_symbol,
)


# --- _bar_utc_hour ---

def test_bar_utc_hour_tz_aware():
    import pandas as pd
    bar = {"time": pd.Timestamp("2025-11-12 14:05:00", tz="UTC")}
    assert _bar_utc_hour(bar) == 14


def test_bar_utc_hour_naive_localizes():
    import pandas as pd
    bar = {"time": pd.Timestamp("2025-11-12 07:30:00")}
    assert _bar_utc_hour(bar) == 7


def test_bar_utc_hour_missing_is_unknown():
    assert _bar_utc_hour({"time": None}) == -1


# --- _bar_hour_override ---

def test_bar_hour_override_overrides_and_restores():
    original = specialized_setups.utc_hour
    with _bar_hour_override(14):
        assert specialized_setups.utc_hour() == 14
    assert specialized_setups.utc_hour is original


def test_bar_hour_override_restores_on_exception():
    original = specialized_setups.utc_hour
    with pytest.raises(RuntimeError):
        with _bar_hour_override(8):
            assert specialized_setups.utc_hour() == 8
            raise RuntimeError("boom")
    assert specialized_setups.utc_hour is original


def test_override_drives_real_detector_window():
    """A rejection feat at hour 14 fires silver_bullet; the same feat at hour 3 does not."""
    feat = {
        "symbol": "XAUUSDm", "price": 100.0,
        "support": 99.0, "resistance": 101.0,
        "rejection": "bullish_rejection", "breakout": "none",
        "bb_position": 0.5,
        "m5_trend": "bullish", "m15_trend": "bullish", "timeframe_alignment": True,
        "stoch_k": 60.0, "stoch_d": 55.0, "stoch_cross": "none",
        "atr": 1.0, "atr_ratio": 0.01, "volume_avg": 1000.0, "volume_ratio": 1.0,
        "bb_upper": 102.0, "bb_middle": 100.0, "bb_lower": 98.0,
        "volatility_regime": "normal",
    }
    ev = {"trend": 0.7, "momentum": 0.5, "structure": 0.8, "liquidity": 0.75,
          "volatility": 0.6, "volume": 0.6, "risk": 0.3}
    cfg = {"signals": {"specialized_setups": {"enabled": True, "shadow": False}}}

    with _bar_hour_override(14):
        fires = specialized_setups.detect_specialized(feat, {"symbol": "XAUUSDm"}, ev, cfg)
    assert any(f["setup_type"] == "silver_bullet" for f in fires)

    with _bar_hour_override(3):
        fires = specialized_setups.detect_specialized(feat, {"symbol": "XAUUSDm"}, ev, cfg)
    assert not any(f["setup_type"] == "silver_bullet" for f in fires)


# --- _replay_config ---

def test_replay_config_forces_all_setups_all_symbols_no_shadow():
    cfg = _replay_config({"signals": {"specialized_setups": {
        "enabled": False, "shadow": True,
        "symbols": ["XAUUSDm"], "setups": ["silver_bullet"],
        "per_symbol": {"XAUUSDm": ["silver_bullet"]},
    }}})
    sa = cfg["signals"]["specialized_setups"]
    assert sa["enabled"] is True
    assert sa["shadow"] is False
    assert sa["symbols"] == []
    assert sa["setups"] == []
    assert sa["per_symbol"] == {}


def test_replay_config_does_not_mutate_original():
    original = {"signals": {"specialized_setups": {"enabled": False, "shadow": False}}}
    _replay_config(original)
    assert original["signals"]["specialized_setups"]["enabled"] is False
    assert original["signals"]["specialized_setups"]["shadow"] is False


# --- _bootstrap_ci95 ---

def test_bootstrap_ci95_all_positive_excludes_zero():
    rs = [1.0] * 10 + [2.0] * 10
    ci = _bootstrap_ci95(rs, reps=200)
    assert ci is not None
    assert ci[0] > 0


def test_bootstrap_ci95_too_few_returns_none():
    assert _bootstrap_ci95([0.5]) is None


# --- aggregate_cells ---

def test_aggregate_empty():
    r = aggregate_cells([])
    assert r["total_fires"] == 0
    assert r["distinct_cells"] == 0
    assert r["cells"] == []
    assert "SELECTION-BIASED" in r["caveat"]


def test_aggregate_cells_stats_thin_sort_and_ci():
    rows = (
        [{"symbol": "XAUUSDm", "setup_type": "silver_bullet", "r_multiple": 2.0}] * 10
        + [{"symbol": "XAUUSDm", "setup_type": "silver_bullet", "r_multiple": -1.0}] * 2
        + [{"symbol": "EURUSDm", "setup_type": "london_judas", "r_multiple": -1.0}] * 3
    )
    r = aggregate_cells(rows, min_n=8)
    assert r["total_fires"] == 15
    assert r["distinct_cells"] == 2
    assert r["thin_cells"] == 1
    sb = next(c for c in r["cells"] if c["setup_type"] == "silver_bullet")
    assert sb["n"] == 12
    assert sb["wins"] == 10
    assert sb["win_rate_pct"] == round(100 * 10 / 12, 1)
    assert sb["expectancy_r"] == round((20.0 - 2.0) / 12, 4)
    assert sb["thin"] is False
    assert sb["ci95_excludes_zero"] is True
    lj = next(c for c in r["cells"] if c["setup_type"] == "london_judas")
    assert lj["thin"] is True
    # sorted by expectancy descending -> silver_bullet (positive) first
    assert r["cells"][0]["setup_type"] == "silver_bullet"


# --- replay_symbol end-to-end with synthetic bars + patched detector ---

def _m5_bars():
    """70 M5 bars. Bars 0..60 have low=99.0 (so support=99 for every fire
    window); bars 61..69 have low=99.5 so the forward bars' low stays above SL.
    All bars high=103, close=100 -> resistance=103, price=100, risk=1, tp=102.
    Forward bars high=103 >= tp 102 and low=99.5 > sl 99 -> TP at +2R."""
    import pandas as pd
    bars = []
    t0 = pd.Timestamp("2025-11-12 14:00", tz="UTC")
    for k in range(70):
        low = 99.0 if k <= 60 else 99.5
        bars.append({
            "time": t0 + pd.Timedelta(minutes=5 * k),
            "open": 100.0, "high": 103.0, "low": low, "close": 100.0,
            "volume": 1000.0,
        })
    return bars


def _m15_bars():
    import pandas as pd
    bars = []
    t0 = pd.Timestamp("2025-11-12 13:00", tz="UTC")
    for k in range(40):
        bars.append({
            "time": t0 + pd.Timedelta(minutes=15 * k),
            "open": 100.0, "high": 103.0, "low": 99.0, "close": 100.0,
            "volume": 1000.0,
        })
    return bars


def test_replay_symbol_labels_fires(monkeypatch):
    # Force a BUY fire on every eligible bar; the real FeatureEngine supplies
    # support/resistance/price from the synthetic bars.
    from core import specialized_replay_labeler as mod

    def fake_detect(feat, ctx, ev, cfg, logger=None):
        return [{"setup_type": "silver_bullet", "side": "BUY",
                 "setup_confidence": 0.7, "reason": "synthetic"}]

    monkeypatch.setattr(mod, "detect_specialized", fake_detect)
    monkeypatch.setattr(mod, "load_bars",
                        lambda sym, tf="M5": _m5_bars() if tf == "M5" else _m15_bars())
    monkeypatch.setattr(mod, "_m15_window_up_to", lambda m15, t, limit=500: m15[-60:])

    rows = replay_symbol("XAUUSDm", {}, max_bars=20, forward_bars=6, tp_r=2.0)
    assert len(rows) >= 1
    for r in rows:
        assert r["setup_type"] == "silver_bullet"
        assert r["side"] == "BUY"
        assert r["r_multiple"] == 2.0
        assert r["exit_reason"] == "tp"
        assert r["bars_held"] >= 1
        assert r["symbol"] == "XAUUSDm"


def test_replay_symbol_no_history_returns_empty(monkeypatch):
    from core import specialized_replay_labeler as mod
    monkeypatch.setattr(mod, "load_bars", lambda sym, tf="M5": [])
    assert replay_symbol("NOPEm", {}) == []

# --- iteration 17: S/R off-by-one fix (exclude current bar) ---

def test_vectorized_breakout_state_fires_when_close_crosses_prior_resistance():
    """Regression for the S/R off-by-one bug: with rolling S/R INCLUDING the
    current bar, `close > resistance` is impossible (close <= high <= rolling
    max) so breakout/breakdown NEVER fired. With shift(1) (prior bars only), a
    close beyond the prior 50-bar high/low produces the breakout/breakdown state.
    """
    import pandas as pd
    from core.specialized_replay_labeler import _vectorized_features
    bars = []
    t0 = pd.Timestamp("2025-11-12 14:00", tz="UTC")
    # 60 bars: high=100, close=99 -> prior resistance (shift(1) rolling 50 max) = 100
    for k in range(60):
        bars.append({"time": t0 + pd.Timedelta(minutes=5 * k),
                     "open": 99.0, "high": 100.0, "low": 98.0, "close": 99.0, "volume": 1000.0})
    # bar 60: close=101 crosses above prior resistance 100 -> breakout
    bars.append({"time": t0 + pd.Timedelta(minutes=5 * 60),
                 "open": 99.0, "high": 101.5, "low": 99.0, "close": 101.0, "volume": 1000.0})
    m15 = [{"time": t0, "open": 99.0, "high": 100.0, "low": 98.0, "close": 99.0, "volume": 1000.0}]
    f = _vectorized_features(bars, m15)
    bo = list(f["breakout"])
    # the last bar must be "breakout" (close 101 > prior resistance 100)
    assert bo[-1] == "breakout", f"expected breakout at last bar, got {bo[-1]!r}; full tail={bo[-3:]}"
    # no bar before the cross should be breakout (close 99 <= resistance 100)
    assert "breakout" not in bo[:-1]


def test_vectorized_breakdown_state_fires_when_close_crosses_prior_support():
    import pandas as pd
    from core.specialized_replay_labeler import _vectorized_features
    bars = []
    t0 = pd.Timestamp("2025-11-12 14:00", tz="UTC")
    # 60 bars: low=100, close=101 -> prior support (shift(1) rolling 50 min) = 100
    for k in range(60):
        bars.append({"time": t0 + pd.Timedelta(minutes=5 * k),
                     "open": 101.0, "high": 102.0, "low": 100.0, "close": 101.0, "volume": 1000.0})
    # bar 60: close=99 crosses below prior support 100 -> breakdown
    bars.append({"time": t0 + pd.Timedelta(minutes=5 * 60),
                 "open": 101.0, "high": 101.0, "low": 98.5, "close": 99.0, "volume": 1000.0})
    m15 = [{"time": t0, "open": 101.0, "high": 102.0, "low": 100.0, "close": 101.0, "volume": 1000.0}]
    f = _vectorized_features(bars, m15)
    bo = list(f["breakout"])
    assert bo[-1] == "breakdown", f"expected breakdown at last bar, got {bo[-1]!r}"
    assert "breakdown" not in bo[:-1]
