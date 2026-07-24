"""Tests for account profile launcher."""

from __future__ import annotations

import os

from core.profile_launcher import (
    auto_select_profile,
    list_profiles,
    load_profile_overlay,
    resolve_profile_for_account,
    set_active_profile,
)
from core.utils import load_config


def test_profiles_exist():
    names = list_profiles()
    assert "30" in names
    assert "30-c1" in names
    assert "30-c2" in names
    assert "100" in names
    assert "growth" in names


def test_profile_30_loads_micro():
    os.environ["MT5_QUANT_PROFILE"] = "30"
    try:
        set_active_profile("30")
        c = load_config()
        assert c.get("active_profile") == "30"
        assert c["practice"]["micro"]["account_size_usd"] == 30
        assert c["mt5"]["symbols"] == list(
            load_profile_overlay("30")["practice"]["micro"]["symbols"]
        )
        assert c["practice"]["micro"]["positive_evolution_enabled"] is True
        assert c["adaptation"]["auto_evolve_cells"] is True
        assert c["execution"]["starting_cash"] == 30
        assert c["practice"]["growth"]["enabled"] is True
        assert c["filters"]["avoid_news"] is True
    finally:
        os.environ.pop("MT5_QUANT_PROFILE", None)


def test_profile_30_c1_loads_evolution():
    os.environ["MT5_QUANT_PROFILE"] = "30-c1"
    try:
        set_active_profile("30-c1")
        c = load_config()
        assert c.get("active_profile") == "30-c1"
        assert c["practice"]["micro"]["positive_evolution_enabled"] is True
        assert c["execution"]["starting_cash"] == 30
    finally:
        os.environ.pop("MT5_QUANT_PROFILE", None)


def test_profile_30_c2_loads_adaptive_evolution():
    os.environ["MT5_QUANT_PROFILE"] = "30-c2"
    try:
        set_active_profile("30-c2")
        c = load_config()
        assert c.get("active_profile") == "30-c2"
        assert c["quant"]["use_adaptive_weights"] is True
        assert c["quant"]["micro_evolution"]["enabled"] is True
        assert c["execution"]["starting_cash"] == 30
    finally:
        os.environ.pop("MT5_QUANT_PROFILE", None)


def test_resolve_profile_demo_30():
    assert resolve_profile_for_account({"account_mode": "demo", "equity": 30}) == "30"
    assert resolve_profile_for_account({"account_mode": "demo", "balance": 50}) == "30"


def test_resolve_profile_demo_100():
    assert resolve_profile_for_account({"account_mode": "demo", "equity": 100}) == "100"


def test_resolve_profile_demo_growth():
    assert resolve_profile_for_account({"account_mode": "demo", "equity": 500}) == "growth"


def test_resolve_profile_real_micro():
    assert resolve_profile_for_account({"account_mode": "real", "equity": 37.74}) == "30-real"


def test_resolve_profile_real_large():
    assert resolve_profile_for_account({"account_mode": "real", "equity": 500}) == "live"


def test_auto_select_from_account_json(monkeypatch):
    monkeypatch.delenv("MT5_QUANT_PROFILE", raising=False)

    def _fake_probe(**_kwargs):
        return None

    monkeypatch.setattr("core.profile_launcher.probe_logged_in_account", _fake_probe)
    monkeypatch.setattr(
        "core.profile_launcher.read_json_state",
        lambda name, default=None: {
            "login": 435656990,
            "equity": 28.5,
            "account_mode": "demo",
        } if name == "account.json" else (default or {}),
    )
    result = auto_select_profile()
    assert result["profile"] == "30"
    assert result["source"] == "account_json"


def test_auto_select_explicit_cli_overrides_probe(monkeypatch):
    monkeypatch.delenv("MT5_QUANT_PROFILE", raising=False)
    monkeypatch.setattr(
        "core.profile_launcher.probe_logged_in_account",
        lambda **_kwargs: {"login": 1, "equity": 5000, "account_mode": "demo"},
    )
    result = auto_select_profile(explicit="30")
    assert result["profile"] == "30"
    assert result["source"] == "cli"


def test_profile_30_real_loads_micro_live():
    os.environ["MT5_QUANT_PROFILE"] = "30-real"
    try:
        set_active_profile("30-real")
        c = load_config()
        assert c.get("active_profile") == "30-real"
        assert c["practice"]["micro"]["live_mode"] is True
        assert c["practice"]["growth"]["enabled"] is False
        assert c["mt5"]["account_mode"] == "real"
        assert c["trading"]["aggressive_mode"] is True
        assert c["mt5"]["symbols"] == list(
            load_profile_overlay("30-real")["practice"]["micro"]["symbols"]
        )
        assert c["execution"]["live_trading_enabled"] is True
    finally:
        os.environ.pop("MT5_QUANT_PROFILE", None)


def test_profile_growth_disables_micro():
    os.environ["MT5_QUANT_PROFILE"] = "growth"
    try:
        set_active_profile("growth")
        c = load_config()
        assert c["practice"]["micro"]["enabled"] is False
        assert len(c["mt5"]["symbols"]) == 14
    finally:
        os.environ.pop("MT5_QUANT_PROFILE", None)