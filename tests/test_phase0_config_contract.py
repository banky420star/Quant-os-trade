"""Phase 0 config / profile / launcher contract tests.

These assert the SHIPPED contract (runtime behavior is covered by
tests/test_phase0_invariants.py):

  * validation is the default profile and carries zero execution authority;
  * live profiles exist only as explicitly marked opt-in deployments;
  * m1_structure is disabled by default with the freshness gates present;
  * normal launchers default to validation; live launchers are marked
    NOT PART OF PHASE 0.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _profile(name: str) -> dict:
    path = ROOT / "profiles" / f"{name}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestValidationProfile:
    def test_three_authority_flags_are_false(self):
        profile = _profile("validation")
        execution = profile["execution"]
        assert execution["live_trading_enabled"] is False
        assert execution["mt5_trading_enabled"] is False
        assert execution["explicit_opt_in_danger_zone"] is False

    def test_demo_account_only(self):
        assert _profile("validation")["mt5"]["account_mode"] == "demo"

    def test_fast_mode_off_and_no_live(self):
        fast = _profile("validation")["fast_mode"]
        assert fast["enabled"] is False
        assert fast["live_enabled"] is False

    def test_micro_and_growth_disabled(self):
        practice = _profile("validation")["practice"]
        assert practice["micro"]["enabled"] is False
        assert practice["growth"]["enabled"] is False


class TestLiveProfilesAreMarkedOptIn:
    @pytest.mark.parametrize("name", ["30-real", "live"])
    def test_live_profiles_explicitly_opt_in(self, name):
        profile = _profile(name)
        execution = profile["execution"]
        assert execution["live_trading_enabled"] is True
        assert execution["explicit_opt_in_danger_zone"] is True
        assert profile["mt5"]["account_mode"] == "real"


class TestM1DefaultConfiguration:
    def test_m1_structure_disabled_by_default(self):
        config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        assert config["m1_structure"]["enabled"] is False

    def test_freshness_gate_present_and_positive(self):
        config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        assert config["m1_structure"]["max_data_age_seconds"] > 0

    def test_service_cadence_in_acceptable_range(self):
        config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        interval = float(config["m1_structure"]["loop_interval_seconds"])
        assert 1 <= interval <= 300

    def test_candle_refresh_cadence_present(self):
        config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        assert float(config["m1_structure"]["candle_refresh_interval_seconds"]) > 0


class TestLauncherContract:
    def test_default_launchers_use_validation(self):
        for name in ("START_AGENT.bat", "LAUNCH.bat"):
            source = (ROOT / name).read_text(encoding="utf-8-sig")
            assert "--profile validation" in source, name

    def test_live_launchers_marked_not_part_of_phase0(self):
        for name in ("run-live.bat", "run-live-auto.bat", "start30-real.bat"):
            source = (ROOT / name).read_text(encoding="utf-8-sig")
            assert "NOT PART OF PHASE 0" in source, name

    def test_non_live_launchers_do_not_promote_live_profiles(self):
        """Paper/demo launchers must never pass a live profile as --profile."""
        live_profiles = ("30-real", "100", "live")
        for name in ("start.bat", "start30.bat", "start30-c2.bat",
                     "start30-c3.bat", "start_growth.bat"):
            source = (ROOT / name).read_text(encoding="utf-8-sig").lower()
            for live_profile in live_profiles:
                assert f"--profile {live_profile}" not in source, \
                    f"{name} promotes the live profile '{live_profile}'"
