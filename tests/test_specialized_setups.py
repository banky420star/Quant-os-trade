"""Tests for the specialized ICT/SMC killzone setups (2026-07-31).

Covers: opt-in gating, UTC killzone windows, per-symbol + per-setup allowlists,
regime compatibility via the classifier, and that they never fire when the
feature window is outside the killzone. Honest framing: these are granularity
for per-symbol evidence, not edge (see VERDICT.md).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.specialized_setups as spec_mod
from core.specialized_setups import detect_specialized, specialized_config
from core.setup_classifier import SetupClassifier
from core.setup_library import SETUP_LIBRARY


def _feat(rejection="bullish_rejection", symbol="XAUUSDm"):
    return {
        "symbol": symbol,
        "rejection": rejection,
        "m5_trend": "bullish" if rejection == "bullish_rejection" else "bearish",
        # mid-band so BB-based specialized setups (iter7) don't fire here —
        # keeps the rejection-based tests isolated on their target setup.
        "bb_position": 0.5,
        "price": 2000.0,
        "support": 1990.0,
        "resistance": 2010.0,
    }


def _ctx(session="overlap_london_ny"):
    return {
        "regime": "weak_trend",
        "phase": "transitional",
        "move_type": "pullback",
        "session": session,
        "market_regime": {"primary": "weak_trend", "bias": "bull"},
    }


def _ev(liquidity=0.8):
    return {
        "trend": 0.7, "momentum": 0.6, "structure": 0.65,
        "volume": 0.5, "liquidity": liquidity, "volatility": 0.4,
    }


def _cfg(enabled=True, symbols=None, setups=None, per_symbol=None, shadow=False):
    return {
        "signals": {
            "specialized_setups": {
                "enabled": enabled,
                "symbols": symbols or [],
                "setups": setups or [],
                "per_symbol": per_symbol or {},
                "shadow": shadow,
            }
        }
    }


# --- opt-in gating ---

def test_disabled_returns_empty(monkeypatch):
    """Default-off: no candidates unless signals.specialized_setups.enabled."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    out = detect_specialized(_feat(), _ctx(), _ev(), _cfg(enabled=False))
    assert out == []


def test_enabled_default_all_setups(monkeypatch):
    """Enabled with no allowlists -> both setups eligible (subject to window)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    out = detect_specialized(_feat(), _ctx(), _ev(), _cfg(enabled=True))
    # 14:00 UTC is in silver_bullet window only (london_judas is 07-10).
    assert len(out) == 1
    assert out[0]["setup_type"] == "silver_bullet"
    assert out[0]["side"] == "BUY"


# --- Silver Bullet window (14:00-15:00 UTC) ---

def test_silver_bullet_fires_in_window_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    out = detect_specialized(_feat(rejection="bullish_rejection"), _ctx(), _ev(0.8), _cfg(True))
    assert out and out[0]["setup_type"] == "silver_bullet"
    assert out[0]["side"] == "BUY"
    assert "14:00-15:00" in out[0]["reason"]


def test_silver_bullet_fires_in_window_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    out = detect_specialized(_feat(rejection="bearish_rejection"), _ctx(), _ev(0.8), _cfg(True))
    assert out and out[0]["setup_type"] == "silver_bullet"
    assert out[0]["side"] == "SELL"


def test_silver_bullet_skipped_outside_window(monkeypatch):
    """03:00 UTC is in neither silver_bullet (14-15) nor london_judas (07-10)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 3)
    out = detect_specialized(_feat(), _ctx(), _ev(0.8), _cfg(True))
    assert out == []


def test_silver_bullet_skipped_low_liquidity(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    out = detect_specialized(_feat(), _ctx(), _ev(0.4), _cfg(True))
    assert out == []  # liquidity 0.4 < min 0.60


def test_silver_bullet_skipped_no_rejection(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    out = detect_specialized(_feat(rejection=None), _ctx(), _ev(0.8), _cfg(True))
    assert out == []


# --- London Judas window (07:00-10:00 UTC) ---

def test_london_judas_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat(rejection="bullish_rejection"), _ctx("london_open"), _ev(0.8), _cfg(True))
    assert out and out[0]["setup_type"] == "london_judas"
    assert out[0]["side"] == "BUY"


def test_london_judas_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat(), _ctx(), _ev(0.8), _cfg(True))
    assert out == []


# --- iteration 2: oil ORB (14:00-19:00 UTC) ---

def _feat_bo(bo="breakout", symbol="USOILm"):
    return {
        "symbol": symbol,
        "breakout": bo,
        "m5_trend": "bullish" if bo == "breakout" else "bearish",
        "bb_position": 0.5,
        "price": 78.0, "support": 77.0, "resistance": 79.0,
    }


def _feat_bb(bb=0.9, symbol="EURUSDm"):
    """Feature dict for BB-based setups: bb_position at an extreme, no
    rejection/breakout state so only BB-reversion detectors can fire."""
    return {
        "symbol": symbol,
        "bb_position": bb,
        "m5_trend": "neutral",
        "rejection": None,
        "breakout": None,
        "price": 1.10, "support": 1.09, "resistance": 1.11,
    }


def test_oil_orb_fires_in_window_breakout(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_bo("breakout"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "oil_orb" in types
    oil = next(c for c in out if c["setup_type"] == "oil_orb")
    assert oil["side"] == "BUY"
    assert "14:00-19:00 UTC" in oil["reason"]


def test_oil_orb_fires_breakdown_sell(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_bo("breakdown"), _ctx(), _ev(0.8), _cfg(True))
    oil = next(c for c in out if c["setup_type"] == "oil_orb")
    assert oil["side"] == "SELL"


def test_oil_orb_skipped_outside_window(monkeypatch):
    """06:00 UTC is before the 14:00-19:00 oil execution window."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 6)
    out = detect_specialized(_feat_bo("breakout"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "oil_orb" not in types


def test_oil_orb_skipped_no_breakout_state(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_bo(None), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "oil_orb" not in types


# --- iteration 2: Asian range breakout (07:00-10:00 UTC) ---

def test_asia_range_breakout_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_bo("breakout", symbol="EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "asia_range_breakout" in types
    arb = next(c for c in out if c["setup_type"] == "asia_range_breakout")
    assert arb["side"] == "BUY"


def test_asia_range_breakout_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_bo("breakout", symbol="EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "asia_range_breakout" not in types


# --- iteration 3: US index RTH open ORB (14:00-16:00 UTC) ---

def test_us_open_orb_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_bo("breakout", symbol="NAS100m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "us_open_orb" in types
    uo = next(c for c in out if c["setup_type"] == "us_open_orb")
    assert uo["side"] == "BUY"
    assert "9:30 AM ET" in uo["reason"]


def test_us_open_orb_skipped_outside_window(monkeypatch):
    """17:00 UTC is past the 14:00-16:00 US open ORB window."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 17)
    out = detect_specialized(_feat_bo("breakout", symbol="NAS100m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "us_open_orb" not in types


# --- iteration 3: prev-day H/L breakout (14:00-21:00 UTC US RTH) ---

def test_prev_day_breakout_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_bo("breakdown", symbol="US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "prev_day_breakout" in types
    pd = next(c for c in out if c["setup_type"] == "prev_day_breakout")
    assert pd["side"] == "SELL"
    assert "Prev day" in pd["reason"]


def test_prev_day_breakout_skipped_outside_rth(monkeypatch):
    """10:00 UTC is before US RTH (14:00-21:00 UTC)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 10)
    out = detect_specialized(_feat_bo("breakout", symbol="US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "prev_day_breakout" not in types


def test_us_open_orb_and_prev_day_cofire_in_overlap(monkeypatch):
    """At 15:00 UTC both index setups are in-window — arena competition lets
    both fire as distinct culturing cells (setup_type differentiates them)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_bo("breakout", symbol="US500m"), _ctx(), _ev(0.8), _cfg(True))
    types = {c["setup_type"] for c in out}
    assert "us_open_orb" in types
    assert "prev_day_breakout" in types


# --- iteration 4: EU open ORB (08:00-10:00 UTC) ---

def test_eu_open_orb_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_bo("breakout", symbol="UK100m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "eu_open_orb" in types
    eu = next(c for c in out if c["setup_type"] == "eu_open_orb")
    assert eu["side"] == "BUY"
    assert "London-Frankfurt" in eu["reason"]


def test_eu_open_orb_skipped_outside_window(monkeypatch):
    """11:00 UTC is past the 08:00-10:00 EU open window."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_bo("breakout", symbol="FR40m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "eu_open_orb" not in types


# --- iteration 4: Tokyo open ORB (00:00-02:00 UTC) ---

def test_tokyo_open_orb_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 1)
    out = detect_specialized(_feat_bo("breakdown", symbol="USDJPYm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "tokyo_open_orb" in types
    tk = next(c for c in out if c["setup_type"] == "tokyo_open_orb")
    assert tk["side"] == "SELL"
    assert "Tokyo" in tk["reason"]


def test_tokyo_open_orb_skipped_outside_window(monkeypatch):
    """03:00 UTC is past the 00:00-02:00 Tokyo first-hour window."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 3)
    out = detect_specialized(_feat_bo("breakout", symbol="JP225m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "tokyo_open_orb" not in types


# --- iteration 5: London Close reversal (15:00-17:00 UTC, rejection-based) ---

def test_london_close_reversal_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 16)
    out = detect_specialized(_feat(rejection="bearish_rejection", symbol="USDCHFm"),
                             _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_close_reversal" in types
    lc = next(c for c in out if c["setup_type"] == "london_close_reversal")
    assert lc["side"] == "SELL"
    assert "15:00-17:00" in lc["reason"]


def test_london_close_reversal_skipped_outside_window(monkeypatch):
    """18:00 UTC is past the 15:00-17:00 London close killzone."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat(rejection="bullish_rejection", symbol="USDCHFm"),
                             _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_close_reversal" not in types


def test_london_close_reversal_skipped_no_rejection(monkeypatch):
    """Rejection-based setup needs a rejection candle."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 16)
    out = detect_specialized(_feat(rejection=None, symbol="USDCHFm"),
                             _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_close_reversal" not in types


# --- iteration 5: Sydney open ORB (21:00-23:00 UTC, breakout-based) ---

def test_sydney_open_orb_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 22)
    out = detect_specialized(_feat_bo("breakout", symbol="AUDUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "sydney_open_orb" in types
    sy = next(c for c in out if c["setup_type"] == "sydney_open_orb")
    assert sy["side"] == "BUY"
    assert "21:00-23:00" in sy["reason"]


def test_sydney_open_orb_skipped_outside_window(monkeypatch):
    """20:00 UTC is before the 21:00-23:00 Sydney open window."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 20)
    out = detect_specialized(_feat_bo("breakout", symbol="AUDUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "sydney_open_orb" not in types


def test_sydney_open_orb_fires_breakdown_sell(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 21)
    out = detect_specialized(_feat_bo("breakdown", symbol="AUDUSDm"), _ctx(), _ev(0.8), _cfg(True))
    sy = next(c for c in out if c["setup_type"] == "sydney_open_orb")
    assert sy["side"] == "SELL"


# --- iteration 6: London morning breakout (10:00-14:00 UTC, breakout-based) ---

def test_london_morning_breakout_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 12)
    out = detect_specialized(_feat_bo("breakout", symbol="EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_morning_breakout" in types
    lm = next(c for c in out if c["setup_type"] == "london_morning_breakout")
    assert lm["side"] == "BUY"
    assert "10:00-14:00" in lm["reason"]


def test_london_morning_breakout_skipped_outside_window(monkeypatch):
    """09:00 UTC is in the London open noise window (07-10), before 10:00-14:00."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_bo("breakout", symbol="EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_morning_breakout" not in types


# --- iteration 6: NY lunch reversal (17:00-19:00 UTC, rejection-based) ---

def test_ny_lunch_reversal_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat(rejection="bullish_rejection", symbol="US30m"),
                             _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_lunch_reversal" in types
    ny = next(c for c in out if c["setup_type"] == "ny_lunch_reversal")
    assert ny["side"] == "BUY"
    assert "17:00-19:00" in ny["reason"]


def test_ny_lunch_reversal_skipped_outside_window(monkeypatch):
    """20:00 UTC is past the 17:00-19:00 NY lunch window."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 20)
    out = detect_specialized(_feat(rejection="bullish_rejection", symbol="US30m"),
                             _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_lunch_reversal" not in types


# --- iteration 7: London BB reversion (07:00-10:00 UTC, bb_position-based) ---

def test_london_bb_reversion_fires_upper_band_sell(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_bb(bb=0.9, symbol="EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_bb_reversion" in types
    lb = next(c for c in out if c["setup_type"] == "london_bb_reversion")
    assert lb["side"] == "SELL"  # bb at upper band -> fade down
    assert "07:00-10:00" in lb["reason"]


def test_london_bb_reversion_fires_lower_band_buy(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_bb(bb=0.05, symbol="EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    lb = next(c for c in out if c["setup_type"] == "london_bb_reversion")
    assert lb["side"] == "BUY"  # bb at lower band -> fade up


def test_london_bb_reversion_skipped_outside_window(monkeypatch):
    """11:00 UTC is past the 07:00-10:00 London BB window."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_bb(bb=0.9, symbol="EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_bb_reversion" not in types


def test_london_bb_reversion_skipped_mid_band(monkeypatch):
    """bb_position 0.5 is mid-band -> no reversion signal."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_bb(bb=0.5, symbol="EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_bb_reversion" not in types


# --- iteration 7: NY BB reversion (14:00-17:00 UTC, bb_position-based) ---

def test_ny_bb_reversion_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_bb(bb=0.9, symbol="US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_bb_reversion" in types
    ny = next(c for c in out if c["setup_type"] == "ny_bb_reversion")
    assert ny["side"] == "SELL"
    assert "14:00-17:00" in ny["reason"]


def test_ny_bb_reversion_skipped_outside_window(monkeypatch):
    """18:00 UTC is past the 14:00-17:00 NY BB window."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_bb(bb=0.05, symbol="US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_bb_reversion" not in types


# --- iteration 6: per-symbol granular allowlist ---

def test_per_symbol_restricts_to_listed_setup(monkeypatch):
    """AUDUSDm keyed in per_symbol to [sydney_open_orb] only — at 22 UTC the
    sydney_open_orb fires, but at 14 UTC a silver_bullet-style rejection on
    AUDUSDm is suppressed because silver_bullet is not in AUDUSDm's per_symbol
    list (even though globally enabled)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    out = detect_specialized(_feat(rejection="bullish_rejection", symbol="AUDUSDm"),
                             _ctx(), _ev(0.8),
                             _cfg(True, per_symbol={"AUDUSDm": ["sydney_open_orb"]}))
    types = [c["setup_type"] for c in out]
    # silver_bullet would normally fire at 14 UTC, but per_symbol blocks it for AUDUSDm
    assert "silver_bullet" not in types
    assert "sydney_open_orb" not in types  # not in window at 14 UTC anyway


def test_per_symbol_allows_listed_setup_when_in_window(monkeypatch):
    """AUDUSDm keyed to [sydney_open_orb] — at 22 UTC sydney_open_orb fires."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 22)
    out = detect_specialized(_feat_bo("breakout", symbol="AUDUSDm"), _ctx(), _ev(0.8),
                             _cfg(True, per_symbol={"AUDUSDm": ["sydney_open_orb"]}))
    types = [c["setup_type"] for c in out]
    assert "sydney_open_orb" in types


def test_per_symbol_does_not_affect_other_symbols(monkeypatch):
    """AUDUSDm restricted to sydney_open_orb, but XAUUSDm is unrestricted ->
    silver_bullet still fires on XAUUSDm at 14 UTC."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    out = detect_specialized(_feat(rejection="bullish_rejection", symbol="XAUUSDm"),
                             _ctx(), _ev(0.8),
                             _cfg(True, per_symbol={"AUDUSDm": ["sydney_open_orb"]}))
    types = [c["setup_type"] for c in out]
    assert "silver_bullet" in types


def test_per_symbol_empty_does_nothing(monkeypatch):
    """Empty per_symbol map = no extra restriction (default behavior preserved)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    out = detect_specialized(_feat(), _ctx(), _ev(0.8), _cfg(True, per_symbol={}))
    assert out and out[0]["setup_type"] == "silver_bullet"


# --- iteration 8: shadow fire-ledger (logs fires without placing orders) ---

def test_shadow_logs_when_enabled_off(monkeypatch):
    """shadow=true, enabled=false -> fires are logged but NOT emitted (no orders)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    logged = []
    monkeypatch.setattr(spec_mod, "append_archive_record",
                        lambda name, rec: logged.append((name, rec)))
    monkeypatch.setattr(spec_mod, "utc_now_iso", lambda: "2026-08-01T14:00:00Z")
    out = detect_specialized(_feat(), _ctx(), _ev(0.8), _cfg(enabled=False, shadow=True))
    assert out == []  # not emitted for trading
    assert len(logged) == 1
    name, rec = logged[0]
    assert name == spec_mod.SHADOW_LEDGER_NAME
    assert rec["setup_type"] == "silver_bullet"
    assert rec["symbol"] == "XAUUSDm"
    assert rec["side"] == "BUY"
    assert rec["fired_at"] == "2026-08-01T14:00:00Z"
    assert rec["feat"]["m5_trend"] == "bullish"


def test_shadow_logs_and_emits_when_enabled_on(monkeypatch):
    """shadow=true, enabled=true -> fires are logged AND emitted for trading."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    logged = []
    monkeypatch.setattr(spec_mod, "append_archive_record",
                        lambda name, rec: logged.append(rec))
    out = detect_specialized(_feat(), _ctx(), _ev(0.8), _cfg(enabled=True, shadow=True))
    assert out and out[0]["setup_type"] == "silver_bullet"
    assert len(logged) == 1
    assert logged[0]["setup_type"] == "silver_bullet"


def test_no_shadow_no_log_when_disabled(monkeypatch):
    """shadow=false, enabled=false -> nothing emitted, nothing logged."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    logged = []
    monkeypatch.setattr(spec_mod, "append_archive_record",
                        lambda name, rec: logged.append(rec))
    out = detect_specialized(_feat(), _ctx(), _ev(0.8), _cfg(enabled=False, shadow=False))
    assert out == []
    assert logged == []


def test_shadow_respects_per_symbol(monkeypatch):
    """shadow logs only setups allowed by per_symbol — silver_bullet blocked on
    XAUUSDm whose per_symbol list is [london_judas], so nothing fires/logs."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    logged = []
    monkeypatch.setattr(spec_mod, "append_archive_record",
                        lambda name, rec: logged.append(rec))
    detect_specialized(_feat(), _ctx(), _ev(0.8),
                       _cfg(enabled=False, shadow=True,
                            per_symbol={"XAUUSDm": ["london_judas"]}))
    assert logged == []  # silver_bullet blocked by per_symbol at 14 UTC


def test_shadow_logs_each_fire_separately(monkeypatch):
    """At 15 UTC a US500m breakout fires THREE breakout-in-window setups
    (us_open_orb 14-16, prev_day_breakout 14-21, oil_orb 14-19 — all key off
    feat.breakout with no symbol restriction when the symbols allowlist is empty)
    — shadow logs one record per fire (3 records)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    logged = []
    monkeypatch.setattr(spec_mod, "append_archive_record",
                        lambda name, rec: logged.append(rec))
    out = detect_specialized(_feat_bo("breakout", symbol="US500m"), _ctx(), _ev(0.8),
                             _cfg(enabled=False, shadow=True))
    assert out == []  # shadow-only: not emitted
    types = [r["setup_type"] for r in logged]
    assert "us_open_orb" in types
    assert "prev_day_breakout" in types
    assert "oil_orb" in types
    assert len(logged) == 3
    # every record carries the symbol + a feat slice
    assert all(r["symbol"] == "US500m" for r in logged)
    assert all("feat" in r for r in logged)


# --- allowlists ---

def test_symbol_allowlist_filters(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    out = detect_specialized(_feat(symbol="USOILm"), _ctx(), _ev(0.8),
                             _cfg(True, symbols=["XAUUSDm"]))
    assert out == []  # USOILm not in allowlist


def test_setup_allowlist_filters(monkeypatch):
    """Only london_judas allowed -> silver_bullet suppressed even in its window."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    out = detect_specialized(_feat(), _ctx(), _ev(0.8), _cfg(True, setups=["london_judas"]))
    assert out == []


# --- first-class registration ---

def test_new_setups_in_known_setups():
    """silver_bullet + london_judas are in SETUP_LIBRARY (so culturing cells,
    setup-aggregate veto, and normalize_setup_type all recognize them)."""
    assert "silver_bullet" in SETUP_LIBRARY
    assert "london_judas" in SETUP_LIBRARY


# --- classifier integration ---

def test_classifier_emits_specialized_when_enabled(monkeypatch):
    """SetupClassifier picks up specialized candidates when enabled + in-window."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    clf = SetupClassifier(_cfg(True))
    # classify_all returns every setup that fires; silver_bullet should appear.
    cands = clf.classify_all(_feat(), _ctx(), _ev(0.8))
    types = [c["setup_type"] for c in cands]
    assert "silver_bullet" in types


def test_classifier_silent_when_disabled(monkeypatch):
    """When disabled, the classifier emits none of the specialized setups."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    clf = SetupClassifier(_cfg(False))
    cands = clf.classify_all(_feat(), _ctx(), _ev(0.8))
    types = [c["setup_type"] for c in cands]
    assert "silver_bullet" not in types
    assert "london_judas" not in types


def test_classifier_regime_filter_blocks_incompatible(monkeypatch):
    """silver_bullet is blocked in volatility_spike regime — the classifier's
    regime filter must drop it even when in-window."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    clf = SetupClassifier(_cfg(True))
    ctx = _ctx()
    ctx["market_regime"] = {"primary": "volatility_spike", "bias": "bull"}
    cands = clf.classify_all(_feat(), ctx, _ev(0.8))
    types = [c["setup_type"] for c in cands]
    assert "silver_bullet" not in types  # blocked regime filtered out

# --- iteration 12: stochastic-cross momentum (4th entry model) ---

def _feat_stoch(cross="bullish_cross", symbol="EURUSDm"):
    """Feature dict for stoch-cross setups: stoch_cross set, no rejection/breakout
    and mid-band BB so only stoch-cross detectors can fire."""
    return {
        "symbol": symbol,
        "stoch_cross": cross,
        "rejection": None,
        "breakout": None,
        "bb_position": 0.5,
        "m5_trend": "bullish" if cross == "bullish_cross" else "bearish",
        "price": 1.10, "support": 1.09, "resistance": 1.11,
    }


def test_london_stoch_cross_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_stoch("bullish_cross", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    assert out and out[0]["setup_type"] == "london_stoch_cross"
    assert out[0]["side"] == "BUY"
    assert "07:00-10:00" in out[0]["reason"]


def test_london_stoch_cross_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_stoch("bearish_cross", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    assert out and out[0]["setup_type"] == "london_stoch_cross"
    assert out[0]["side"] == "SELL"


def test_london_stoch_cross_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_stoch("bullish_cross", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_stoch_cross" not in types


def test_london_stoch_cross_skipped_low_momentum(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    ev = _ev(0.8)
    ev["momentum"] = 0.4  # < min_momentum 0.50
    out = detect_specialized(_feat_stoch("bullish_cross", "EURUSDm"), _ctx(), ev, _cfg(True))
    assert out == []


def test_london_stoch_cross_skipped_no_cross(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_stoch("none", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    assert out == []


def test_ny_stoch_cross_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_stoch("bullish_cross", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_stoch_cross" in types
    ny = next(c for c in out if c["setup_type"] == "ny_stoch_cross")
    assert ny["side"] == "BUY"
    assert "14:00-17:00" in ny["reason"]


def test_ny_stoch_cross_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_stoch("bullish_cross", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_stoch_cross" not in types


# --- iteration 12: replay-evidence per-symbol veto ---

def _cfg_veto(veto_map, **kw):
    cfg = _cfg(**kw)
    cfg["signals"]["specialized_setups"]["apply_replay_veto"] = True
    # specialized_config reads the veto from state via _load_replay_veto(); the
    # test injects the veto by monkeypatching _load_replay_veto (see tests below).
    return cfg


def test_replay_veto_blocks_vetoed_setup(monkeypatch):
    """silver_bullet vetoed on XAUUSDm -> even with rejection at 14 UTC, no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    monkeypatch.setattr(spec_mod, "_load_replay_veto",
                        lambda: {"XAUUSDm": ["silver_bullet"]})
    out = detect_specialized(_feat(rejection="bullish_rejection", symbol="XAUUSDm"),
                             _ctx(), _ev(0.8), _cfg_veto({}))
    assert out == []


def test_replay_veto_does_not_affect_non_vetoed_setup(monkeypatch):
    """silver_bullet vetoed on XAUUSDm, but oil_orb (also 14-19 UTC) is not vetoed
    -> oil_orb still fires on a breakout at 14 UTC."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    monkeypatch.setattr(spec_mod, "_load_replay_veto",
                        lambda: {"XAUUSDm": ["silver_bullet"]})
    out = detect_specialized(_feat_bo("breakout", symbol="XAUUSDm"),
                             _ctx(), _ev(0.8), _cfg_veto({}))
    types = [c["setup_type"] for c in out]
    assert "oil_orb" in types
    assert "silver_bullet" not in types


def test_replay_veto_off_by_default(monkeypatch):
    """apply_replay_veto not set -> _load_replay_veto not consulted; a veto map
    injected has no effect, silver_bullet fires normally at 14 UTC."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    called = {"n": 0}
    def _should_not_be_called():
        called["n"] += 1
        return {"XAUUSDm": ["silver_bullet"]}
    monkeypatch.setattr(spec_mod, "_load_replay_veto", _should_not_be_called)
    out = detect_specialized(_feat(), _ctx(), _ev(0.8), _cfg(True))  # no apply_replay_veto
    assert out and out[0]["setup_type"] == "silver_bullet"
    assert called["n"] == 0


def test_replay_veto_shadow_blocks_log(monkeypatch):
    """shadow=true, enabled=false, apply_replay_veto=true -> vetoed setup is not
    even logged to the shadow ledger."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 14)
    logged = []
    monkeypatch.setattr(spec_mod, "append_archive_record",
                        lambda name, rec: logged.append(rec))
    monkeypatch.setattr(spec_mod, "_load_replay_veto",
                        lambda: {"XAUUSDm": ["silver_bullet"]})
    detect_specialized(_feat(), _ctx(), _ev(0.8),
                       _cfg_veto({}, enabled=False, shadow=True))
    assert logged == []


# --- iteration 13: volatility-expansion momentum (5th entry model) ---

def _feat_vol(vol_regime="high", trend="bullish", symbol="EURUSDm"):
    """Feature dict for vol-expansion setups: volatility_regime set, M5 trend set,
    no rejection/breakout/stoch_cross and mid-band BB so only vol-expansion fires."""
    return {
        "symbol": symbol,
        "volatility_regime": vol_regime,
        "m5_trend": trend,
        "rejection": None, "breakout": None, "stoch_cross": "none",
        "bb_position": 0.5,
        "price": 1.10, "support": 1.09, "resistance": 1.11,
    }


def _ev_vol(volatility=0.7):
    ev = _ev(0.8)
    ev["volatility"] = volatility
    ev["momentum"] = 0.6
    return ev


def test_london_vol_expansion_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_vol("high", "bullish", "EURUSDm"), _ctx(), _ev_vol(0.7), _cfg(True))
    assert out and out[0]["setup_type"] == "london_vol_expansion"
    assert out[0]["side"] == "BUY"
    assert "07:00-10:00" in out[0]["reason"]


def test_london_vol_expansion_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_vol("high", "bearish", "EURUSDm"), _ctx(), _ev_vol(0.7), _cfg(True))
    assert out and out[0]["setup_type"] == "london_vol_expansion"
    assert out[0]["side"] == "SELL"


def test_london_vol_expansion_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_vol("high", "bullish", "EURUSDm"), _ctx(), _ev_vol(0.7), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_vol_expansion" not in types


def test_london_vol_expansion_skipped_low_volatility_regime(monkeypatch):
    """volatility_regime != high -> no fire (this is the primary key)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_vol("normal", "bullish", "EURUSDm"), _ctx(), _ev_vol(0.7), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_vol_expansion" not in types


def test_london_vol_expansion_skipped_low_volatility_evidence(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_vol("high", "bullish", "EURUSDm"), _ctx(), _ev_vol(0.4), _cfg(True))
    assert out == []  # volatility 0.4 < min 0.50


def test_london_vol_expansion_fires_at_high_regime_evidence(monkeypatch):
    """Regression: EvidenceEngine maps volatility_regime=='high' -> volatility
    evidence 0.55. A 0.60 gate made the regime check + vol gate mutually exclusive
    (setup never fired in replay). min_volatility=0.50 lets the 0.55 through."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_vol("high", "bullish", "EURUSDm"), _ctx(), _ev_vol(0.55), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_vol_expansion" in types


def test_london_vol_expansion_skipped_neutral_trend(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_vol("high", "neutral", "EURUSDm"), _ctx(), _ev_vol(0.7), _cfg(True))
    assert out == []  # no direction


def test_ny_vol_expansion_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_vol("high", "bullish", "US30m"), _ctx(), _ev_vol(0.7), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_vol_expansion" in types
    ny = next(c for c in out if c["setup_type"] == "ny_vol_expansion")
    assert ny["side"] == "BUY"
    assert "14:00-17:00" in ny["reason"]


def test_ny_vol_expansion_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_vol("high", "bullish", "US30m"), _ctx(), _ev_vol(0.7), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_vol_expansion" not in types


# --- iteration 14: volume-spike confirmation (6th entry model) ---

def _feat_vspike(volume_ratio=1.5, trend="bullish", symbol="EURUSDm"):
    """Feature dict for volume-spike setups: volume_ratio set, M5 trend set,
    no rejection/breakout/stoch_cross, mid-band BB, normal vol regime so only
    the volume-spike setup fires (vol_expansion needs regime==high)."""
    return {
        "symbol": symbol,
        "volume_ratio": volume_ratio,
        "m5_trend": trend,
        "volatility_regime": "normal",
        "rejection": None, "breakout": None, "stoch_cross": "none",
        "bb_position": 0.5,
        "price": 1.10, "support": 1.09, "resistance": 1.11,
    }


def _ev_vspike(volume_ratio=1.5, momentum=0.6):
    """Evidence where volume score matches what EvidenceEngine would produce for
    a given volume_ratio (0.3 + (ratio-0.5)*0.5, capped [0,1])."""
    ev = _ev(0.8)
    ev["volume"] = min(1.0, max(0.0, 0.3 + (volume_ratio - 0.5) * 0.5))
    ev["momentum"] = momentum
    return ev


def test_london_volume_spike_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_vspike(1.6, "bullish", "EURUSDm"), _ctx(), _ev_vspike(1.6), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_volume_spike" in types
    s = next(c for c in out if c["setup_type"] == "london_volume_spike")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]


def test_london_volume_spike_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_vspike(1.8, "bearish", "EURUSDm"), _ctx(), _ev_vspike(1.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_volume_spike" in types
    s = next(c for c in out if c["setup_type"] == "london_volume_spike")
    assert s["side"] == "SELL"


def test_london_volume_spike_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_vspike(1.6, "bullish", "EURUSDm"), _ctx(), _ev_vspike(1.6), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_volume_spike" not in types


def test_london_volume_spike_skipped_low_volume(monkeypatch):
    """volume_ratio below the 1.5x gate -> no fire (this is the primary key)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_vspike(1.2, "bullish", "EURUSDm"), _ctx(), _ev_vspike(1.2), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_volume_spike" not in types


def test_london_volume_spike_skipped_neutral_trend(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_vspike(1.6, "neutral", "EURUSDm"), _ctx(), _ev_vspike(1.6), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_volume_spike" not in types


def test_ny_volume_spike_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_vspike(1.7, "bullish", "US30m"), _ctx(), _ev_vspike(1.7), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_volume_spike" in types
    s = next(c for c in out if c["setup_type"] == "ny_volume_spike")
    assert s["side"] == "BUY"
    assert "14:00-17:00" in s["reason"]


def test_ny_volume_spike_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_vspike(1.7, "bullish", "US30m"), _ctx(), _ev_vspike(1.7), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_volume_spike" not in types


# --- iteration 15: multi-timeframe alignment (7th entry model) ---

def _feat_mtf(aligned=True, m5="bullish", symbol="EURUSDm"):
    """Feature dict for MTF-align setups: timeframe_alignment set, m5_trend set.
    No rejection/breakout/stoch_cross, mid-band BB, normal vol regime + low
    volume_ratio so only the MTF-align setup fires."""
    f = _feat_vspike(0.5, m5, symbol)  # low volume, normal regime, neutral-ish
    f["timeframe_alignment"] = aligned
    f["m5_trend"] = m5
    f["m15_trend"] = m5 if aligned else ("bearish" if m5 == "bullish" else "bullish")
    return f


def _ev_mtf(trend=0.7, momentum=0.6):
    ev = _ev(0.8)
    ev["trend"] = trend
    ev["momentum"] = momentum
    return ev


def test_london_mtf_align_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_mtf(True, "bullish", "EURUSDm"), _ctx(), _ev_mtf(), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_mtf_align" in types
    s = next(c for c in out if c["setup_type"] == "london_mtf_align")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]


def test_london_mtf_align_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_mtf(True, "bearish", "EURUSDm"), _ctx(), _ev_mtf(), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_mtf_align" in types
    s = next(c for c in out if c["setup_type"] == "london_mtf_align")
    assert s["side"] == "SELL"


def test_london_mtf_align_skipped_not_aligned(monkeypatch):
    """timeframe_alignment == False -> no fire (this is the primary key)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_mtf(False, "bullish", "EURUSDm"), _ctx(), _ev_mtf(), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_mtf_align" not in types


def test_london_mtf_align_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_mtf(True, "bullish", "EURUSDm"), _ctx(), _ev_mtf(), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_mtf_align" not in types


def test_london_mtf_align_skipped_weak_trend(monkeypatch):
    """trend evidence below the 0.50 gate -> no fire (filters flat aligned trends)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_mtf(True, "bullish", "EURUSDm"), _ctx(), _ev_mtf(trend=0.3), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_mtf_align" not in types


def test_london_mtf_align_skipped_neutral_trend(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_mtf(True, "neutral", "EURUSDm"), _ctx(), _ev_mtf(), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_mtf_align" not in types


def test_ny_mtf_align_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_mtf(True, "bullish", "US30m"), _ctx(), _ev_mtf(), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_mtf_align" in types
    s = next(c for c in out if c["setup_type"] == "ny_mtf_align")
    assert s["side"] == "BUY"
    assert "14:00-17:00" in s["reason"]


def test_ny_mtf_align_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_mtf(True, "bullish", "US30m"), _ctx(), _ev_mtf(), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_mtf_align" not in types


# --- iteration 16: stochastic oversold/overbought reversion (8th entry model) ---

def _feat_stochr(k=15, symbol="EURUSDm"):
    """Feature dict for stoch-reversion setups: stoch_k set at an extreme.
    No rejection/breakout, mid-band BB, no stoch_cross, normal vol regime, low
    volume, neutral m5_trend so only the stoch-reversion setup fires."""
    f = _feat_mtf(False, "neutral", symbol)  # not aligned, neutral trend, low vol
    f["stoch_k"] = k
    f["stoch_cross"] = "none"
    return f


def test_london_stoch_reversion_fires_oversold(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_stochr(12, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_stoch_reversion" in types
    s = next(c for c in out if c["setup_type"] == "london_stoch_reversion")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]


def test_london_stoch_reversion_fires_overbought(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_stochr(88, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_stoch_reversion" in types
    s = next(c for c in out if c["setup_type"] == "london_stoch_reversion")
    assert s["side"] == "SELL"


def test_london_stoch_reversion_skipped_mid_range(monkeypatch):
    """stoch_k in the middle (50) -> no fire (this is the primary key)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_stochr(50, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_stoch_reversion" not in types


def test_london_stoch_reversion_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_stochr(12, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_stoch_reversion" not in types


def test_london_stoch_reversion_boundary_oversold_inclusive(monkeypatch):
    """stoch_k == 20 (exactly oversold) -> fires BUY (<= is inclusive)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_stochr(20, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_stoch_reversion" in types


def test_london_stoch_reversion_boundary_overbought_inclusive(monkeypatch):
    """stoch_k == 80 (exactly overbought) -> fires SELL (>= is inclusive)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_stochr(80, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_stoch_reversion" in types


def test_ny_stoch_reversion_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_stochr(85, "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_stoch_reversion" in types
    s = next(c for c in out if c["setup_type"] == "ny_stoch_reversion")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_stoch_reversion_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_stochr(85, "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_stoch_reversion" not in types


# --- iteration 18: HTF-trend-filtered breakout (9th entry model) -------------

def _feat_htf(bo="breakout", m15="bullish", symbol="EURUSDm"):
    """Feature dict for HTF-breakout setups: breakout state + m15_trend DIRECTION
    (the primary key) aligned to the breakout direction. Built on _feat_bo so the
    breakout state + m5_trend are set; m15_trend direction is the new key."""
    f = _feat_bo(bo, symbol)
    f["m15_trend"] = m15
    return f


def test_london_htf_breakout_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_htf("breakout", "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_htf_breakout" in types
    s = next(c for c in out if c["setup_type"] == "london_htf_breakout")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]


def test_london_htf_breakout_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_htf("breakdown", "bearish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_htf_breakout" in types
    s = next(c for c in out if c["setup_type"] == "london_htf_breakout")
    assert s["side"] == "SELL"


def test_london_htf_breakout_skipped_when_m15_disagrees(monkeypatch):
    """M15 trend opposite to breakout direction -> no fire (m15 direction is the
    primary key filter)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_htf("breakout", "bearish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_htf_breakout" not in types


def test_london_htf_breakout_skipped_no_breakout_state(monkeypatch):
    """No breakout/breakdown state -> no fire (needs the structural break)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    f = _feat_htf("breakout", "bullish", "EURUSDm")
    f["breakout"] = None
    out = detect_specialized(f, _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_htf_breakout" not in types


def test_london_htf_breakout_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_htf("breakout", "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_htf_breakout" not in types


def test_ny_htf_breakout_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_htf("breakdown", "bearish", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_htf_breakout" in types
    s = next(c for c in out if c["setup_type"] == "ny_htf_breakout")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_htf_breakout_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_htf("breakout", "bullish", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_htf_breakout" not in types


# --- iteration 19: BB-squeeze breakout (10th entry model) -------------------

def _feat_squeeze(bo="breakout", squeeze=0.10, symbol="EURUSDm"):
    """Feature dict for squeeze-breakout setups: bb_squeeze_pct (compression
    percentile, low = tight squeeze) + breakout state (the primary keys). Built
    on _feat_htf so the breakout state is set; bb_squeeze_pct is the new key."""
    f = _feat_htf(bo, "bullish" if bo == "breakout" else "bearish", symbol)
    f["bb_squeeze_pct"] = squeeze
    return f


def test_london_squeeze_breakout_fires_compressed_breakout(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_squeeze("breakout", 0.10, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_squeeze_breakout" in types
    s = next(c for c in out if c["setup_type"] == "london_squeeze_breakout")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "squeeze" in s["reason"].lower()


def test_london_squeeze_breakout_fires_breakdown_sell(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_squeeze("breakdown", 0.05, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_squeeze_breakout" in types
    s = next(c for c in out if c["setup_type"] == "london_squeeze_breakout")
    assert s["side"] == "SELL"


def test_london_squeeze_breakout_skipped_when_not_compressed(monkeypatch):
    """bb_squeeze_pct above the 0.20 threshold -> no squeeze -> no fire (the
    compression percentile is the primary key)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_squeeze("breakout", 0.50, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_squeeze_breakout" not in types


def test_london_squeeze_breakout_skipped_no_breakout_state(monkeypatch):
    """Compressed but no structural breakout -> no fire (needs the release)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    f = _feat_squeeze("breakout", 0.10, "EURUSDm")
    f["breakout"] = None
    out = detect_specialized(f, _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_squeeze_breakout" not in types


def test_london_squeeze_breakout_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_squeeze("breakout", 0.10, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_squeeze_breakout" not in types


def test_ny_squeeze_breakout_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_squeeze("breakdown", 0.08, "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_squeeze_breakout" in types
    s = next(c for c in out if c["setup_type"] == "ny_squeeze_breakout")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_squeeze_breakout_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_squeeze("breakout", 0.10, "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_squeeze_breakout" not in types


def test_squeeze_breakout_threshold_boundary_inclusive(monkeypatch):
    """bb_squeeze_pct == 0.20 (exactly the threshold) -> fires (<= is inclusive)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_squeeze("breakout", 0.20, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_squeeze_breakout" in types


# --- iteration 20: volume-confirmed breakout (11th entry model) ------------

def _feat_vbreakout(bo="breakout", vr=1.6, symbol="EURUSDm"):
    """Feature dict for volume-breakout setups: volume_ratio (volume
    confirmation) + breakout state (the primary compound keys). Built on
    _feat_squeeze so breakout + a neutral bb_squeeze_pct (0.5, no squeeze) are
    set, then volume_ratio is the new key (squeeze stays inactive at 0.5)."""
    f = _feat_squeeze(bo, 0.5, symbol)  # 0.5 = not compressed -> squeeze inactive
    f["volume_ratio"] = vr
    return f


def test_london_volume_breakout_fires_volume_confirmed_breakout(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_vbreakout("breakout", 1.6, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_volume_breakout" in types
    s = next(c for c in out if c["setup_type"] == "london_volume_breakout")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "volume" in s["reason"].lower()


def test_london_volume_breakout_fires_breakdown_sell(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_vbreakout("breakdown", 2.0, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_volume_breakout" in types
    s = next(c for c in out if c["setup_type"] == "london_volume_breakout")
    assert s["side"] == "SELL"


def test_london_volume_breakout_skipped_low_volume(monkeypatch):
    """volume_ratio below the 1.5 threshold -> no fire (volume confirmation is
    the primary key)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_vbreakout("breakout", 1.2, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_volume_breakout" not in types


def test_london_volume_breakout_skipped_no_breakout_state(monkeypatch):
    """High volume but no structural breakout -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    f = _feat_vbreakout("breakout", 1.6, "EURUSDm")
    f["breakout"] = None
    out = detect_specialized(f, _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_volume_breakout" not in types


def test_london_volume_breakout_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_vbreakout("breakout", 1.6, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_volume_breakout" not in types


def test_ny_volume_breakout_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_vbreakout("breakdown", 1.8, "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_volume_breakout" in types
    s = next(c for c in out if c["setup_type"] == "ny_volume_breakout")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_volume_breakout_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_vbreakout("breakout", 1.6, "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_volume_breakout" not in types


def test_volume_breakout_threshold_boundary_inclusive(monkeypatch):
    """volume_ratio == 1.5 (exactly the threshold) -> fires (>= is inclusive)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_vbreakout("breakout", 1.5, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_volume_breakout" in types


# --- iteration 21: RSI oversold/overbought reversion (12th entry model) -----

def _feat_rsi(rsi=25, symbol="EURUSDm"):
    """Feature dict for RSI-reversion setups: rsi set at an extreme (the primary
    key). Built on _feat_stochr(50) so stoch_k is mid-range (stoch_reversion
    inactive), not aligned (mtf_align inactive), neutral trend, low volume_ratio
    (volume_spike/volume_breakout inactive), normal vol regime (vol_expansion
    inactive), no rejection/breakout, mid-band BB (bb_reversion inactive) — only
    the RSI-reversion setup can fire when rsi is at an extreme."""
    f = _feat_stochr(50, symbol)  # stoch_k=50 mid-range -> stoch_reversion inactive
    f["rsi"] = rsi
    return f


def test_london_rsi_reversion_fires_oversold(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_rsi(22, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_rsi_reversion" in types
    s = next(c for c in out if c["setup_type"] == "london_rsi_reversion")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "RSI" in s["reason"]


def test_london_rsi_reversion_fires_overbought(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_rsi(78, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_rsi_reversion" in types
    s = next(c for c in out if c["setup_type"] == "london_rsi_reversion")
    assert s["side"] == "SELL"


def test_london_rsi_reversion_skipped_mid_range(monkeypatch):
    """rsi in the middle (50) -> no fire (rsi level is the primary key)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_rsi(50, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_rsi_reversion" not in types


def test_london_rsi_reversion_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_rsi(22, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_rsi_reversion" not in types


def test_london_rsi_reversion_boundary_oversold_inclusive(monkeypatch):
    """rsi == 30 (exactly oversold) -> fires BUY (<= is inclusive)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_rsi(30, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_rsi_reversion" in types


def test_london_rsi_reversion_boundary_overbought_inclusive(monkeypatch):
    """rsi == 70 (exactly overbought) -> fires SELL (>= is inclusive)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_rsi(70, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_rsi_reversion" in types


def test_ny_rsi_reversion_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_rsi(82, "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_rsi_reversion" in types
    s = next(c for c in out if c["setup_type"] == "ny_rsi_reversion")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_rsi_reversion_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_rsi(82, "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_rsi_reversion" not in types


def test_rsi_reversion_distinct_from_stoch_reversion(monkeypatch):
    """When stoch_k is mid-range (50, stoch_reversion inactive) but rsi is
    oversold, only the RSI-reversion setup fires — confirms the two oscillators
    are independent entry models (the iter-21 distinction from iter-16)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_rsi(22, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_rsi_reversion" in types
    assert "london_stoch_reversion" not in types


# --- iteration 22: MACD signal-line-cross momentum (13th entry model) --------

def _feat_macd(cross="bullish_cross", symbol="EURUSDm"):
    """Feature dict for MACD-cross setups: macd_cross set (the primary key),
    stoch_cross neutralized to 'none' so the stoch-cross setup (same window,
    different oscillator) does not co-fire. Built on _feat_stoch then override:
    mid-band BB (bb_reversion inactive), no rejection/breakout, m5_trend aligned
    to the cross direction. rsi left at neutral 50 (rsi_reversion inactive)."""
    f = _feat_stoch(cross, symbol)  # sets stoch_cross, m5_trend, bb_position=0.5
    f["stoch_cross"] = "none"  # neutralize stoch_cross so only MACD-cross fires
    f["rsi"] = 50.0  # neutral RSI so rsi_reversion stays inactive
    f["macd_cross"] = cross
    return f


def test_london_macd_cross_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_macd("bullish_cross", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_macd_cross" in types
    s = next(c for c in out if c["setup_type"] == "london_macd_cross")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "MACD" in s["reason"]


def test_london_macd_cross_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_macd("bearish_cross", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_macd_cross" in types
    s = next(c for c in out if c["setup_type"] == "london_macd_cross")
    assert s["side"] == "SELL"


def test_london_macd_cross_skipped_no_cross(monkeypatch):
    """macd_cross == 'none' -> no fire (the cross event is the primary key)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_macd("none", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_macd_cross" not in types


def test_london_macd_cross_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_macd("bullish_cross", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_macd_cross" not in types


def test_london_macd_cross_skipped_low_momentum(monkeypatch):
    """momentum evidence below the 0.50 gate -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    ev = _ev(0.8)
    ev["momentum"] = 0.4  # < min_momentum 0.50
    out = detect_specialized(_feat_macd("bullish_cross", "EURUSDm"), _ctx(), ev, _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_macd_cross" not in types


def test_ny_macd_cross_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_macd("bearish_cross", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_macd_cross" in types
    s = next(c for c in out if c["setup_type"] == "ny_macd_cross")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_macd_cross_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_macd("bullish_cross", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_macd_cross" not in types


def test_macd_cross_distinct_from_stoch_cross(monkeypatch):
    """When stoch_cross is 'none' (stoch_cross inactive) but macd_cross is
    bullish, only the MACD-cross setup fires — confirms the two oscillator
    crosses are independent entry models (the iter-22 distinction from
    iter-12)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_macd("bullish_cross", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_macd_cross" in types
    assert "london_stoch_cross" not in types


# --- iteration 23: CCI oversold/overbought reversion (14th entry model) ------

def _feat_cci(cci=-120, symbol="EURUSDm"):
    """Feature dict for CCI-reversion setups: cci set at an extreme (the primary
    key). Built on _feat_rsi(50) so rsi is neutral (rsi_reversion inactive),
    stoch_k mid-range (stoch_reversion inactive), not aligned (mtf_align
    inactive), neutral trend, low volume_ratio, normal vol regime, no
    rejection/breakout, mid-band BB — only the CCI-reversion setup can fire
    when cci is at an extreme. macd_cross unset -> None -> macd_cross inactive."""
    f = _feat_rsi(50, symbol)  # rsi=50 neutral, stoch_k=50 mid-range, all else neutral
    f["cci"] = cci
    return f


def test_london_cci_reversion_fires_oversold(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_cci(-130, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_cci_reversion" in types
    s = next(c for c in out if c["setup_type"] == "london_cci_reversion")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "CCI" in s["reason"]


def test_london_cci_reversion_fires_overbought(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_cci(140, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_cci_reversion" in types
    s = next(c for c in out if c["setup_type"] == "london_cci_reversion")
    assert s["side"] == "SELL"


def test_london_cci_reversion_skipped_mid_range(monkeypatch):
    """cci in the middle (0) -> no fire (cci level is the primary key)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_cci(0, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_cci_reversion" not in types


def test_london_cci_reversion_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_cci(-130, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_cci_reversion" not in types


def test_london_cci_reversion_boundary_oversold_inclusive(monkeypatch):
    """cci == -100 (exactly oversold) -> fires BUY (<= is inclusive)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_cci(-100, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_cci_reversion" in types


def test_london_cci_reversion_boundary_overbought_inclusive(monkeypatch):
    """cci == +100 (exactly overbought) -> fires SELL (>= is inclusive)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_cci(100, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_cci_reversion" in types


def test_ny_cci_reversion_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_cci(160, "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_cci_reversion" in types
    s = next(c for c in out if c["setup_type"] == "ny_cci_reversion")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_cci_reversion_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_cci(160, "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_cci_reversion" not in types


def test_cci_reversion_distinct_from_rsi_reversion(monkeypatch):
    """When rsi is neutral (50, rsi_reversion inactive) but cci is oversold,
    only the CCI-reversion setup fires — confirms CCI and RSI are independent
    entry models (the iter-23 distinction from iter-21)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_cci(-130, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_cci_reversion" in types
    assert "london_rsi_reversion" not in types


# --- iteration 24: MFI oversold/overbought reversion (15th entry model) ------

def _feat_mfi(mfi=15, symbol="EURUSDm"):
    """Feature dict for MFI-reversion setups: mfi set at an extreme (the primary
    key). Built on _feat_cci(0) so cci is neutral (cci_reversion inactive), rsi
    neutral (rsi_reversion inactive), stoch_k mid-range (stoch_reversion
    inactive), not aligned (mtf_align inactive), neutral trend, low volume_ratio,
    normal vol regime, no rejection/breakout, mid-band BB — only the MFI-reversion
    setup can fire when mfi is at an extreme."""
    f = _feat_cci(0, symbol)  # cci=0 neutral, rsi=50, stoch_k=50, all else neutral
    f["mfi"] = mfi
    return f


def test_london_mfi_reversion_fires_oversold(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_mfi(10, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_mfi_reversion" in types
    s = next(c for c in out if c["setup_type"] == "london_mfi_reversion")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "MFI" in s["reason"]


def test_london_mfi_reversion_fires_overbought(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_mfi(88, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_mfi_reversion" in types
    s = next(c for c in out if c["setup_type"] == "london_mfi_reversion")
    assert s["side"] == "SELL"


def test_london_mfi_reversion_skipped_mid_range(monkeypatch):
    """mfi in the middle (50) -> no fire (mfi level is the primary key)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_mfi(50, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_mfi_reversion" not in types


def test_london_mfi_reversion_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_mfi(10, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_mfi_reversion" not in types


def test_london_mfi_reversion_boundary_oversold_inclusive(monkeypatch):
    """mfi == 20 (exactly oversold) -> fires BUY (<= is inclusive)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_mfi(20, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_mfi_reversion" in types


def test_london_mfi_reversion_boundary_overbought_inclusive(monkeypatch):
    """mfi == 80 (exactly overbought) -> fires SELL (>= is inclusive)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_mfi(80, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_mfi_reversion" in types


def test_ny_mfi_reversion_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_mfi(92, "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_mfi_reversion" in types
    s = next(c for c in out if c["setup_type"] == "ny_mfi_reversion")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_mfi_reversion_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_mfi(92, "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_mfi_reversion" not in types


def test_mfi_reversion_distinct_from_rsi_reversion(monkeypatch):
    """When rsi is neutral (50, rsi_reversion inactive) but mfi is oversold,
    only the MFI-reversion setup fires — confirms MFI and RSI are independent
    entry models (the iter-24 distinction: MFI is volume-weighted, RSI is
    pure-price)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_mfi(10, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_mfi_reversion" in types
    assert "london_rsi_reversion" not in types


def _feat_adx(adx=30, di_plus=25, di_minus=15, symbol="EURUSDm"):
    """Feature dict for ADX-trend setups: adx + di_plus/di_minus set (the
    primary keys). Built on _feat_mfi(50) so mfi is neutral (mfi_reversion
    inactive), cci neutral, rsi neutral, stoch_k mid-range, not aligned
    (mtf_align inactive), neutral trend, low volume_ratio, normal vol regime,
    no rejection/breakout, mid-band BB — only the ADX-trend setup can fire
    when adx >= 25 and the DI lines diverge."""
    f = _feat_mfi(50, symbol)  # mfi=50 neutral, all oscillators neutral
    f["adx"] = adx
    f["di_plus"] = di_plus
    f["di_minus"] = di_minus
    return f


def test_london_adx_trend_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_adx(30, 25, 15, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_trend" in types
    s = next(c for c in out if c["setup_type"] == "london_adx_trend")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "ADX" in s["reason"]


def test_london_adx_trend_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_adx(28, 10, 22, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_trend" in types
    s = next(c for c in out if c["setup_type"] == "london_adx_trend")
    assert s["side"] == "SELL"


def test_london_adx_trend_skipped_weak_adx(monkeypatch):
    """adx < 25 (weak/no trend) -> no fire (adx strength is the primary key)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_adx(20, 25, 15, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_trend" not in types


def test_london_adx_trend_boundary_adx_inclusive(monkeypatch):
    """adx == 25 (exactly min_adx) -> fires (>= is inclusive)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_adx(25, 25, 15, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_trend" in types


def test_london_adx_trend_skipped_di_equal(monkeypatch):
    """adx strong but +DI == -DI (no directional dominance) -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_adx(30, 20, 20, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_trend" not in types


def test_london_adx_trend_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_adx(30, 25, 15, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_trend" not in types


def test_ny_adx_trend_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_adx(32, 12, 24, "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_adx_trend" in types
    s = next(c for c in out if c["setup_type"] == "ny_adx_trend")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_adx_trend_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_adx(32, 25, 15, "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_adx_trend" not in types


def test_adx_trend_distinct_from_mtf_align(monkeypatch):
    """When adx is strong but mtf alignment is off (mtf_align inactive), only
    the ADX-trend setup fires — confirms ADX is an independent entry model
    (the iter-25 distinction: ADX is trend STRENGTH, mtf_align is
    multi-timeframe directional alignment)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_adx(30, 25, 15, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_trend" in types
    assert "london_mtf_align" not in types


def _feat_obv(obv_cross="bullish_cross", symbol="EURUSDm"):
    """Feature dict for OBV-cross setups: obv_cross set (the primary key).
    Built on _feat_adx(20, 20, 20) so adx is weak (<25 → adx_trend inactive)
    AND di_plus == di_minus (no directional dominance), mfi neutral
    (mfi_reversion inactive), cci neutral, rsi neutral, stoch_k mid-range,
    not aligned (mtf_align inactive), neutral trend, low volume_ratio, normal
    vol regime, no rejection/breakout, mid-band BB — only the OBV-cross setup
    can fire when obv_cross is a real cross event."""
    f = _feat_adx(20, 20, 20, symbol)  # adx weak + di equal → adx_trend inactive
    f["obv_cross"] = obv_cross
    return f


def test_london_obv_cross_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_obv("bullish_cross", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_obv_cross" in types
    s = next(c for c in out if c["setup_type"] == "london_obv_cross")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "OBV" in s["reason"]


def test_london_obv_cross_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_obv("bearish_cross", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_obv_cross" in types
    s = next(c for c in out if c["setup_type"] == "london_obv_cross")
    assert s["side"] == "SELL"


def test_london_obv_cross_skipped_no_cross(monkeypatch):
    """obv_cross == none (OBV hasn't crossed its EMA this bar) -> no fire
    (the cross event is the primary key)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_obv("none", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_obv_cross" not in types


def test_london_obv_cross_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_obv("bullish_cross", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_obv_cross" not in types


def test_ny_obv_cross_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_obv("bearish_cross", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_obv_cross" in types
    s = next(c for c in out if c["setup_type"] == "ny_obv_cross")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_obv_cross_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_obv("bullish_cross", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_obv_cross" not in types


def test_obv_cross_distinct_from_macd_cross(monkeypatch):
    """When macd_cross is unset (None → macd_cross inactive) but obv_cross is a
    real cross, only the OBV-cross setup fires — confirms OBV is an independent
    entry model (the iter-26 distinction: OBV is volume-accumulation,
    macd_cross is a price-EMA-difference momentum cross)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_obv("bullish_cross", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_obv_cross" in types
    assert "london_macd_cross" not in types


def test_obv_cross_distinct_from_adx_trend(monkeypatch):
    """When obv_cross is set but adx is weak (adx_trend inactive), only the
    OBV-cross setup fires — confirms OBV and ADX are independent entry models
    (the iter-26 distinction: OBV is volume-accumulation, ADX is trend
    strength)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_obv("bullish_cross", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_obv_cross" in types
    assert "london_adx_trend" not in types


def _feat_atrpct(atr_pct=0.85, m5_trend="bullish", symbol="EURUSDm"):
    """Feature dict for ATR-pct-breakout setups: atr_pct + m5_trend set (the
    primary keys). Built on _feat_obv("none") so obv_cross is none (OBV
    inactive), adx weak + di equal (adx_trend inactive), mfi neutral, cci
    neutral, rsi neutral, stoch_k mid-range, not aligned (mtf_align inactive),
    low volume_ratio (volume_spike/volume_breakout inactive), normal vol
    (volatility_regime unset → vol_expansion inactive), no rejection-driven
    breakout — only the ATR-pct setup can fire when atr_pct >= 0.70 and m5_trend
    is directional."""
    f = _feat_obv("none", symbol)  # obv_cross=none → OBV inactive
    f["atr_pct"] = atr_pct
    f["m5_trend"] = m5_trend
    return f


def test_london_atr_pct_breakout_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_atrpct(0.85, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_atr_pct_breakout" in types
    s = next(c for c in out if c["setup_type"] == "london_atr_pct_breakout")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "ATR-pct" in s["reason"]


def test_london_atr_pct_breakout_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_atrpct(0.78, "bearish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_atr_pct_breakout" in types
    s = next(c for c in out if c["setup_type"] == "london_atr_pct_breakout")
    assert s["side"] == "SELL"


def test_london_atr_pct_breakout_skipped_low_pct(monkeypatch):
    """atr_pct < 0.70 (vol not expanding relative to history) -> no fire
    (the relative-vol rank is the primary key)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_atrpct(0.50, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_atr_pct_breakout" not in types


def test_london_atr_pct_breakout_boundary_inclusive(monkeypatch):
    """atr_pct == 0.70 (exactly min) -> fires (>= is inclusive)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_atrpct(0.70, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_atr_pct_breakout" in types


def test_london_atr_pct_breakout_skipped_neutral_trend(monkeypatch):
    """atr_pct high but m5_trend neutral (no direction) -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_atrpct(0.85, "neutral", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_atr_pct_breakout" not in types


def test_london_atr_pct_breakout_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_atrpct(0.85, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_atr_pct_breakout" not in types


def test_ny_atr_pct_breakout_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_atrpct(0.80, "bearish", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_atr_pct_breakout" in types
    s = next(c for c in out if c["setup_type"] == "ny_atr_pct_breakout")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_atr_pct_breakout_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_atrpct(0.80, "bullish", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_atr_pct_breakout" not in types


def test_atr_pct_breakout_distinct_from_vol_expansion(monkeypatch):
    """When atr_pct is high but volatility_regime is unset (vol_expansion
    inactive — it requires the absolute 'high' regime), only the ATR-pct setup
    fires — confirms ATR-pct is an independent entry model (the iter-27
    distinction: atr_pct is a relative-vol RANK gate, vol_expansion is an
    absolute atr_ratio gate)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_atrpct(0.85, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_atr_pct_breakout" in types
    assert "london_vol_expansion" not in types


def _feat_cmf(cmf=0.25, m5_trend="bullish", symbol="EURUSDm"):
    """Feature dict for CMF-continuation setups: cmf + m5_trend set (the
    primary keys). Built on _feat_atrpct("bullish") with atr_pct LOW (0.40 <
    0.70 so ATR-pct inactive) so only the CMF setup can fire when cmf >= +0.10
    (accumulation) + m5_trend bullish, or cmf <= -0.10 (distribution) +
    bearish. obv_cross none, adx weak, mfi neutral, cci neutral, rsi neutral,
    stoch mid-range, not aligned, low volume_ratio, normal vol, no
    rejection-driven breakout."""
    f = _feat_atrpct(0.40, m5_trend, symbol)  # atr_pct low → ATR-pct inactive
    f["cmf"] = cmf
    f["m5_trend"] = m5_trend
    return f


def test_london_cmf_continuation_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_cmf(0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_cmf_continuation" in types
    s = next(c for c in out if c["setup_type"] == "london_cmf_continuation")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "CMF" in s["reason"]
    assert "accumulation" in s["reason"]


def test_london_cmf_continuation_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_cmf(-0.22, "bearish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_cmf_continuation" in types
    s = next(c for c in out if c["setup_type"] == "london_cmf_continuation")
    assert s["side"] == "SELL"
    assert "distribution" in s["reason"]


def test_london_cmf_continuation_skipped_weak_accumulation(monkeypatch):
    """cmf between -0.10 and +0.10 (neutral pressure) + bullish -> no fire
    (the money-flow pressure magnitude is the primary key)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_cmf(0.05, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_cmf_continuation" not in types


def test_london_cmf_continuation_boundary_inclusive(monkeypatch):
    """cmf == +0.10 (exactly min accumulation) + bullish -> fires (>= inclusive)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_cmf(0.10, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_cmf_continuation" in types


def test_london_cmf_continuation_skipped_trend_pressure_mismatch(monkeypatch):
    """cmf positive (accumulation) but m5_trend bearish -> no fire (pressure
    must agree with trend direction)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_cmf(0.25, "bearish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_cmf_continuation" not in types


def test_london_cmf_continuation_skipped_neutral_trend(monkeypatch):
    """cmf strong accumulation but m5_trend neutral (no direction) -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_cmf(0.25, "neutral", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_cmf_continuation" not in types


def test_london_cmf_continuation_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_cmf(0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_cmf_continuation" not in types


def test_ny_cmf_continuation_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_cmf(-0.30, "bearish", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_cmf_continuation" in types
    s = next(c for c in out if c["setup_type"] == "ny_cmf_continuation")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]
    assert "distribution" in s["reason"]


def test_ny_cmf_continuation_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_cmf(0.25, "bullish", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_cmf_continuation" not in types


def test_cmf_continuation_distinct_from_other_volume_models(monkeypatch):
    """When cmf is strong accumulation + bullish but obv_cross is none, mfi
    neutral, volume_ratio low (the other three volume models inactive), only
    the CMF setup fires — confirms CMF is an independent 4th volume entry
    model (the iter-28 distinction: intrabar close-location * volume pressure,
    vs volume_ratio level / mfi price-change ratio / obv cumulative line)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_cmf(0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_cmf_continuation" in types
    assert "london_obv_cross" not in types
    assert "london_mfi_reversion" not in types
    assert "london_volume_spike" not in types
    assert "london_volume_breakout" not in types


def _feat_triple(adx=30, atr_pct=0.85, cmf=0.25, m5_trend="bullish", symbol="EURUSDm"):
    """Feature dict for triple-confirm setups: adx + atr_pct + cmf + m5_trend
    all set (the 3-way conjunction is the primary key). Built on _feat_cmf
    (cmf + m5_trend set, atr_pct low 0.40 so ATR-pct inactive, adx weak so
    ADX-trend inactive) then overrides adx strong + atr_pct high + di lines so
    all three dims hold. obv_cross none, mfi neutral, cci neutral, rsi neutral,
    stoch mid-range, not aligned, low volume_ratio, normal vol, no
    rejection-driven breakout — only the triple-confirm (and possibly the
    single-dim adx/atr_pct/cmf setups, which the tests account for) can fire."""
    f = _feat_cmf(cmf, m5_trend, symbol)  # cmf + m5_trend set, atr_pct=0.40, adx weak
    f["adx"] = adx
    f["di_plus"] = 25
    f["di_minus"] = 15
    f["atr_pct"] = atr_pct
    return f


def test_london_triple_confirm_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_triple(30, 0.85, 0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_triple_confirm" in types
    s = next(c for c in out if c["setup_type"] == "london_triple_confirm")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "ADX" in s["reason"]
    assert "ATR-pct" in s["reason"]
    assert "CMF" in s["reason"]
    assert "triple confirmation" in s["reason"]


def test_london_triple_confirm_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_triple(28, 0.78, -0.22, "bearish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_triple_confirm" in types
    s = next(c for c in out if c["setup_type"] == "london_triple_confirm")
    assert s["side"] == "SELL"
    assert "distribution" in s["reason"]


def test_london_triple_confirm_skipped_weak_adx(monkeypatch):
    """adx < 25 (one of the 3 dims fails) -> no triple fire (conjunction)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_triple(20, 0.85, 0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_triple_confirm" not in types


def test_london_triple_confirm_skipped_low_atr_pct(monkeypatch):
    """atr_pct < 0.70 (one of the 3 dims fails) -> no triple fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_triple(30, 0.50, 0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_triple_confirm" not in types


def test_london_triple_confirm_skipped_weak_cmf(monkeypatch):
    """cmf between -0.10 and +0.10 (one of the 3 dims fails) -> no triple fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_triple(30, 0.85, 0.05, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_triple_confirm" not in types


def test_london_triple_confirm_skipped_trend_pressure_mismatch(monkeypatch):
    """cmf positive (accumulation) but m5_trend bearish -> pressure disagrees
    with trend -> no triple fire (all 3 must AGREE)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_triple(30, 0.85, 0.25, "bearish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_triple_confirm" not in types


def test_london_triple_confirm_skipped_neutral_trend(monkeypatch):
    """all 3 dims strong but m5_trend neutral (no direction) -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_triple(30, 0.85, 0.25, "neutral", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_triple_confirm" not in types


def test_london_triple_confirm_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_triple(30, 0.85, 0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_triple_confirm" not in types


def test_ny_triple_confirm_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_triple(28, 0.80, -0.30, "bearish", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_triple_confirm" in types
    s = next(c for c in out if c["setup_type"] == "ny_triple_confirm")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_triple_confirm_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_triple(30, 0.85, 0.25, "bullish", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_triple_confirm" not in types


def test_triple_confirm_distinct_from_single_dim_setups(monkeypatch):
    """When atr_pct is dropped below 0.70 (one dim fails) the triple-confirm
    stops firing even though adx + cmf still hold (the single-dim adx_trend
    and cmf_continuation may still fire) — confirms the triple-confirm is a
    genuine CONJUNCTION entry model (the iter-29 distinction: requires all 3
    dims at once, not any subset)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    full = detect_specialized(_feat_triple(30, 0.85, 0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    assert "london_triple_confirm" in [c["setup_type"] for c in full]
    # drop atr_pct below threshold -> triple stops
    dropped = detect_specialized(_feat_triple(30, 0.50, 0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    assert "london_triple_confirm" not in [c["setup_type"] for c in dropped]


def _feat_adx_cmf(adx=30, cmf=0.25, m5_trend="bullish", symbol="EURUSDm"):
    """Feature dict for ADX+CMF 2-way-compound setups: adx + cmf + m5_trend set
    (the 2-way conjunction is the primary key). Built on _feat_cmf (cmf +
    m5_trend set, atr_pct LOW 0.40 so ATR-pct + triple inactive, adx weak) then
    overrides adx strong + di lines. Only the ADX+CMF compound (and the
    single-dim adx_trend / cmf_continuation, which the tests account for) can
    fire."""
    f = _feat_cmf(cmf, m5_trend, symbol)  # cmf + m5_trend set, atr_pct=0.40, adx weak
    f["adx"] = adx
    f["di_plus"] = 25
    f["di_minus"] = 15
    return f


def test_london_adx_cmf_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_adx_cmf(30, 0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_cmf" in types
    s = next(c for c in out if c["setup_type"] == "london_adx_cmf")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "ADX" in s["reason"]
    assert "CMF" in s["reason"]
    assert "2-way confluence" in s["reason"]


def test_london_adx_cmf_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_adx_cmf(28, -0.22, "bearish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_cmf" in types
    s = next(c for c in out if c["setup_type"] == "london_adx_cmf")
    assert s["side"] == "SELL"
    assert "distribution" in s["reason"]


def test_london_adx_cmf_skipped_weak_adx(monkeypatch):
    """adx < 25 (one of the 2 dims fails) -> no fire (conjunction)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_adx_cmf(20, 0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_cmf" not in types


def test_london_adx_cmf_skipped_weak_cmf(monkeypatch):
    """cmf between -0.10 and +0.10 (one of the 2 dims fails) -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_adx_cmf(30, 0.05, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_cmf" not in types


def test_london_adx_cmf_skipped_trend_pressure_mismatch(monkeypatch):
    """cmf positive (accumulation) but m5_trend bearish -> pressure disagrees
    -> no fire (the 2 dims must AGREE)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_adx_cmf(30, 0.25, "bearish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_cmf" not in types


def test_london_adx_cmf_skipped_neutral_trend(monkeypatch):
    """both dims strong but m5_trend neutral -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_adx_cmf(30, 0.25, "neutral", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_cmf" not in types


def test_london_adx_cmf_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_adx_cmf(30, 0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_cmf" not in types


def test_ny_adx_cmf_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_adx_cmf(28, -0.30, "bearish", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_adx_cmf" in types
    s = next(c for c in out if c["setup_type"] == "ny_adx_cmf")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_adx_cmf_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_adx_cmf(30, 0.25, "bullish", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_adx_cmf" not in types


def test_adx_cmf_distinct_from_triple_confirm(monkeypatch):
    """The ADX+CMF 2-way compound does NOT require atr_pct (unlike the triple).
    With atr_pct LOW (0.40 < 0.70, triple inactive) but adx strong + cmf in
    direction, the ADX+CMF compound still fires — confirms it is a genuine
    2-way conjunction distinct from the 3-way triple (the iter-30 distinction:
    drops the atr_pct gate)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_adx_cmf(30, 0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_cmf" in types
    # triple requires atr_pct>=0.70 (feat has 0.40 here) -> triple inactive
    assert "london_triple_confirm" not in types


def _feat_adx_atr_pct(adx=30, atr_pct=0.85, m5_trend="bullish", symbol="EURUSDm"):
    """Feature dict for ADX+ATR-pct 2-way-compound setups: adx + atr_pct +
    m5_trend set (the 2-way conjunction is the primary key), cmf explicitly
    ZERO so the cmf-gated setups (cmf_continuation, adx_cmf, triple_confirm)
    cannot fire. Built on _feat_atrpct (atr_pct + m5_trend set, adx weak) then
    overrides adx strong + di lines and forces cmf=0.0. Only the ADX+ATR-pct
    compound (and the single-dim adx_trend / atr_pct_breakout, which the tests
    account for) can fire."""
    f = _feat_atrpct(atr_pct, m5_trend, symbol)  # atr_pct + m5_trend set, adx weak
    f["adx"] = adx
    f["di_plus"] = 25
    f["di_minus"] = 15
    f["cmf"] = 0.0  # no money-flow gate -> cmf/triple/adx_cmf inactive
    return f


def test_london_adx_atr_pct_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_adx_atr_pct(30, 0.85, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_atr_pct" in types
    s = next(c for c in out if c["setup_type"] == "london_adx_atr_pct")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "ADX" in s["reason"]
    assert "ATR-pct" in s["reason"]
    assert "2-way confluence" in s["reason"]
    # no money-flow gate -> cmf not mentioned in the reason
    assert "CMF" not in s["reason"]


def test_london_adx_atr_pct_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_adx_atr_pct(28, 0.78, "bearish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_atr_pct" in types
    s = next(c for c in out if c["setup_type"] == "london_adx_atr_pct")
    assert s["side"] == "SELL"


def test_london_adx_atr_pct_skipped_weak_adx(monkeypatch):
    """adx < 25 (one of the 2 dims fails) -> no fire (conjunction)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_adx_atr_pct(20, 0.85, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_atr_pct" not in types


def test_london_adx_atr_pct_skipped_low_atr_pct(monkeypatch):
    """atr_pct < 0.70 (one of the 2 dims fails) -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_adx_atr_pct(30, 0.50, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_atr_pct" not in types


def test_london_adx_atr_pct_skipped_neutral_trend(monkeypatch):
    """both dims strong but m5_trend neutral -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_adx_atr_pct(30, 0.85, "neutral", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_atr_pct" not in types


def test_london_adx_atr_pct_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_adx_atr_pct(30, 0.85, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_atr_pct" not in types


def test_ny_adx_atr_pct_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_adx_atr_pct(28, 0.80, "bearish", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_adx_atr_pct" in types
    s = next(c for c in out if c["setup_type"] == "ny_adx_atr_pct")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_adx_atr_pct_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_adx_atr_pct(30, 0.85, "bullish", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_adx_atr_pct" not in types


def test_adx_atr_pct_distinct_from_triple_and_adx_cmf(monkeypatch):
    """The ADX+ATR-pct 2-way compound does NOT require cmf (unlike the triple
    and adx_cmf). With cmf ZERO (triple/adx_cmf/cmf_continuation inactive) but
    adx strong + atr_pct high + m5_trend directional, the ADX+ATR-pct compound
    still fires — confirms it is a genuine 2-way conjunction distinct from the
    cmf-gated compounds (the iter-31 distinction: drops the cmf gate)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_adx_atr_pct(30, 0.85, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_adx_atr_pct" in types
    # cmf=0 -> cmf-gated compounds inactive
    assert "london_triple_confirm" not in types
    assert "london_adx_cmf" not in types
    assert "london_cmf_continuation" not in types


def _feat_atr_pct_cmf(atr_pct=0.85, cmf=0.25, m5_trend="bullish", symbol="EURUSDm"):
    """Feature dict for ATR-pct+CMF 2-way-compound setups: atr_pct + cmf +
    m5_trend set (the 2-way conjunction is the primary key), adx explicitly
    WEAK so the adx-gated setups (adx_trend, adx_cmf, triple_confirm) cannot
    fire. Built on _feat_cmf (cmf + m5_trend set, atr_pct low 0.40, adx weak)
    then overrides atr_pct high and forces adx weak + di equal. Only the
    ATR-pct+CMF compound (and the single-dim atr_pct_breakout / cmf_continuation,
    which the tests account for) can fire."""
    f = _feat_cmf(cmf, m5_trend, symbol)  # cmf + m5_trend set, atr_pct=0.40, adx weak
    f["atr_pct"] = atr_pct
    f["adx"] = 10  # weak -> adx-gated setups (adx_trend/adx_cmf/triple) inactive
    f["di_plus"] = 15
    f["di_minus"] = 15
    return f


def test_london_atr_pct_cmf_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_atr_pct_cmf(0.85, 0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_atr_pct_cmf" in types
    s = next(c for c in out if c["setup_type"] == "london_atr_pct_cmf")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "ATR-pct" in s["reason"]
    assert "CMF" in s["reason"]
    assert "2-way confluence" in s["reason"]
    # no strength gate -> ADX not mentioned in the reason
    assert "ADX" not in s["reason"]


def test_london_atr_pct_cmf_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_atr_pct_cmf(0.78, -0.22, "bearish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_atr_pct_cmf" in types
    s = next(c for c in out if c["setup_type"] == "london_atr_pct_cmf")
    assert s["side"] == "SELL"
    assert "distribution" in s["reason"]


def test_london_atr_pct_cmf_skipped_low_atr_pct(monkeypatch):
    """atr_pct < 0.70 (one of the 2 dims fails) -> no fire (conjunction)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_atr_pct_cmf(0.50, 0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_atr_pct_cmf" not in types


def test_london_atr_pct_cmf_skipped_weak_cmf(monkeypatch):
    """cmf between -0.10 and +0.10 (one of the 2 dims fails) -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_atr_pct_cmf(0.85, 0.05, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_atr_pct_cmf" not in types


def test_london_atr_pct_cmf_skipped_trend_pressure_mismatch(monkeypatch):
    """cmf positive (accumulation) but m5_trend bearish -> pressure disagrees
    -> no fire (the 2 dims must AGREE)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_atr_pct_cmf(0.85, 0.25, "bearish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_atr_pct_cmf" not in types


def test_london_atr_pct_cmf_skipped_neutral_trend(monkeypatch):
    """both dims strong but m5_trend neutral -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_atr_pct_cmf(0.85, 0.25, "neutral", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_atr_pct_cmf" not in types


def test_london_atr_pct_cmf_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_atr_pct_cmf(0.85, 0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_atr_pct_cmf" not in types


def test_ny_atr_pct_cmf_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_atr_pct_cmf(0.80, -0.30, "bearish", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_atr_pct_cmf" in types
    s = next(c for c in out if c["setup_type"] == "ny_atr_pct_cmf")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_atr_pct_cmf_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_atr_pct_cmf(0.85, 0.25, "bullish", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_atr_pct_cmf" not in types


def test_atr_pct_cmf_distinct_from_adx_bearing_compounds(monkeypatch):
    """The ATR-pct+CMF 2-way compound does NOT require adx (unlike the triple,
    adx_cmf, adx_atr_pct). With adx WEAK (10 < 25, adx-gated compounds inactive)
    but atr_pct high + cmf in direction + m5_trend directional, the ATR-pct+CMF
    compound still fires — confirms it is a genuine 2-way conjunction distinct
    from the adx-bearing compounds (the iter-32 distinction: drops the adx
    gate, completes the attribution matrix)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_atr_pct_cmf(0.85, 0.25, "bullish", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_atr_pct_cmf" in types
    # adx=10 weak -> adx-gated compounds inactive
    assert "london_triple_confirm" not in types
    assert "london_adx_cmf" not in types
    assert "london_adx_atr_pct" not in types
    assert "london_adx_trend" not in types


# --- iteration 34: session-gated VWAP-band mean reversion (24th entry model,
# 5th volume dimension = volume-weighted price anchor; keys off feat.vwap_position). ---

def _feat_vwap(vwap_position=0.95, m5_trend="neutral", symbol="EURUSDm"):
    """Feature dict for VWAP-reversion setups: vwap_position at a band extreme
    (the primary key). Built on _feat_atrpct (which inherits the inert OBV/ADX/
    MFI/CCI/RSI/stoch/mtf/volume baseline) then OVERRIDES so ONLY VWAP reversion
    can fire: m5_trend=neutral (continuation setups atr_pct_breakout /
    cmf_continuation / all compounds require a directional trend), bb_position
    mid (BB reversion needs bb_position extreme), atr_pct low (ATR-pct inactive),
    cmf neutral (CMF inactive), vwap_position at the extreme. The VWAP reversion
    detector ignores m5_trend (it fades a band extreme), so this isolates it."""
    f = _feat_atrpct(0.40, "neutral", symbol)  # atr_pct low, trend neutral
    f["vwap_position"] = vwap_position
    f["vwap"] = 1.10
    f["vwap_upper"] = 1.11
    f["vwap_lower"] = 1.09
    f["bb_position"] = 0.5  # mid -> BB reversion inactive
    f["cmf"] = 0.0          # neutral -> CMF inactive
    f["m5_trend"] = "neutral"
    return f


def test_london_vwap_reversion_fires_above_upper(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_vwap(0.95, "neutral", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_vwap_reversion" in types
    s = next(c for c in out if c["setup_type"] == "london_vwap_reversion")
    assert s["side"] == "SELL"
    assert "07:00-10:00" in s["reason"]
    assert "VWAP upper" in s["reason"]


def test_london_vwap_reversion_fires_below_lower(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_vwap(0.04, "neutral", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_vwap_reversion" in types
    s = next(c for c in out if c["setup_type"] == "london_vwap_reversion")
    assert s["side"] == "BUY"
    assert "VWAP lower" in s["reason"]


def test_london_vwap_reversion_skipped_mid_position(monkeypatch):
    """vwap_position in the middle of the band (not at an extreme) -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_vwap(0.50, "neutral", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_vwap_reversion" not in types


def test_london_vwap_reversion_skipped_below_upper_threshold(monkeypatch):
    """vwap_position = 0.90 (< 0.92 upper threshold) -> not overstretched -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_vwap(0.90, "neutral", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_vwap_reversion" not in types


def test_london_vwap_reversion_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_vwap(0.95, "neutral", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_vwap_reversion" not in types


def test_ny_vwap_reversion_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_vwap(0.97, "neutral", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_vwap_reversion" in types
    s = next(c for c in out if c["setup_type"] == "ny_vwap_reversion")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_vwap_reversion_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_vwap(0.95, "neutral", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_vwap_reversion" not in types


def test_vwap_reversion_distinct_from_bb_reversion(monkeypatch):
    """VWAP reversion keys off vwap_position (volume-weighted band), NOT
    bb_position (price-only Bollinger band). With bb_position mid (0.5, BB
    inactive) but vwap_position at the extreme, only VWAP reversion fires —
    confirms it is a genuinely distinct entry model, not a BB relabel."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_vwap(0.95, "neutral", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_vwap_reversion" in types
    assert "london_bb_reversion" not in types
    assert "ny_bb_reversion" not in types


def test_vwap_reversion_ignores_trend(monkeypatch):
    """VWAP reversion is a mean-reversion model: it fires on the band extreme
    regardless of m5_trend (unlike the continuation compounds which need a
    directional trend). Neutral trend + extreme vwap_position still fires."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_vwap(0.95, "neutral", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_vwap_reversion" in types
    # neutral trend -> continuation setups inactive
    assert "london_atr_pct_breakout" not in types
    assert "london_cmf_continuation" not in types
    assert "london_triple_confirm" not in types


# --- iteration 35: session-gated Supertrend-flip trend continuation (25th entry
# model, new trend-STATE dimension = ATR-band flip; keys off feat.supertrend_flip). ---

def _feat_supertrend(flip="bullish_flip", symbol="EURUSDm"):
    """Feature dict for Supertrend-flip setups: supertrend_flip set (the primary
    key). Built on _feat_atrpct (inert OBV/ADX/MFI/CCI/RSI/stoch/mtf/volume
    baseline) then OVERRIDES so ONLY the Supertrend-flip setup can fire:
    m5_trend neutral (continuation setups that need directional trend stay
    inactive — the Supertrend flip is its OWN trend signal), bb_position mid
    (BB reversion inactive), atr_pct low (ATR-pct inactive), cmf neutral (CMF
    inactive), vwap_position mid (VWAP reversion inactive), supertrend_flip at
    the event. The detector keys ONLY on supertrend_flip, so this isolates it."""
    f = _feat_atrpct(0.40, "neutral", symbol)  # atr_pct low, trend neutral
    f["supertrend_dir"] = "up" if flip == "bullish_flip" else "down"
    f["supertrend_flip"] = flip
    f["bb_position"] = 0.5     # mid -> BB reversion inactive
    f["vwap_position"] = 0.5   # mid -> VWAP reversion inactive
    f["cmf"] = 0.0             # neutral -> CMF inactive
    f["m5_trend"] = "neutral"
    return f


def test_london_supertrend_flip_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_supertrend("bullish_flip", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_supertrend_flip" in types
    s = next(c for c in out if c["setup_type"] == "london_supertrend_flip")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "bullish flip" in s["reason"]


def test_london_supertrend_flip_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_supertrend("bearish_flip", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_supertrend_flip" in types
    s = next(c for c in out if c["setup_type"] == "london_supertrend_flip")
    assert s["side"] == "SELL"
    assert "bearish flip" in s["reason"]


def test_london_supertrend_flip_skipped_no_flip(monkeypatch):
    """supertrend_flip == none (no trend-state change this bar) -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_supertrend("none", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_supertrend_flip" not in types


def test_london_supertrend_flip_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_supertrend("bullish_flip", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_supertrend_flip" not in types


def test_ny_supertrend_flip_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_supertrend("bearish_flip", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_supertrend_flip" in types
    s = next(c for c in out if c["setup_type"] == "ny_supertrend_flip")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_supertrend_flip_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_supertrend("bullish_flip", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_supertrend_flip" not in types


def test_supertrend_flip_distinct_from_other_trend_setups(monkeypatch):
    """Supertrend-flip keys off supertrend_flip (ATR-band trend-STATE change),
    NOT m5_trend (EMA direction) or adx (strength). With m5_trend neutral +
    adx weak, only the Supertrend-flip setup fires on the flip event — confirms
    it is a genuinely distinct entry model, not a relabel of adx_trend /
    trend_continuation."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_supertrend("bullish_flip", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_supertrend_flip" in types
    # neutral trend + weak adx -> other trend/continuation setups inactive
    assert "london_adx_trend" not in types
    assert "london_atr_pct_breakout" not in types
    assert "london_triple_confirm" not in types


# --- iteration 36: session-gated Ichimoku Tenkan/Kijun-cross continuation (26th
# entry model, new EQUILIBRIUM dimension = rolling high-low midpoint cross
# confirmed by cloud position; keys off feat.ichimoku_tk_cross + cloud_position). ---

def _feat_ichimoku(tk_cross="bullish_cross", cloud_position="above", symbol="EURUSDm"):
    """Feature dict for Ichimoku TK-cross setups: ichimoku_tk_cross + cloud
    position set (the primary keys). Built on _feat_atrpct (inert OBV/ADX/MFI/
    CCI/RSI/stoch/mtf/volume baseline) then OVERRIDES so ONLY the Ichimoku setup
    can fire: m5_trend neutral (continuation/adx/compound setups inactive),
    bb_position mid (BB/VWAP reversion inactive), atr_pct low (ATR-pct inactive),
    cmf neutral (CMF inactive), vwap_position mid, supertrend_flip none
    (Supertrend inactive). The detector keys ONLY on the TK cross + cloud, so
    this isolates it."""
    f = _feat_atrpct(0.40, "neutral", symbol)
    f["ichimoku_tk_cross"] = tk_cross
    f["ichimoku_cloud_position"] = cloud_position
    f["bb_position"] = 0.5
    f["vwap_position"] = 0.5
    f["cmf"] = 0.0
    f["supertrend_flip"] = "none"
    f["m5_trend"] = "neutral"
    return f


def test_london_ichimoku_tk_cross_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_ichimoku("bullish_cross", "above", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_ichimoku_tk_cross" in types
    s = next(c for c in out if c["setup_type"] == "london_ichimoku_tk_cross")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "bullish TK cross" in s["reason"]
    assert "above cloud" in s["reason"]


def test_london_ichimoku_tk_cross_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_ichimoku("bearish_cross", "below", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_ichimoku_tk_cross" in types
    s = next(c for c in out if c["setup_type"] == "london_ichimoku_tk_cross")
    assert s["side"] == "SELL"
    assert "bearish TK cross" in s["reason"]
    assert "below cloud" in s["reason"]


def test_london_ichimoku_tk_cross_skipped_inside_cloud(monkeypatch):
    """TK cross + price INSIDE the cloud = weak, no cloud confirmation -> skip."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_ichimoku("bullish_cross", "inside", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_ichimoku_tk_cross" not in types


def test_london_ichimoku_tk_cross_skipped_no_cross(monkeypatch):
    """ichimoku_tk_cross == none (no Tenkan/Kijun cross this bar) -> skip."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_ichimoku("none", "above", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_ichimoku_tk_cross" not in types


def test_london_ichimoku_tk_cross_skipped_cross_cloud_misaligned(monkeypatch):
    """bullish cross + below cloud (cross disagrees with cloud bias) -> skip
    (the cloud confirmation gate enforces alignment)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_ichimoku("bullish_cross", "below", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_ichimoku_tk_cross" not in types


def test_london_ichimoku_tk_cross_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_ichimoku("bullish_cross", "above", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_ichimoku_tk_cross" not in types


def test_ny_ichimoku_tk_cross_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_ichimoku("bearish_cross", "below", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_ichimoku_tk_cross" in types
    s = next(c for c in out if c["setup_type"] == "ny_ichimoku_tk_cross")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_ichimoku_tk_cross_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_ichimoku("bullish_cross", "above", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_ichimoku_tk_cross" not in types


def test_ichimoku_tk_cross_distinct_from_supertrend_and_adx(monkeypatch):
    """Ichimoku TK cross keys off the rolling-midpoint equilibrium cross + cloud
    position, NOT supertrend_flip (ATR-band state) or adx (strength) or m5_trend
    (EMA direction). With supertrend_flip=none, m5_trend neutral, adx weak, ONLY
    the Ichimoku setup fires on the TK cross + cloud event — confirms it is a
    genuinely distinct entry model, not a relabel of supertrend_flip / adx_trend
    / trend_continuation."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_ichimoku("bullish_cross", "above", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_ichimoku_tk_cross" in types
    assert "london_supertrend_flip" not in types
    assert "london_adx_trend" not in types
    assert "london_atr_pct_breakout" not in types
    assert "london_triple_confirm" not in types


# --- iteration 37: session-gated Fair Value Gap imbalance continuation (27th
# entry model, new PRICE-IMBALANCE dimension = non-adjacent 3-bar ICT/SMC gap
# gated by ATR-relative size; keys off feat.fvg). ---

def _feat_fvg(fvg="bullish_fvg", symbol="EURUSDm"):
    """Feature dict for FVG setups: fvg + fvg_size_atr set (the primary keys).
    Built on _feat_atrpct (inert OBV/ADX/MFI/CCI/RSI/stoch/mtf/volume baseline)
    then OVERRIDES so ONLY the FVG setup can fire: m5_trend neutral (continuation
    setups inactive), bb_position mid (BB/VWAP reversion inactive), atr_pct low,
    cmf neutral (CMF inactive), vwap_position mid, supertrend_flip none,
    ichimoku_tk_cross none (Ichimoku inactive). The detector keys ONLY on fvg,
    so this isolates it."""
    f = _feat_atrpct(0.40, "neutral", symbol)
    f["fvg"] = fvg
    f["fvg_size_atr"] = 0.5 if fvg != "none" else 0.0
    f["bb_position"] = 0.5
    f["vwap_position"] = 0.5
    f["cmf"] = 0.0
    f["supertrend_flip"] = "none"
    f["ichimoku_tk_cross"] = "none"
    f["ichimoku_cloud_position"] = "inside"
    f["m5_trend"] = "neutral"
    return f


def test_london_fvg_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_fvg("bullish_fvg", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_fvg" in types
    s = next(c for c in out if c["setup_type"] == "london_fvg")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "bullish Fair Value Gap" in s["reason"]


def test_london_fvg_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_fvg("bearish_fvg", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_fvg" in types
    s = next(c for c in out if c["setup_type"] == "london_fvg")
    assert s["side"] == "SELL"
    assert "bearish Fair Value Gap" in s["reason"]


def test_london_fvg_skipped_no_gap(monkeypatch):
    """fvg == none (no 3-bar imbalance this bar) -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_fvg("none", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_fvg" not in types


def test_london_fvg_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_fvg("bullish_fvg", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_fvg" not in types


def test_ny_fvg_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_fvg("bearish_fvg", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_fvg" in types
    s = next(c for c in out if c["setup_type"] == "ny_fvg")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_fvg_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_fvg("bullish_fvg", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_fvg" not in types


def test_fvg_distinct_from_breakout_and_supertrend(monkeypatch):
    """FVG keys off the 3-bar non-adjacent structural gap, NOT prior-bar S/R
    breakout (feat.breakout) or supertrend_flip (ATR-band state) or ichimoku_tk
    _cross (midpoint cross). With breakout unset, supertrend_flip=none,
    ichimoku_tk_cross=none, m5_trend neutral, ONLY the FVG setup fires on the
    fvg event — confirms it is a genuinely distinct entry model, not a relabel
    of breakout / supertrend_flip / ichimoku_tk_cross / trend_continuation."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_fvg("bullish_fvg", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_fvg" in types
    assert "london_supertrend_flip" not in types
    assert "london_ichimoku_tk_cross" not in types
    assert "london_atr_pct_breakout" not in types
    assert "london_breakout" not in types
    assert "london_volume_breakout" not in types


def _feat_engulfing(engulfing="bullish_engulfing", symbol="EURUSDm"):
    """Feature dict for Engulfing setups: engulfing key set (the primary key).
    Built on _feat_atrpct (inert OBV/ADX/MFI/CCI/RSI/stoch/mtf/volume baseline)
    then OVERRIDES so ONLY the Engulfing setup can fire: m5_trend neutral
    (continuation setups inactive), bb_position mid (BB/VWAP reversion inactive),
    atr_pct low, cmf neutral (CMF inactive), vwap_position mid, supertrend_flip
    none, ichimoku_tk_cross none (Ichimoku inactive), fvg none (FVG inactive).
    The detector keys ONLY on feat.engulfing, so this isolates it."""
    f = _feat_atrpct(0.40, "neutral", symbol)
    f["engulfing"] = engulfing
    f["fvg"] = "none"
    f["fvg_size_atr"] = 0.0
    f["ichimoku_tk_cross"] = "none"
    f["ichimoku_cloud_position"] = "inside"
    f["supertrend_flip"] = "none"
    f["bb_position"] = 0.5
    f["vwap_position"] = 0.5
    f["cmf"] = 0.0
    f["m5_trend"] = "neutral"
    return f


def test_london_engulfing_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_engulfing("bullish_engulfing", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_engulfing" in types
    s = next(c for c in out if c["setup_type"] == "london_engulfing")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "bullish Engulfing" in s["reason"]


def test_london_engulfing_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_engulfing("bearish_engulfing", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_engulfing" in types
    s = next(c for c in out if c["setup_type"] == "london_engulfing")
    assert s["side"] == "SELL"
    assert "bearish Engulfing" in s["reason"]


def test_london_engulfing_skipped_no_pattern(monkeypatch):
    """engulfing == none (no 2-bar body engulf this bar) -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_engulfing("none", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_engulfing" not in types


def test_london_engulfing_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_engulfing("bullish_engulfing", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_engulfing" not in types


def test_ny_engulfing_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_engulfing("bearish_engulfing", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_engulfing" in types
    s = next(c for c in out if c["setup_type"] == "ny_engulfing")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_engulfing_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_engulfing("bullish_engulfing", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_engulfing" not in types


def test_engulfing_distinct_from_fvg_and_rejection(monkeypatch):
    """Engulfing keys off the 2-bar body-vs-body engulf (feat.engulfing), NOT
    the 3-bar non-adjacent FVG gap (feat.fvg) or 1-bar wick rejection
    (feat.rejection). With fvg=none, rejection unset, supertrend_flip=none,
    ichimoku_tk_cross=none, m5_trend neutral, ONLY the Engulfing setup fires on
    the engulfing event — confirms it is a genuinely distinct CANDLE-STRUCTURE
    entry model, not a relabel of fvg / liquidity_sweep / trend_continuation."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_engulfing("bullish_engulfing", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_engulfing" in types
    assert "london_fvg" not in types
    assert "liquidity_sweep" not in types
    assert "london_supertrend_flip" not in types
    assert "london_ichimoku_tk_cross" not in types
    assert "trend_continuation" not in types


def _feat_inside_bar(inside_bar="bullish_inside_breakout", symbol="EURUSDm"):
    """Feature dict for Inside-Bar setups: inside_bar key set (the primary key).
    Built on _feat_atrpct (inert OBV/ADX/MFI/CCI/RSI/stoch/mtf/volume baseline)
    then OVERRIDES so ONLY the Inside-Bar setup can fire: m5_trend neutral
    (continuation setups inactive), bb_position mid (BB/VWAP reversion inactive),
    atr_pct low, cmf neutral (CMF inactive), vwap_position mid, supertrend_flip
    none, ichimoku_tk_cross none (Ichimoku inactive), fvg none (FVG inactive),
    engulfing none (Engulfing inactive). The detector keys ONLY on
    feat.inside_bar, so this isolates it."""
    f = _feat_atrpct(0.40, "neutral", symbol)
    f["inside_bar"] = inside_bar
    f["fvg"] = "none"
    f["fvg_size_atr"] = 0.0
    f["engulfing"] = "none"
    f["ichimoku_tk_cross"] = "none"
    f["ichimoku_cloud_position"] = "inside"
    f["supertrend_flip"] = "none"
    f["bb_position"] = 0.5
    f["vwap_position"] = 0.5
    f["cmf"] = 0.0
    f["m5_trend"] = "neutral"
    return f


def test_london_inside_bar_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_inside_bar("bullish_inside_breakout", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_inside_bar" in types
    s = next(c for c in out if c["setup_type"] == "london_inside_bar")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "bullish Inside-Bar" in s["reason"]


def test_london_inside_bar_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_inside_bar("bearish_inside_breakout", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_inside_bar" in types
    s = next(c for c in out if c["setup_type"] == "london_inside_bar")
    assert s["side"] == "SELL"
    assert "bearish Inside-Bar" in s["reason"]


def test_london_inside_bar_skipped_no_breakout(monkeypatch):
    """inside_bar == none (no 3-bar range-nesting breakout this bar) -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_inside_bar("none", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_inside_bar" not in types


def test_london_inside_bar_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_inside_bar("bullish_inside_breakout", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_inside_bar" not in types


def test_ny_inside_bar_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_inside_bar("bearish_inside_breakout", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_inside_bar" in types
    s = next(c for c in out if c["setup_type"] == "ny_inside_bar")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_inside_bar_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_inside_bar("bullish_inside_breakout", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_inside_bar" not in types


def test_inside_bar_distinct_from_engulfing_and_fvg(monkeypatch):
    """Inside-Bar keys off the 3-bar range-nesting breakout (feat.inside_bar),
    NOT the 2-bar body engulf (feat.engulfing) or 3-bar FVG gap (feat.fvg) or
    1-bar wick rejection. With engulfing=none, fvg=none, rejection unset,
    supertrend_flip=none, ichimoku_tk_cross=none, m5_trend neutral, ONLY the
    Inside-Bar setup fires on the inside-bar event — confirms it is a genuinely
    distinct CANDLE-STRUCTURE entry model (range-nesting, not body engulf /
    gap / wick), not a relabel."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_inside_bar("bullish_inside_breakout", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_inside_bar" in types
    assert "london_engulfing" not in types
    assert "london_fvg" not in types
    assert "liquidity_sweep" not in types
    assert "london_supertrend_flip" not in types
    assert "london_ichimoku_tk_cross" not in types
    assert "trend_continuation" not in types


def _feat_close_streak(streak=3, symbol="EURUSDm"):
    """Feature dict for Close-Streak setups: close_streak key set (the primary
    key). Built on _feat_atrpct (inert OBV/ADX/MFI/CCI/RSI/stoch/mtf/volume
    baseline) then OVERRIDES so ONLY the Close-Streak setup can fire: m5_trend
    neutral (continuation setups inactive), bb_position mid (BB/VWAP reversion
    inactive), atr_pct low, cmf neutral (CMF inactive), vwap_position mid,
    supertrend_flip none, ichimoku_tk_cross none (Ichimoku inactive), fvg none
    (FVG inactive), engulfing none (Engulfing inactive), inside_bar none
    (Inside-Bar inactive). The detector keys ONLY on feat.close_streak
    (>= +min_streak -> BUY / <= -min_streak -> SELL), so this isolates it."""
    f = _feat_atrpct(0.40, "neutral", symbol)
    f["close_streak"] = streak
    f["fvg"] = "none"
    f["fvg_size_atr"] = 0.0
    f["engulfing"] = "none"
    f["inside_bar"] = "none"
    f["ichimoku_tk_cross"] = "none"
    f["ichimoku_cloud_position"] = "inside"
    f["supertrend_flip"] = "none"
    f["bb_position"] = 0.5
    f["vwap_position"] = 0.5
    f["cmf"] = 0.0
    f["m5_trend"] = "neutral"
    return f


def test_london_close_streak_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_close_streak(3, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_close_streak" in types
    s = next(c for c in out if c["setup_type"] == "london_close_streak")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "bullish close-streak" in s["reason"]


def test_london_close_streak_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_close_streak(-4, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_close_streak" in types
    s = next(c for c in out if c["setup_type"] == "london_close_streak")
    assert s["side"] == "SELL"
    assert "bearish close-streak" in s["reason"]


def test_london_close_streak_skipped_below_threshold(monkeypatch):
    """|streak| < min_streak (3) -> no fire (a 2-close run is not a signal)."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_close_streak(2, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_close_streak" not in types


def test_london_close_streak_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_close_streak(3, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_close_streak" not in types


def test_ny_close_streak_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_close_streak(-3, "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_close_streak" in types
    s = next(c for c in out if c["setup_type"] == "ny_close_streak")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_close_streak_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_close_streak(3, "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_close_streak" not in types


def test_close_streak_distinct_from_trend_and_engulfing(monkeypatch):
    """Close-Streak keys off the consecutive-close run-length
    (feat.close_streak), NOT the EMA-trend state (feat.m5_trend) or 2-bar body
    engulf (feat.engulfing) or 3-bar FVG/inside-bar. With m5_trend neutral,
    engulfing=none, fvg=none, inside_bar=none, supertrend_flip=none,
    ichimoku_tk_cross=none, ONLY the Close-Streak setup fires on the streak
    event — confirms it is a genuinely distinct STATISTICAL entry model
    (run-length, not EMA-state / body engulf / gap / range-nesting), not a
    relabel."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_close_streak(3, "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_close_streak" in types
    assert "london_engulfing" not in types
    assert "london_fvg" not in types
    assert "london_inside_bar" not in types
    assert "london_adx_trend" not in types
    assert "london_mtf_align" not in types
    assert "trend_continuation" not in types


def _feat_order_block(ob="bullish_order_block", symbol="EURUSDm"):
    """Feature dict for Order-Block setups: order_block key set (the primary
    key). Built on _feat_atrpct (inert OBV/ADX/MFI/CCI/RSI/stoch/mtf/volume
    baseline) then OVERRIDES so ONLY the Order-Block setup can fire: m5_trend
    neutral (continuation setups inactive), bb_position mid (BB/VWAP reversion
    inactive), atr_pct low, cmf neutral (CMF inactive), vwap_position mid,
    supertrend_flip none, ichimoku_tk_cross none (Ichimoku inactive), fvg none
    (FVG inactive), engulfing none (Engulfing inactive), inside_bar none
    (Inside-Bar inactive), close_streak 0 (Close-Streak inactive). The detector
    keys ONLY on feat.order_block, so this isolates it."""
    f = _feat_atrpct(0.40, "neutral", symbol)
    f["order_block"] = ob
    f["fvg"] = "none"
    f["fvg_size_atr"] = 0.0
    f["engulfing"] = "none"
    f["inside_bar"] = "none"
    f["close_streak"] = 0
    f["ichimoku_tk_cross"] = "none"
    f["ichimoku_cloud_position"] = "inside"
    f["supertrend_flip"] = "none"
    f["bb_position"] = 0.5
    f["vwap_position"] = 0.5
    f["cmf"] = 0.0
    f["m5_trend"] = "neutral"
    return f


def test_london_order_block_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_order_block("bullish_order_block", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_order_block" in types
    s = next(c for c in out if c["setup_type"] == "london_order_block")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "bullish Order Block" in s["reason"]


def test_london_order_block_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_order_block("bearish_order_block", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_order_block" in types
    s = next(c for c in out if c["setup_type"] == "london_order_block")
    assert s["side"] == "SELL"
    assert "bearish Order Block" in s["reason"]


def test_london_order_block_skipped_on_none(monkeypatch):
    """order_block='none' (no displacement) -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_order_block("none", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_order_block" not in types


def test_london_order_block_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_order_block("bullish_order_block", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_order_block" not in types


def test_ny_order_block_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_order_block("bearish_order_block", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_order_block" in types
    s = next(c for c in out if c["setup_type"] == "ny_order_block")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_order_block_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_order_block("bullish_order_block", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_order_block" not in types


def test_order_block_distinct_from_engulfing_and_close_streak(monkeypatch):
    """Order-Block keys off the displacement-origin label (feat.order_block),
    NOT the 2-bar body engulf (feat.engulfing) or consecutive-close run-length
    (feat.close_streak) or 3-bar FVG/inside-bar. With engulfing=none,
    close_streak=0, inside_bar=none, fvg=none, supertrend_flip=none,
    ichimoku_tk_cross=none, ONLY the Order-Block setup fires on the order_block
    event — confirms it is a genuinely distinct CANDLE-STRUCTURE entry model
    (opposite-origin + impulse-magnitude, not body-wrap / run-length / gap /
    range-nesting), not a relabel."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_order_block("bullish_order_block", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_order_block" in types
    assert "london_engulfing" not in types
    assert "london_close_streak" not in types
    assert "london_inside_bar" not in types
    assert "london_fvg" not in types
    assert "london_adx_trend" not in types
    assert "london_mtf_align" not in types
    assert "trend_continuation" not in types


def _feat_ha(ha="bullish_ha_strong", symbol="EURUSDm"):
    """Feature dict for Heikin-Ashi setups: ha_trend key set (the primary key).
    Built on _feat_atrpct (inert OBV/ADX/MFI/CCI/RSI/stoch/mtf/volume baseline)
    then OVERRIDES so ONLY the HA setup can fire: m5_trend neutral (continuation
    setups inactive), bb_position mid (BB/VWAP reversion inactive), atr_pct low,
    cmf neutral (CMF inactive), vwap_position mid, supertrend_flip none,
    ichimoku_tk_cross none (Ichimoku inactive), fvg none (FVG inactive), engulfing
    none (Engulfing inactive), inside_bar none (Inside-Bar inactive), close_streak
    0 (Close-Streak inactive), order_block none (Order-Block inactive). The
    detector keys ONLY on feat.ha_trend, so this isolates it."""
    f = _feat_atrpct(0.40, "neutral", symbol)
    f["ha_trend"] = ha
    f["fvg"] = "none"
    f["fvg_size_atr"] = 0.0
    f["engulfing"] = "none"
    f["inside_bar"] = "none"
    f["close_streak"] = 0
    f["order_block"] = "none"
    f["ichimoku_tk_cross"] = "none"
    f["ichimoku_cloud_position"] = "inside"
    f["supertrend_flip"] = "none"
    f["bb_position"] = 0.5
    f["vwap_position"] = 0.5
    f["cmf"] = 0.0
    f["m5_trend"] = "neutral"
    return f


def test_london_ha_fires_bullish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_ha("bullish_ha_strong", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_ha" in types
    s = next(c for c in out if c["setup_type"] == "london_ha")
    assert s["side"] == "BUY"
    assert "07:00-10:00" in s["reason"]
    assert "bullish Heikin-Ashi" in s["reason"]


def test_london_ha_fires_bearish(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 9)
    out = detect_specialized(_feat_ha("bearish_ha_strong", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_ha" in types
    s = next(c for c in out if c["setup_type"] == "london_ha")
    assert s["side"] == "SELL"
    assert "bearish Heikin-Ashi" in s["reason"]


def test_london_ha_skipped_on_none(monkeypatch):
    """ha_trend='none' (no smoothed strong-trend candle) -> no fire."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_ha("none", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_ha" not in types


def test_london_ha_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 11)
    out = detect_specialized(_feat_ha("bullish_ha_strong", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_ha" not in types


def test_ny_ha_fires_in_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 15)
    out = detect_specialized(_feat_ha("bearish_ha_strong", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_ha" in types
    s = next(c for c in out if c["setup_type"] == "ny_ha")
    assert s["side"] == "SELL"
    assert "14:00-17:00" in s["reason"]


def test_ny_ha_skipped_outside_window(monkeypatch):
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 18)
    out = detect_specialized(_feat_ha("bullish_ha_strong", "US30m"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "ny_ha" not in types


def test_ha_distinct_from_close_streak_and_order_block(monkeypatch):
    """Heikin-Ashi keys off the smoothed-candle wick-absence label
    (feat.ha_trend), NOT the raw-close run-length (feat.close_streak) or raw
    displacement (feat.order_block) or 2-bar body engulf / 3-bar FVG+inside-bar.
    With close_streak=0, order_block=none, engulfing=none, inside_bar=none,
    fvg=none, supertrend_flip=none, ichimoku_tk_cross=none, ONLY the HA setup
    fires on the ha_trend event — confirms it is a genuinely distinct
    PRICE-TRANSFORM entry model (smoothed-candle wick-absence, not raw run-length
    / displacement / body-wrap / gap / range-nesting), not a relabel."""
    monkeypatch.setattr(spec_mod, "utc_hour", lambda: 8)
    out = detect_specialized(_feat_ha("bullish_ha_strong", "EURUSDm"), _ctx(), _ev(0.8), _cfg(True))
    types = [c["setup_type"] for c in out]
    assert "london_ha" in types
    assert "london_close_streak" not in types
    assert "london_order_block" not in types
    assert "london_engulfing" not in types
    assert "london_inside_bar" not in types
    assert "london_fvg" not in types
    assert "london_adx_trend" not in types
    assert "london_mtf_align" not in types
    assert "trend_continuation" not in types
