"""Tests for the 4 research setups (originally shadow-only trials 2026-08-04,
FLIPPED LIVE 2026-08-04 per user request to grow the usable arsenal).

Covers:
  * Registry membership — the 4 specs are registered and are NOT shadow_only
    (they emit live candidates when enabled, scored via the live culturing
    ledger; replay-veto parity intentionally absent, apply_replay_veto OFF).
  * detect_specialized: the 4 specs EMIT candidates when enabled=true (the
    PASS-14 flip from shadow_only=True -> live emission).
  * FeatureEngine state computation: _cvd_divergence (bearish/bullish),
    _session_volume_profile_state (poc_rejection / va_breakout),
    _intermarket_zero_cross (cross-symbol zero-crossing).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import pytest

from core import specialized_setups as spec_mod
from core.feature_engine import FeatureEngine
from core.specialized_setups import (
    SPECIALIZED_SETUPS,
    detect_specialized,
)

NEW4 = [
    "cvd_divergence_reversal",
    "intermarket_divergence_zero_cross",
    "session_volume_profile_poc_rejection",
    "session_volume_profile_va_breakout",
]


def _cfg(enabled=True, shadow=True):
    return {
        "signals": {
            "specialized_setups": {
                "enabled": enabled,
                "shadow": shadow,
                "symbols": [],
                "setups": [],
                "per_symbol": {},
                "apply_replay_veto": False,
            }
        }
    }


def _feat(overrides=None):
    f = {
        "symbol": "XAUUSDm",
        "price": 4000.0,
        "m5_trend": "neutral",
        "rejection": "none",
        "breakout": "none",
        "bb_position": 0.5,
    }
    if overrides:
        f.update(overrides)
    return f


def _ev(overrides=None):
    e = {
        "liquidity": 0.7,
        "structure": 0.6,
        "momentum": 0.6,
        "volume": 0.6,
        "trend": 0.5,
    }
    if overrides:
        e.update(overrides)
    return e


# --- registry --------------------------------------------------------------


def test_new4_registered_live():
    """PASS-14: the 4 research specs are registered and NOT shadow_only — they
    emit live candidates when enabled (flipped from shadow-only trials)."""
    names = {s["name"]: s for s in SPECIALIZED_SETUPS}
    for name in NEW4:
        assert name in names, f"{name} missing from registry"
        assert not names[name].get("shadow_only"), f"{name} still shadow_only (PASS-14 flipped it live)"


def test_new4_emit_when_enabled(monkeypatch):
    """PASS-14: the (no-longer-shadow) research specs EMIT candidates when
    enabled=true. They are still logged to the shadow ledger when the global
    shadow flag is on (the global shadow path logs all fires)."""
    logged: list[dict] = []
    monkeypatch.setattr(spec_mod, "_shadow_log", lambda fires, feat, ctx: logged.extend(fires))
    out = detect_specialized(
        _feat({"cvd_divergence": "bearish_divergence"}),
        {"symbol": "XAUUSDm"},
        _ev(),
        _cfg(enabled=True, shadow=True),
    )
    types = [c["setup_type"] for c in out]
    assert "cvd_divergence_reversal" in types  # now emitted (was [] when shadow_only)
    assert "cvd_divergence_reversal" in [c["setup_type"] for c in logged]  # also logged (global shadow on)


def test_new4_emit_when_global_shadow_off(monkeypatch):
    """PASS-14: with enabled=true + global shadow=false, the research specs
    EMIT candidates and are NOT logged (the shadow ledger only runs when the
    global shadow flag is on). This is the live-trading path."""
    logged: list[dict] = []
    monkeypatch.setattr(spec_mod, "_shadow_log", lambda fires, feat, ctx: logged.extend(fires))
    out = detect_specialized(
        _feat({"vp_poc_rejection": "bullish"}),
        {"symbol": "XAUUSDm"},
        _ev(),
        _cfg(enabled=True, shadow=False),
    )
    types = [c["setup_type"] for c in out]
    assert "session_volume_profile_poc_rejection" in types  # emitted (was [] when shadow_only)
    assert logged == []  # global shadow off -> no shadow logging


def test_shadow_only_ignored_when_disabled(monkeypatch):
    logged: list[dict] = []
    monkeypatch.setattr(spec_mod, "_shadow_log", lambda fires, feat, ctx: logged.extend(fires))
    out = detect_specialized(
        _feat({"intermarket_zero_cross": "bullish_cross"}),
        {"symbol": "US30m"},
        _ev(),
        _cfg(enabled=False, shadow=False),
    )
    assert out == []


def test_normal_setups_still_emit_when_enabled(monkeypatch):
    """The shadow_only path must not break the live (non-shadow) setups."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    out = detect_specialized(
        _feat({"rejection": "bullish_rejection"}),
        {"symbol": "XAUUSDm"},
        _ev(),
        _cfg(enabled=True, shadow=False),
    )
    types = {c["setup_type"] for c in out}
    assert "silver_bullet" in types  # 14:00 UTC silver bullet window


# --- detect callables (direct) ---------------------------------------------


def test_cvd_divergence_detect_sides():
    out = spec_mod._cvd_divergence_reversal(_feat({"cvd_divergence": "bearish_divergence"}), {}, _ev(), _cfg())
    assert out and out["side"] == "SELL"
    out = spec_mod._cvd_divergence_reversal(_feat({"cvd_divergence": "bullish_divergence"}), {}, _ev(), _cfg())
    assert out and out["side"] == "BUY"
    assert spec_mod._cvd_divergence_reversal(_feat(), {}, _ev(), _cfg()) is None


def test_intermarket_detect_sides():
    out = spec_mod._intermarket_divergence_zero_cross(
        _feat({"intermarket_zero_cross": "bullish_cross", "intermarket_anchor": "NAS100m"}), {}, _ev(), _cfg()
    )
    assert out and out["side"] == "BUY"
    out = spec_mod._intermarket_divergence_zero_cross(
        _feat({"intermarket_zero_cross": "bearish_cross"}), {}, _ev(), _cfg()
    )
    assert out and out["side"] == "SELL"
    assert spec_mod._intermarket_divergence_zero_cross(_feat(), {}, _ev(), _cfg()) is None


def test_vp_poc_detect_sides():
    out = spec_mod._session_volume_profile_poc_rejection(_feat({"vp_poc_rejection": "bullish"}), {}, _ev(), _cfg())
    assert out and out["side"] == "BUY"
    out = spec_mod._session_volume_profile_poc_rejection(_feat({"vp_poc_rejection": "bearish"}), {}, _ev(), _cfg())
    assert out and out["side"] == "SELL"
    assert spec_mod._session_volume_profile_poc_rejection(_feat(), {}, _ev(), _cfg()) is None


def test_vp_va_breakout_detect_sides():
    out = spec_mod._session_volume_profile_va_breakout(_feat({"vp_va_breakout": "bullish"}), {}, _ev(), _cfg())
    assert out and out["side"] == "BUY"
    out = spec_mod._session_volume_profile_va_breakout(_feat({"vp_va_breakout": "bearish"}), {}, _ev(), _cfg())
    assert out and out["side"] == "SELL"
    assert spec_mod._session_volume_profile_va_breakout(_feat(), {}, _ev(), _cfg()) is None


def test_adx_rising_trend_detect_sides():
    """adx_di_rising_trend: detect callable maps adx_trend state -> side."""
    out = spec_mod._adx_di_rising_trend(_feat({"adx_trend": "bullish_trend"}), {}, _ev(), _cfg())
    assert out and out["side"] == "BUY"
    out = spec_mod._adx_di_rising_trend(_feat({"adx_trend": "bearish_trend"}), {}, _ev(), _cfg())
    assert out and out["side"] == "SELL"
    assert spec_mod._adx_di_rising_trend(_feat(), {}, _ev(), _cfg()) is None


def test_adx_trend_state_bullish_on_uptrend():
    """Range (ADX low) then a strong RECENT uptrend -> ADX still rising + DI+ >
    DI- -> bullish_trend. A perfectly steady trend converges ADX to ~100 and
    plateaus (not strictly rising), so the trend must have started recently
    enough that ADX is still in its climbing phase at the last bar."""
    n = 60
    closes = []
    for i in range(n):
        if i < 30:
            closes.append(100.0 + (1.0 if i % 2 else 0.0))  # oscillating range
        else:
            closes.append(100.0 + 3.0 * (i - 29))  # strong recent uptrend
    opens = closes[:]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    vols = [100.0] * n
    df = _df(closes, opens=opens, highs=highs, lows=lows, vols=vols)
    eng = FeatureEngine({})
    assert eng._adx_trend_state(df) == "bullish_trend"


def test_adx_trend_state_bearish_on_downtrend():
    """Range (ADX low) then a strong RECENT downtrend -> ADX still rising + DI- >
    DI+ -> bearish_trend (mirror of the bullish construction)."""
    n = 60
    closes = []
    for i in range(n):
        if i < 30:
            closes.append(200.0 + (1.0 if i % 2 else 0.0))  # oscillating range
        else:
            closes.append(200.0 - 3.0 * (i - 29))  # strong recent downtrend
    opens = closes[:]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    vols = [100.0] * n
    df = _df(closes, opens=opens, highs=highs, lows=lows, vols=vols)
    eng = FeatureEngine({})
    assert eng._adx_trend_state(df) == "bearish_trend"


def test_adx_trend_state_flat_is_none():
    """Flat series -> ADX ~ 0/NaN, no trend -> None."""
    eng = FeatureEngine({})
    assert eng._adx_trend_state(_df([100.0] * 60)) is None


def test_adx_trend_state_needs_history():
    eng = FeatureEngine({})
    assert eng._adx_trend_state(_df([100.0] * 10)) is None


def test_ict_ote_detect_sides():
    """ict_ote: detect callable maps ote_state -> side."""
    out = spec_mod._ict_ote(_feat({"ote_state": "bullish_ote"}), {}, _ev(), _cfg())
    assert out and out["side"] == "BUY"
    out = spec_mod._ict_ote(_feat({"ote_state": "bearish_ote"}), {}, _ev(), _cfg())
    assert out and out["side"] == "SELL"
    assert spec_mod._ict_ote(_feat(), {}, _ev(), _cfg()) is None


def test_ote_state_bullish_retracement():
    """Swing low at bar 10 -> up leg to swing high at bar 40 -> monotonic
    retracement DOWN into the 62-79% zone at the last bar -> bullish_ote.
    Monotonic decline after the high ensures no new pivot low/high forms, so
    the most-recent pivot stays the high (up leg)."""
    n = 80
    closes = [0.0] * n; highs = [0.0] * n; lows = [0.0] * n
    for i in range(n):
        if i < 10:
            c = 100.0
        elif i == 10:
            c = 96.0  # dip low
        elif i <= 39:
            c = 96.0 + (i - 10) * 1.1  # monotonic up to ~127.9
        elif i == 40:
            c = 129.0  # peak
        else:
            c = 129.0 - (i - 40) * 0.63  # monotonic decline to ~105
        closes[i] = c; highs[i] = c + 0.5; lows[i] = c - 0.5
    # force the bar-10 low to be a clear swing low (dip below the flat region)
    lows[10] = 95.0; highs[10] = 96.5
    # leg = 95 -> 131 (high at bar 40 = 129.5); 62% = ~108.7, 79% = ~102.6
    df = _df(closes, opens=closes[:], highs=highs, lows=lows, vols=[100.0] * n)
    eng = FeatureEngine({})
    assert eng._ote_state(df, atr=1.0) == "bullish_ote"


def test_ote_state_bearish_retracement():
    """Swing high at bar 10 -> down leg to swing low at bar 40 -> monotonic
    retracement UP into the 62-79% zone at the last bar -> bearish_ote (mirror)."""
    n = 80
    closes = [0.0] * n; highs = [0.0] * n; lows = [0.0] * n
    for i in range(n):
        if i < 10:
            c = 100.0
        elif i == 10:
            c = 104.0  # spike high
        elif i <= 39:
            c = 104.0 - (i - 10) * 1.1  # monotonic down to ~72.1
        elif i == 40:
            c = 71.0  # trough
        else:
            c = 71.0 + (i - 40) * 0.63  # monotonic rally to ~95
        closes[i] = c; highs[i] = c + 0.5; lows[i] = c - 0.5
    highs[10] = 105.0; lows[10] = 103.5  # clear swing high
    # leg = 105 -> 70.5 (low at bar 40 = 70.5); 62% up = ~92.2, 79% = ~98.4
    df = _df(closes, opens=closes[:], highs=highs, lows=lows, vols=[100.0] * n)
    eng = FeatureEngine({})
    assert eng._ote_state(df, atr=1.0) == "bearish_ote"


def test_ote_state_needs_history():
    eng = FeatureEngine({})
    assert eng._ote_state(_df([100.0] * 10), atr=1.0) is None


def test_ote_state_no_leg():
    """Flat series -> no pivots -> no leg -> None."""
    eng = FeatureEngine({})
    assert eng._ote_state(_df([100.0] * 60), atr=1.0) is None


# --- ICT Breaker Block (iter-53) + RSI divergence (iter-54) ----------------


def test_ict_breaker_block_detect_sides():
    """ict_breaker_block: detect callable maps breaker_block state -> side."""
    out = spec_mod._ict_breaker_block(_feat({"breaker_block": "bullish_breaker"}), {}, _ev(), _cfg())
    assert out and out["side"] == "BUY"
    out = spec_mod._ict_breaker_block(_feat({"breaker_block": "bearish_breaker"}), {}, _ev(), _cfg())
    assert out and out["side"] == "SELL"
    assert spec_mod._ict_breaker_block(_feat(), {}, _ev(), _cfg()) is None


def test_rsi_divergence_detect_sides():
    """rsi_divergence: detect callable maps rsi_divergence state -> side."""
    out = spec_mod._rsi_divergence(_feat({"rsi_divergence": "bearish_divergence"}), {}, _ev(), _cfg())
    assert out and out["side"] == "SELL"
    out = spec_mod._rsi_divergence(_feat({"rsi_divergence": "bullish_divergence"}), {}, _ev(), _cfg())
    assert out and out["side"] == "BUY"
    assert spec_mod._rsi_divergence(_feat(), {}, _ev(), _cfg()) is None


def test_breaker_block_state_bullish():
    """Pivot high H at bar 12, broken above at bar 21, retraced to H zone at the
    last bar with a bullish rejection -> bullish_breaker. A newer higher pivot
    high (~115) is NOT retested (price declined from it) so the search falls
    through to the broken H."""
    from core.specialized_setups import _ict_breaker_block  # noqa: F401  (registry import side-effect)
    n = 50
    closes = [100.0] * n; highs = [100.5] * n; lows = [99.5] * n; opens = [100.0] * n
    # pivot high at bar 12 (H=105); surrounding bars < 105
    for i in range(7, 12):
        highs[i] = 101.0
    for i in range(13, 18):
        highs[i] = 101.0
    highs[12] = 105.0; closes[12] = 104.0; lows[12] = 99.5; opens[12] = 100.0
    # decline bars 13-20
    for i in range(13, 21):
        closes[i] = 98.0; highs[i] = 99.0; lows[i] = 97.5; opens[i] = 100.0
    # break above H at bar 21 (close 106 > 105), strictly before k=49
    closes[21] = 106.0; highs[21] = 106.5; lows[21] = 99.0; opens[21] = 99.0
    # rise to a newer higher pivot high near bar 35 (115), then decline back
    for i in range(22, 36):
        c = 106.0 + (115.0 - 106.0) * (i - 22) / 13
        closes[i] = c; highs[i] = c + 0.5; lows[i] = c - 0.5; opens[i] = c - 0.5
    highs[35] = 115.0; closes[35] = 114.5  # pivot high at 35
    for i in range(36, 42):
        highs[i] = 113.0  # ensure bar 35 is a pivot (right side lower)
    # decline back to the H zone; retest bar k=49
    for i in range(36, 49):
        c = 114.0 - (114.0 - 105.0) * (i - 35) / 14
        closes[i] = c; highs[i] = c + 0.4; lows[i] = c - 0.4; opens[i] = c + 0.2
    # bar 49: retest H=105 (low 105.1 within [104.65,105.35]) + bullish rejection
    opens[49] = 105.0; closes[49] = 105.8; lows[49] = 105.1; highs[49] = 106.0
    df = _df(closes, opens=opens, highs=highs, lows=lows, vols=[100.0] * n)
    eng = FeatureEngine({})
    assert eng._breaker_block_state(df, atr=1.0) == "bullish_breaker"


def test_breaker_block_state_bearish():
    """Pivot low L at bar 12, broken below at bar 21, retraced to L zone at the
    last bar with a bearish rejection -> bearish_breaker (mirror of bullish)."""
    n = 50
    closes = [100.0] * n; highs = [100.5] * n; lows = [99.5] * n; opens = [100.0] * n
    # pivot low at bar 12 (L=95); surrounding bars lows > 95
    for i in range(7, 12):
        lows[i] = 99.0
    for i in range(13, 18):
        lows[i] = 99.0
    lows[12] = 95.0; closes[12] = 96.0; highs[12] = 100.5; opens[12] = 100.0
    # rise bars 13-20
    for i in range(13, 21):
        closes[i] = 102.0; highs[i] = 103.0; lows[i] = 101.5; opens[i] = 100.0
    # break below L at bar 21 (close 94 < 95)
    closes[21] = 94.0; highs[21] = 101.0; lows[21] = 93.5; opens[21] = 101.0
    # decline to a newer lower pivot low near bar 35 (85), then rise back
    for i in range(22, 36):
        c = 94.0 - (94.0 - 85.0) * (i - 22) / 13
        closes[i] = c; highs[i] = c + 0.5; lows[i] = c - 0.5; opens[i] = c + 0.5
    lows[35] = 85.0; closes[35] = 85.5  # pivot low at 35
    for i in range(36, 42):
        lows[i] = 87.0  # ensure bar 35 is a pivot (right side higher)
    # rise back to the L zone; retest bar k=49
    for i in range(36, 49):
        c = 86.0 + (95.0 - 86.0) * (i - 35) / 14
        closes[i] = c; highs[i] = c + 0.4; lows[i] = c - 0.4; opens[i] = c - 0.2
    # bar 49: retest L=95 (high 94.9 within [94.65,95.35]) + bearish rejection
    opens[49] = 95.0; closes[49] = 94.2; lows[49] = 94.0; highs[49] = 94.9
    df = _df(closes, opens=opens, highs=highs, lows=lows, vols=[100.0] * n)
    eng = FeatureEngine({})
    assert eng._breaker_block_state(df, atr=1.0) == "bearish_breaker"


def test_breaker_block_state_needs_history():
    eng = FeatureEngine({})
    assert eng._breaker_block_state(_df([100.0] * 10), atr=1.0) is None


def test_breaker_block_state_flat_is_none():
    """Flat series -> no pivots -> None."""
    eng = FeatureEngine({})
    assert eng._breaker_block_state(_df([100.0] * 60), atr=1.0) is None


def test_rsi_divergence_state_needs_history():
    eng = FeatureEngine({})
    assert eng._rsi_divergence(_df([100.0] * 10)) is None


def test_rsi_divergence_state_flat_is_none():
    """Flat series -> no pivot divergence -> None."""
    eng = FeatureEngine({})
    assert eng._rsi_divergence(_df([100.0] * 80)) is None


def _norm(x):
    if x is None:
        return None
    if isinstance(x, float) and np.isnan(x):
        return None
    return x


def test_rsi_divergence_parity_smoke():
    """FeatureEngine._rsi_divergence vs _vectorized_rsi_divergence at every bar
    on a damped-sine+drift series (creates divergent pivots) -> 0 mismatches."""
    from core.specialized_replay_labeler import _vectorized_rsi_divergence
    n = 220
    ts = pd.date_range("2026-08-03 00:00:00", periods=n, freq="5min", tz="UTC")
    i = np.arange(n)
    amp = 12.0 * (1.0 - 0.6 * i / n)            # damped amplitude
    closes = 100.0 + 0.25 * i + amp * np.sin(i * 0.35)
    opens = closes - 0.5; highs = np.maximum(opens, closes) + 0.5; lows = np.minimum(opens, closes) - 0.5
    df = pd.DataFrame({"time": ts, "open": opens, "high": highs, "low": lows,
                       "close": closes, "volume": [100.0] * n})
    eng = FeatureEngine({})
    vec = _vectorized_rsi_divergence(df["high"].to_numpy(), df["low"].to_numpy(),
                                     df["close"].to_numpy(), _wilder_rsi(df["close"]))
    mism = 0; fired = 0
    for k in range(20, n):
        live = _norm(eng._rsi_divergence(df.iloc[:k + 1]))
        v = _norm(vec[k])
        if v is not None:
            fired += 1
        if live != v:
            mism += 1
    assert fired > 0, "smoke produced no divergence — parity untested"
    assert mism == 0, f"RSI divergence parity: {mism} mismatches across {n} bars ({fired} fired)"


def test_breaker_block_parity_smoke():
    """FeatureEngine._breaker_block_state vs _vectorized_breaker_block_state at
    every bar on a damped-sine+drift series -> 0 mismatches."""
    from core.specialized_replay_labeler import _vectorized_breaker_block_state
    n = 220
    ts = pd.date_range("2026-08-03 00:00:00", periods=n, freq="5min", tz="UTC")
    i = np.arange(n)
    amp = 12.0 * (1.0 - 0.6 * i / n)
    closes = 100.0 + 0.25 * i + amp * np.sin(i * 0.35)
    opens = closes - 0.5; highs = np.maximum(opens, closes) + 0.5; lows = np.minimum(opens, closes) - 0.5
    df = pd.DataFrame({"time": ts, "open": opens, "high": highs, "low": lows,
                       "close": closes, "volume": [100.0] * n})
    eng = FeatureEngine({})
    # ATR series (SMA-14 of TR proxy ~ simple); pass a constant 1.0 ATR to both
    # paths so the zone test matches exactly (live helper takes a scalar atr).
    atr_arr = np.full(n, 1.0)
    vec = _vectorized_breaker_block_state(df["high"].to_numpy(), df["low"].to_numpy(),
                                          df["close"].to_numpy(), df["open"].to_numpy(), atr_arr)
    mism = 0; fired = 0
    for k in range(30, n):
        live = _norm(eng._breaker_block_state(df.iloc[:k + 1], atr=1.0))
        v = _norm(vec[k])
        if v is not None:
            fired += 1
        if live != v:
            mism += 1
    assert fired > 0, "smoke produced no breaker — parity untested"
    assert mism == 0, f"breaker parity: {mism} mismatches across {n} bars ({fired} fired)"


def _wilder_rsi(close: pd.Series, period: int = 14) -> np.ndarray:
    delta = close.diff()
    gain = delta.clip(lower=0.0); loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).to_numpy(dtype=float)


def _df(closes, opens=None, highs=None, lows=None, vols=None, start_ts="2026-08-03 00:00:00"):
    n = len(closes)
    opens = opens or closes
    highs = highs or [max(o, c) for o, c in zip(opens, closes)]
    lows = lows or [min(o, c) for o, c in zip(opens, closes)]
    vols = vols or [100.0] * n
    times = pd.date_range(start=start_ts, periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {"time": times, "open": opens, "high": highs, "low": lows, "close": closes, "volume": vols}
    )


def test_cvd_bearish_divergence_fires():
    """Price higher high + CVD lower high at a confirmed pivot -> SELL state.

    Deterministic construction (verified empirically): flat at 100 through bar
    24, pivot high #1 at bar 25 (105), down-drift 26-54 (negative deltas drag
    the cumulative CVD down), pivot high #2 at bar 55 (107, higher price),
    confirmed at bar 60 = current bar. CVD at 55 < CVD at 25 -> bearish.
    """
    n = 61
    closes = [100.0] * n
    opens = [100.0] * n
    highs = [100.0] * n
    lows = [100.0] * n
    vols = [10.0] * n
    # Pivot high #1
    closes[25], opens[25], highs[25], lows[25] = 105.0, 100.0, 105.5, 99.5
    # Down-drift: close 99 every bar -> negative delta drags CVD down
    for i in range(26, 55):
        closes[i], opens[i], highs[i], lows[i] = 99.0, 100.0, 100.0, 98.5
    # Pivot high #2 (higher price), confirmed at bar 60 = current bar
    closes[55], opens[55], highs[55], lows[55] = 107.0, 100.0, 107.5, 101.0
    df = _df(closes, opens=opens, highs=highs, lows=lows, vols=vols)
    eng = FeatureEngine({})
    assert eng._cvd_divergence(df) == "bearish_divergence"


def test_cvd_needs_history():
    eng = FeatureEngine({})
    assert eng._cvd_divergence(_df([100.0] * 10)) is None


def test_vp_va_breakout_state():
    """Close above VAH with volume_ratio >= 1.2 -> bullish va_breakout.

    Deterministic (verified empirically): 110 bars drifting 100 -> ~101, last
    close 106 (well above the value area) with 300 volume (3x the 100 avg).
    """
    n = 110
    closes = [100.0 + 0.01 * i for i in range(n)]
    closes[-1] = 106.0  # breakout close
    opens = closes[:]
    highs = [c + 0.1 for c in closes]
    lows = [c - 0.1 for c in closes]
    vols = [100.0] * n
    vols[-1] = 300.0  # 3x average -> >= 1.2 threshold
    df = _df(closes, opens=opens, highs=highs, lows=lows, vols=vols)
    eng = FeatureEngine({})
    state = eng._session_volume_profile_state(df, atr=0.5)
    assert state["va_breakout"] == "bullish"


def test_vp_state_needs_atr():
    eng = FeatureEngine({})
    state = eng._session_volume_profile_state(_df([100.0] * 120), atr=0.0)
    assert state == {"poc_rejection": None, "va_breakout": None}


def test_intermarket_zero_cross_detects_cross():
    """Spread zA-zB crossing zero at the last bar -> bullish_cross.

    Deterministic (verified empirically): a 12-pi sine over 400 bars has an
    upward spread zero-crossing at index 139; truncating to the first 140 bars
    puts that crossing exactly on the last bar, so the vectorized z-score
    (rolling 50-bar mean/std, EMA-20) fires bullish_cross. Anchor is flat at
    100 (z ~ 0), so the crossing is driven by the target's z alone.
    """
    n = 400
    ts = pd.date_range("2026-08-03 00:00:00", periods=n, freq="5min", tz="UTC")
    phase = np.linspace(0, 12 * np.pi, n)
    t_closes = 100.0 + 3.0 * np.sin(phase)
    t_bars = [{"time": t, "close": float(c), "open": float(c), "high": float(c),
               "low": float(c), "volume": 100} for t, c in zip(ts, t_closes)]
    a_bars = [{"time": t, "close": 100.0, "open": 100.0, "high": 100.0,
               "low": 100.0, "volume": 100} for t in ts]
    assert FeatureEngine._intermarket_zero_cross(t_bars[:140], a_bars[:140]) == "bullish_cross"


def test_intermarket_short_series_none():
    t_bars = [{"time": pd.Timestamp("2026-08-03", tz="UTC"), "close": 100.0}]
    a_bars = [{"time": pd.Timestamp("2026-08-03", tz="UTC"), "close": 120.0}]
    assert FeatureEngine._intermarket_zero_cross(t_bars, a_bars) is None


def test_intermarket_stale_anchor_none():
    """If the anchor's latest close is older than the target's current bar,
    the crossing would describe a stale bar — must refuse (None)."""
    n = 400
    ts = pd.date_range("2026-08-03 00:00:00", periods=n, freq="5min", tz="UTC")
    phase = np.linspace(0, 12 * np.pi, n)
    t_closes = 100.0 + 3.0 * np.sin(phase)
    t_bars = [{"time": t, "close": float(c), "open": float(c), "high": float(c),
               "low": float(c), "volume": 100} for t, c in zip(ts, t_closes)]
    a_bars = [{"time": t, "close": 100.0, "open": 100.0, "high": 100.0,
               "low": 100.0, "volume": 100} for t in ts]
    # Same firing construction as the crossing test, but anchor lags by one bar.
    assert FeatureEngine._intermarket_zero_cross(t_bars[:140], a_bars[:139]) is None


def test_compute_all_injects_intermarket_and_vp():
    """compute_all produces the new state fields on every symbol + the
    cross-symbol intermarket stamp when the spread crosses zero."""
    n = 150
    ts = pd.date_range("2026-08-03 00:00:00", periods=n, freq="5min", tz="UTC")

    def mk_anchor():
        bars = []
        for i, t in enumerate(ts):
            c = 100.0
            if i < 80:
                c = 100.0 + 100.0 * i / 80
            if i >= 120:
                c = 200.0 - 100.0 * (i - 119) / 30
            bars.append({"time": t, "open": c, "high": c + 0.1,
                         "low": c - 0.1, "close": c, "volume": 100.0})
        return bars

    def mk_flat():
        return [{"time": t, "open": 100.0, "high": 100.1,
                 "low": 99.9, "close": 100.0, "volume": 100.0} for t in ts]

    candles = {"symbols": {"US30m": {"M5": mk_flat(), "M15": mk_flat()[::3]},
                           "NAS100m": {"M5": mk_anchor(), "M15": mk_anchor()[::3]}}}
    eng = FeatureEngine({})
    feats = eng.compute_all(candles)
    for s, f in feats["symbols"].items():
        assert "vp_poc_rejection" in f
        assert "vp_va_breakout" in f
        assert "cvd_divergence" in f
    assert set(feats["symbols"].keys()) == {"US30m", "NAS100m"}
    # US30m is the target in (US30m, NAS100m); when the cross fired the stamp
    # is present with the anchor recorded.
    if "intermarket_zero_cross" in feats["symbols"]["US30m"]:
        assert feats["symbols"]["US30m"]["intermarket_anchor"] == "NAS100m"
