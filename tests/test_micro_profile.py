"""Tests for $30 micro-account profile."""

from __future__ import annotations

import copy

import os

from core.account_mode import runtime_mode_summary
from core.micro_profile import micro_profile_enabled, sync_micro_profile
from core.profile_launcher import set_active_profile
from core.utils import load_config


def _load_micro_config():
    set_active_profile("30")
    os.environ["MT5_QUANT_PROFILE"] = "30"
    return load_config()


def test_micro_profile_enabled_in_config():
    config = _load_micro_config()
    assert micro_profile_enabled(config)
    assert config["mt5"]["symbols"] == ["XAUUSDm", "USOILm", "UK100m"]
    assert config["trading"]["max_open_per_symbol"] == 1
    assert config["trading"]["allow_pyramiding"] is False
    assert config["trading"]["aggressive_mode"] is True
    assert config["execution"]["max_lot"] == 0.01
    assert config["risk"]["max_symbol_exposure_usd"] == 12.0
    assert config["risk"]["max_total_exposure_usd"] == 18.0
    assert config["signals"]["default_risk_percent"] == 1.0
    assert config["practice"]["growth"]["max_daily_loss_pct"] == 10
    assert config["risk"]["max_loss_per_trade_usd"] == 10
    assert config["trading"]["reentry_cooldown_seconds"] == 30
    assert config["trading"]["entry_confirm_seconds"] == 30
    assert config["trading"]["regime_flip_replace_enabled"] is True
    assert config["risk"]["cap_loss_to_balance"] is True
    assert config["trading"]["trailing"]["per_symbol"]["XAUUSDm"]["trail_points_atr_mult"] == 0.28
    assert config["trading"]["exits"]["runner"]["trail_tighten_mult"] == 0.35


def test_micro_runtime_mode_label():
    config = _load_micro_config()
    mode = runtime_mode_summary(config)
    assert mode["micro_profile_active"] is True
    assert mode["label"] == "micro_growth"
    assert "XAUUSDm" in mode["detail"]


def test_practice_gates_do_not_override_micro_exposure():
    config = _load_micro_config()
    from core.practice_session import sync_practice_gates

    config["practice"]["max_symbol_exposure_usd"] = 80
    config["practice"]["max_total_exposure_usd"] = 80
    sync_practice_gates(config)
    assert config["risk"]["max_symbol_exposure_usd"] == 12.0
    assert config["risk"]["max_total_exposure_usd"] == 18.0


def test_micro_disabled_is_noop():
    config = {"practice": {"micro": {"enabled": False, "symbols": ["XAUUSDm"]}}}
    before = copy.deepcopy(config)
    assert sync_micro_profile(config) == before