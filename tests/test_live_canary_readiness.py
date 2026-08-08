from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "live_canary_preflight", ROOT / "scripts" / "live_canary_preflight.py"
)
assert SPEC and SPEC.loader
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def _profile() -> dict:
    cfg = yaml.safe_load((ROOT / "profiles" / "live-canary.yaml").read_text(encoding="utf-8"))
    cfg["active_profile"] = "live-canary"
    return cfg


def _account() -> dict:
    return {"login": 123456, "account_mode": "real", "connected": True}


def _feed() -> dict:
    return {"worker_alive": True}


def _m1() -> dict:
    return {
        "decisions": {
            "XAUUSDm": {
                "data_fresh": True,
                "data_age_seconds": 12.0,
                "state": "WAIT",
            }
        }
    }


def _checks(cfg: dict, *, armed: bool = False, confirmation: str = ""):
    return preflight.evaluate_preflight(
        cfg,
        _account(),
        _feed(),
        _m1(),
        expected_account=123456,
        expected_commit="abc123",
        actual_commit="abc123",
        branch="agent/live-canary-readiness",
        dirty=False,
        armed=armed,
        confirmation=confirmation,
    )


def _by_name(checks):
    return {c.name: c for c in checks}


def test_live_canary_profile_ships_unarmed():
    cfg = _profile()
    execution = cfg["execution"]
    assert execution["live_trading_enabled"] is False
    assert execution["mt5_trading_enabled"] is False
    assert execution["explicit_opt_in_danger_zone"] is False
    assert cfg["fast_mode"]["live_enabled"] is False
    assert cfg["learning"]["mode"] == "observe_only"
    assert cfg["mt5"]["symbols"] == ["XAUUSDm"]
    assert cfg["trading"]["max_open_per_symbol"] == 1
    assert cfg["trading"]["allow_pyramiding"] is False


def test_unarmed_preflight_passes_only_with_authority_closed():
    checks = _by_name(_checks(_profile()))
    assert checks["unarmed_by_default"].ok is True
    assert all(c.ok for c in checks.values())


def test_armed_preflight_rejects_default_profile():
    checks = _by_name(_checks(_profile(), armed=True, confirmation="LIVE 123456 abc123"))
    assert checks["live_authority"].ok is False


def test_armed_preflight_requires_all_three_authority_flags():
    cfg = _profile()
    cfg["execution"]["live_trading_enabled"] = True
    cfg["execution"]["mt5_trading_enabled"] = True
    cfg["execution"]["explicit_opt_in_danger_zone"] = False
    checks = _by_name(_checks(cfg, armed=True, confirmation="LIVE 123456 abc123"))
    assert checks["live_authority"].ok is False


def test_armed_preflight_requires_exact_operator_confirmation():
    cfg = _profile()
    cfg["execution"]["live_trading_enabled"] = True
    cfg["execution"]["mt5_trading_enabled"] = True
    cfg["execution"]["explicit_opt_in_danger_zone"] = True
    checks = _by_name(_checks(cfg, armed=True, confirmation="LIVE 123456 WRONG"))
    assert checks["live_authority"].ok is True
    assert checks["operator_confirmation"].ok is False


def test_armed_preflight_can_pass_only_when_every_gate_matches():
    cfg = _profile()
    cfg["execution"]["live_trading_enabled"] = True
    cfg["execution"]["mt5_trading_enabled"] = True
    cfg["execution"]["explicit_opt_in_danger_zone"] = True
    checks = _checks(cfg, armed=True, confirmation="LIVE 123456 abc123")
    assert all(c.ok for c in checks)


def test_stale_m1_blocks_readiness():
    cfg = _profile()
    stale = _m1()
    stale["decisions"]["XAUUSDm"]["data_fresh"] = False
    stale["decisions"]["XAUUSDm"]["data_age_seconds"] = 1000.0
    checks = preflight.evaluate_preflight(
        cfg,
        _account(),
        _feed(),
        stale,
        expected_account=123456,
        expected_commit="abc123",
        actual_commit="abc123",
        branch="agent/live-canary-readiness",
        dirty=False,
        armed=False,
        confirmation="",
    )
    assert _by_name(checks)["m1_fresh"].ok is False


def test_wrong_real_account_blocks_readiness():
    checks = preflight.evaluate_preflight(
        _profile(),
        {"login": 999999, "account_mode": "real"},
        _feed(),
        _m1(),
        expected_account=123456,
        expected_commit="abc123",
        actual_commit="abc123",
        branch="agent/live-canary-readiness",
        dirty=False,
        armed=False,
        confirmation="",
    )
    assert _by_name(checks)["expected_account"].ok is False


def test_dirty_tree_or_wrong_commit_blocks_readiness():
    checks = preflight.evaluate_preflight(
        _profile(),
        _account(),
        _feed(),
        _m1(),
        expected_account=123456,
        expected_commit="abc123",
        actual_commit="def456",
        branch="agent/live-canary-readiness",
        dirty=True,
        armed=False,
        confirmation="",
    )
    by_name = _by_name(checks)
    assert by_name["commit_match"].ok is False
    assert by_name["clean_worktree"].ok is False
