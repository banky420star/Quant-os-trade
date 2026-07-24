"""Gold (XAU) never-rejectable policy — shipped entry points."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.gold_policy import gold_force_pass, is_gold_symbol
from core.evaluation_policy import evaluate_candidate
from core.verifier import Verifier


def test_is_gold_symbol():
    assert is_gold_symbol("XAUUSDm")
    assert is_gold_symbol("XAUUSD")
    assert is_gold_symbol("xauusdm")
    assert not is_gold_symbol("EURUSDm")
    assert not is_gold_symbol("USOILm")


def test_gold_force_pass_config():
    assert gold_force_pass("XAUUSDm", {"signals": {"gold_never_rejectable": True}})
    assert not gold_force_pass("XAUUSDm", {"signals": {"gold_never_rejectable": False}})
    assert not gold_force_pass("EURUSDm", {"signals": {"gold_never_rejectable": True}})


def test_evaluation_gold_bypasses_quality_not_blocklist(growth_profile):
    from core.utils import load_config

    config = load_config()
    config.setdefault("signals", {})["gold_never_rejectable"] = True
    config.setdefault("evaluation", {})
    config["evaluation"]["skip_below_score"] = 99
    config["evaluation"]["min_policy_score"] = 99
    config["evaluation"]["symbol_blocklist"] = []
    config["evaluation"]["setup_blocklist"] = []
    feat = {"price": 2400.0, "atr": 4.0, "volume_ratio": 0.1, "m5_trend": "bullish"}
    sig = {
        "signal_id": "g1",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "setup_type": "pullback",
        "confidence": 30,
        "entry": 2400.0,
        "sl": 2390.0,
        "tp1": 2420.0,
        "within_reach": False,
        "distance_atr": 2.0,
        "entry_quality": 10,
        "market_context": {"session": "tokyo", "regime": "ranging"},
    }
    out = evaluate_candidate(sig, feat, config)
    assert out["evaluation"]["action"] == "execute"
    assert "gold_never_rejectable" in out["evaluation"]["reason"]

    # Structural blocklist still applies to gold
    config["evaluation"]["symbol_blocklist"] = ["XAUUSDm"]
    out2 = evaluate_candidate(sig, feat, config)
    assert out2["evaluation"]["action"] == "skip"
    assert "symbol_blocklist" in out2["evaluation"]["reason"]


def test_verifier_force_approves_gold():
    config = {
        "signals": {
            "min_confidence": 90,
            "min_risk_reward": 5.0,
            "gold_never_rejectable": True,
        },
        "filters": {
            "spread_mult": 0.01,
            "min_atr_ratio": 0.5,
            "max_spread_points": {"XAUUSDm": 1},
            "min_volume_ratio": 1.0,
        },
        "risk": {"max_symbol_exposure_usd": 1000, "max_total_exposure_usd": 1000},
        "execution": {"starting_cash": 100},
        "trading": {},
        "news": {},
    }
    v = Verifier(config)
    signal = {
        "signal_id": "gx1",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "setup_type": "pullback",
        "confidence": 20,
        "entry": 2400.0,
        "sl": 2390.0,
        "tp1": 2405.0,  # low RR would fail normally
        "risk_reward": 0.5,
        "reason": "test",
        "market_context": {"session": "tokyo", "market_regime": {"primary": "ranging", "bias": "bearish"}},
    }
    feat = {
        "price": 2400.0,
        "atr": 5.0,
        "m5_trend": "bearish",
        "m15_trend": "bullish",  # TF misalignment
        "volume_ratio": 0.1,
        "spread": 50,
    }
    approved, rejected = v.verify_batch(
        [signal],
        {"symbols": {"XAUUSDm": feat}},
        active_signals=[],
        kill_switch=False,
        spread_data={"XAUUSDm": 500.0},
        equity=100.0,
        balance=100.0,
    )
    assert len(approved) == 1
    assert approved[0]["approved"] is True
    assert approved[0].get("gold_forced_approve") is True
    assert len(rejected) == 0


def test_verifier_gold_still_blocked_by_exposure():
    config = {
        "signals": {
            "min_confidence": 90,
            "min_risk_reward": 5.0,
            "gold_never_rejectable": True,
        },
        "filters": {
            "spread_mult": 0.01,
            "min_atr_ratio": 0.5,
            "max_spread_points": {"XAUUSDm": 1},
            "min_volume_ratio": 1.0,
        },
        # Tiny exposure so check_exposure_limits fails
        "risk": {
            "max_symbol_exposure_usd": 0.01,
            "max_total_exposure_usd": 0.01,
            "unlimited_trades": False,
        },
        "execution": {"starting_cash": 100},
        "trading": {},
        "news": {},
    }
    v = Verifier(config)
    signal = {
        "signal_id": "gx_exp",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "setup_type": "pullback",
        "confidence": 20,
        "entry": 2400.0,
        "sl": 2390.0,
        "tp1": 2405.0,
        "risk_reward": 0.5,
        "size": 1.0,
        "volume": 1.0,
        "market_context": {},
    }
    feat = {
        "price": 2400.0,
        "atr": 5.0,
        "m5_trend": "bearish",
        "m15_trend": "bullish",
        "volume_ratio": 0.1,
        "atr_ratio": 0.1,
    }
    approved, rejected = v.verify_batch(
        [signal],
        {"symbols": {"XAUUSDm": feat}},
        active_signals=[{
            "symbol": "XAUUSDm", "side": "BUY", "size": 10.0, "entry": 2400.0,
            "sl": 2390.0, "tp1": 2410.0, "confidence": 90,
        }],
        kill_switch=False,
        spread_data={"XAUUSDm": 500.0},
        equity=100.0,
        balance=100.0,
    )
    # Hard safety: gold must not force-approve over exposure
    if approved:
        assert approved[0]["approved"] is False or not approved[0].get("gold_forced_approve")
    assert len(rejected) >= 1 or (approved and not approved[0]["approved"])


def test_verifier_still_rejects_gold_on_kill_switch():
    config = {
        "signals": {"min_confidence": 50, "min_risk_reward": 1.0, "gold_never_rejectable": True},
        "filters": {
            "spread_mult": 3.0,
            "min_atr_ratio": 0.1,
            "max_spread_points": {"XAUUSDm": 9999},
            "min_volume_ratio": 0.1,
        },
        "risk": {"max_symbol_exposure_usd": 1000, "max_total_exposure_usd": 1000},
        "execution": {"starting_cash": 100},
        "trading": {},
        "news": {},
    }
    v = Verifier(config)
    signal = {
        "signal_id": "gx2",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "setup_type": "pullback",
        "confidence": 80,
        "entry": 2400.0,
        "sl": 2390.0,
        "tp1": 2420.0,
        "risk_reward": 2.0,
        "market_context": {},
    }
    feat = {"price": 2400.0, "atr": 5.0, "m5_trend": "bullish", "m15_trend": "bullish", "volume_ratio": 1.0}
    approved, rejected = v.verify_batch(
        [signal],
        {"symbols": {"XAUUSDm": feat}},
        kill_switch=True,
        equity=100.0,
        balance=100.0,
    )
    # kill_switch is hard safety — gold still cannot trade when account is halted
    assert len(rejected) == 1 or (approved and not approved[0]["approved"])
