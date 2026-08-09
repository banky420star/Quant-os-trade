from __future__ import annotations

from pathlib import Path

import yaml

from scripts import phase1_demo_preflight as preflight

ROOT = Path(__file__).resolve().parent.parent


def _profile() -> dict:
    cfg = yaml.safe_load((ROOT / "profiles" / "phase1-demo.yaml").read_text(encoding="utf-8"))
    cfg["active_profile"] = "phase1-demo"
    return cfg


def _account(mode: str = "demo", login: int = 123456) -> dict:
    return {"login": login, "account_mode": mode, "connected": True}


def _feed() -> dict:
    return {"worker_alive": True, "refresh_failures_consecutive": 0}


def _m1(fresh: bool = True, age: float = 12.0) -> dict:
    return {"decisions": {"XAUUSDm": {"data_fresh": fresh, "data_age_seconds": age, "state": "WAIT"}}}


def _checks(cfg: dict, *, armed: bool = False, confirmation: str = ""):
    return preflight.evaluate_preflight(
        cfg,
        _account(),
        _feed(),
        _m1(),
        expected_account=123456,
        expected_commit="abc123",
        expected_branch="agent/phase1-demo-validation",
        actual_commit="abc123",
        branch="agent/phase1-demo-validation",
        dirty=False,
        armed=armed,
        confirmation=confirmation,
        kill_switch={"kill_switch": False},
    )


def _by_name(checks):
    return {c.name: c for c in checks}


def test_profile_ships_unarmed_and_single_symbol():
    cfg = _profile()
    execution = cfg["execution"]
    assert execution["live_trading_enabled"] is False
    assert execution["mt5_trading_enabled"] is False
    assert execution["explicit_opt_in_danger_zone"] is False
    assert cfg["mt5"]["account_mode"] == "demo"
    assert cfg["mt5"]["symbols"] == ["XAUUSDm"]
    assert cfg["m1_structure"]["enabled"] is True
    assert cfg["fast_mode"]["live_enabled"] is False
    assert cfg["learning"]["mode"] == "observe_only"


def test_unarmed_fresh_demo_passes():
    assert all(c.ok for c in _checks(_profile()))


def test_stale_m1_fails():
    checks = preflight.evaluate_preflight(
        _profile(), _account(), _feed(), _m1(False, 120.0),
        expected_account=123456,
        expected_commit="abc123",
        expected_branch="agent/phase1-demo-validation",
        actual_commit="abc123",
        branch="agent/phase1-demo-validation",
        dirty=False,
        armed=False,
        confirmation="",
        kill_switch={"kill_switch": False},
    )
    assert _by_name(checks)["m1_fresh"].ok is False


def test_real_account_is_hard_rejected():
    checks = preflight.evaluate_preflight(
        _profile(), _account("real"), _feed(), _m1(),
        expected_account=123456,
        expected_commit="abc123",
        expected_branch="agent/phase1-demo-validation",
        actual_commit="abc123",
        branch="agent/phase1-demo-validation",
        dirty=False,
        armed=False,
        confirmation="",
        kill_switch={"kill_switch": False},
    )
    by_name = _by_name(checks)
    assert by_name["demo_account_runtime"].ok is False
    assert by_name["real_account_rejected"].ok is False


def test_kill_switch_or_feed_failure_blocks():
    checks = preflight.evaluate_preflight(
        _profile(), _account(), {"worker_alive": True, "refresh_failures_consecutive": 2}, _m1(),
        expected_account=123456,
        expected_commit="abc123",
        expected_branch="agent/phase1-demo-validation",
        actual_commit="abc123",
        branch="agent/phase1-demo-validation",
        dirty=False,
        armed=False,
        confirmation="",
        kill_switch={"kill_switch": True},
    )
    by_name = _by_name(checks)
    assert by_name["kill_switch_clear"].ok is False
    assert by_name["feed_no_failures"].ok is False


def test_armed_demo_requires_all_authority_flags_and_confirmation():
    cfg = _profile()
    checks = _by_name(_checks(cfg, armed=True, confirmation="DEMO 123456 abc123"))
    assert checks["demo_route_authority"].ok is False

    cfg["execution"]["live_trading_enabled"] = True
    cfg["execution"]["mt5_trading_enabled"] = True
    cfg["execution"]["explicit_opt_in_danger_zone"] = True
    checks = _by_name(_checks(cfg, armed=True, confirmation="WRONG"))
    assert checks["demo_route_authority"].ok is True
    assert checks["operator_confirmation"].ok is False

    checks = _checks(cfg, armed=True, confirmation="DEMO 123456 abc123")
    assert all(c.ok for c in checks)


def test_wrong_account_commit_branch_or_dirty_tree_blocks():
    checks = preflight.evaluate_preflight(
        _profile(), _account(login=999999), _feed(), _m1(),
        expected_account=123456,
        expected_commit="abc123",
        expected_branch="agent/phase1-demo-validation",
        actual_commit="def456",
        branch="wrong-branch",
        dirty=True,
        armed=False,
        confirmation="",
        kill_switch={"kill_switch": False},
    )
    by_name = _by_name(checks)
    assert by_name["expected_account"].ok is False
    assert by_name["commit_match"].ok is False
    assert by_name["expected_branch"].ok is False
    assert by_name["clean_worktree"].ok is False
