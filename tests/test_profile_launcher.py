"""Tests for account profile launcher."""

from __future__ import annotations

import os

from core.profile_launcher import list_profiles, set_active_profile
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
        assert c["mt5"]["symbols"] == ["XAUUSDm", "USOILm", "UK100m"]
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


def test_profile_growth_disables_micro():
    os.environ["MT5_QUANT_PROFILE"] = "growth"
    try:
        set_active_profile("growth")
        c = load_config()
        assert c["practice"]["micro"]["enabled"] is False
        assert len(c["mt5"]["symbols"]) == 14
    finally:
        os.environ.pop("MT5_QUANT_PROFILE", None)