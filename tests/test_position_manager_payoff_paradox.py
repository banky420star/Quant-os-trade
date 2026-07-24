"""Payoff Paradox PATCH 2026-07-20: tests for the BE-floor gate.

The patch adds a `min_r_multiple_win` knob to trading.exits and to
practice.micro.exits. compute_managed_sl applies a single guard:

    if lock < min_r_multiple_win * risk_distance AND profit_dist > 0:
        skip the BE move entirely

Patch (b) (only stale-close losers) and patch (c) (partial_tp disabled)
are tested via upstream integration points:
    * manage_partial_tp_mt5 returns early when partial_tp disabled (existing)
    * distance_first_triggers nulls USD triggers for losers (existing)

The previous v1 had trigger_atr_mult=0.85 in the test cfg, which made the
BE trigger require profit_dist >= 0.85*atr in tests where atr=80 -- this
v2 uses USD-only BE so trigger fires deterministically.
"""

from __future__ import annotations

import copy
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.position_manager import compute_managed_sl
from core.exit_manager import partial_tp_enabled


# -------------------------- minimal cfg factory --------------------------

def _cfg(min_r_win: float = 0.4, *,
         trigger_usd: float = 0.5, lock_usd: float = 4.0,
         partial_tp: bool = False, trail_enabled: bool = False,
         be_per_symbol_override: dict | None = None):
    """Build a USD-driven BE cfg.

    distance_first_triggers disabled so explicit trigger_usd and lock_usd
    drive the BE block end-to-end. atr-mult is disabled to keep the model
    predictable.
    """
    per_symbol = {
        "XAUUSDm": {
            "trigger_profit_usd": trigger_usd,
            "lock_profit_usd": lock_usd,
            "trigger_atr_mult": 0.0,
            "lock_profit_atr_mult": 0.0,
        }
    }
    if be_per_symbol_override:
        per_symbol["XAUUSDm"].update(be_per_symbol_override)
    return {
        "trading": {
            "exits": {
                "min_r_multiple_win": min_r_win,
                "partial_tp": {
                    "enabled": partial_tp,
                    "fraction": 0.5,
                    "min_volume_remain": 0.01,
                },
                "runner": {
                    "extend_tp_to_tp2": True,
                    "lock_profit_rr": 0.45,
                    "trail_tighten_mult": 0.7,
                },
                "defer_trail_until": {"require_partial_or_rr": True, "min_rr": 1.0},
            },
            "break_even": {
                "enabled": True,
                "trigger_profit_usd": trigger_usd,
                "lock_profit_usd": lock_usd,
                "trigger_atr_mult": 0.0,
                "lock_profit_atr_mult": 0.0,
                "per_symbol": per_symbol,
            },
            "trailing": {
                "enabled": trail_enabled,
                "activation_profit_usd": None,
                "activation_atr_mult": 100.0,
                "trail_points_atr_mult": 0.9,
                "per_symbol": {
                    "XAUUSDm": {"activation_atr_mult": 100.0, "trail_points_atr_mult": 0.9},
                },
            },
            "max_open_per_symbol": 1,
        },
        "trade_manager": {
            "enabled": True,
            "distance_first_triggers": False,
            "min_usd_trigger": 15,
        },
    }


def _pos(side: str = "BUY", entry: float = 2400.0, sl: float = 2390.0,
         profit: float = 0.0, ticket: int = 1):
    return {
        "ticket": ticket,
        "symbol": "XAUUSDm",
        "side": side,
        "entry": entry,
        "sl": sl,
        "profit": profit,
    }


# -------------------------- (c) partial_tp gate --------------------------

def test_partial_tp_disabled_matches_tier1_config():
    cfg = _cfg(partial_tp=False)
    assert partial_tp_enabled(cfg) is False


# -------------------------- (a) BE-floor gate --------------------------

def test_be_floor_suppresses_dust_lock_on_winner():
    """dust: lock=0.5 < floor=4.0, profit_dist > 0 -> BE suppressed."""
    cfg = _cfg(min_r_win=0.4, trigger_usd=0.5, lock_usd=0.5)
    pos = _pos(entry=2400.0, sl=2390.0, profit=0.5)  # risk=10, floor=4
    new_sl, row, actions = compute_managed_sl(
        cfg, pos, current_price=2402.5, atr=10.0, mgmt_row={}, point=0.01,
    )
    # profit_usd=0.5 >= trigger_usd=0.5 -> BE block armed; lock=0.5
    # 0.5 < 4.0 floor AND profit_dist=2.5 > 0 -> BE suppressed
    assert "break_even" not in actions, f"expected BE suppressed; got {actions}"
    assert not row.get("break_even")


def test_be_floor_allows_when_lock_meets_floor():
    """At-floor lock: lock=4.0 >= floor=4.0 -> BE fires."""
    cfg = _cfg(min_r_win=0.4, trigger_usd=0.5, lock_usd=4.0)
    pos = _pos(entry=2400.0, sl=2390.0, profit=4.0)  # risk=10, floor=4
    new_sl, row, actions = compute_managed_sl(
        cfg, pos, current_price=2404.0, atr=10.0, mgmt_row={}, point=0.01,
    )
    assert "break_even" in actions, f"BE should fire; got {actions}"
    assert row.get("break_even") is True


def test_be_floor_sell_symmetry():
    """SELL winner with lock=floor should also fire BE."""
    cfg = _cfg(min_r_win=0.4, trigger_usd=0.5, lock_usd=4.0)
    pos = _pos(side="SELL", entry=2400.0, sl=2410.0, profit=4.0)
    new_sl, row, actions = compute_managed_sl(
        cfg, pos, current_price=2396.0, atr=10.0, mgmt_row={}, point=0.01,
    )
    assert "break_even" in actions, f"SELL winner with lock=floor should fire BE; got {actions}"


def test_be_floor_higher_threshold_blocks_dust_lock():
    """min_r_win=0.5 -> floor = 5.0 (risk=10). lock=4.0 < 5.0 -> BE suppressed."""
    cfg = _cfg(min_r_win=0.5, trigger_usd=0.5, lock_usd=4.0)
    pos = _pos(entry=2400.0, sl=2390.0, profit=4.0)
    new_sl, row, actions = compute_managed_sl(
        cfg, pos, current_price=2404.0, atr=10.0, mgmt_row={}, point=0.01,
    )
    assert "break_even" not in actions, (
        f"min_r_win=0.5 with lock=4 < floor=5 must suppress BE; got {actions}"
    )


def test_be_floor_zero_min_r_win_disables_gate():
    """min_r_win=0 disables the gate -> BE fires for any lock USD > 0."""
    cfg = _cfg(min_r_win=0.0, trigger_usd=0.5, lock_usd=0.5)
    pos = _pos(entry=2400.0, sl=2390.0, profit=0.5)
    new_sl, row, actions = compute_managed_sl(
        cfg, pos, current_price=2402.5, atr=10.0, mgmt_row={}, point=0.01,
    )
    assert "break_even" in actions, f"min_r_win=0 must disable the floor; got {actions}"


def test_be_floor_threshold_change_only_blocks_underneath():
    """Lock=floor must fire BE; only strict-less-than is suppressed.

    Concretely: with risk=10 and lock=5.0,
      min_r_win=0.4 -> floor=4.0 -> 5.0 < 4.0 False -> BE fires.
      min_r_win=0.7 -> floor=7.0 -> 5.0 < 7.0 True  -> BE suppressed.
    """
    pos = _pos(entry=2400.0, sl=2390.0, profit=5.0)  # risk=10, lock=5

    cfg_low = _cfg(min_r_win=0.4, trigger_usd=0.5, lock_usd=5.0)
    _, _, acts_low = compute_managed_sl(
        cfg_low, pos, current_price=2405.0, atr=10.0, mgmt_row={}, point=0.01,
    )
    assert "break_even" in acts_low, (
        f"min_r_win=0.4 with lock=5 > floor=4 must fire BE; got {acts_low}"
    )

    cfg_high = _cfg(min_r_win=0.7, trigger_usd=0.5, lock_usd=5.0)
    _, _, acts_high = compute_managed_sl(
        cfg_high, pos, current_price=2405.0, atr=10.0, mgmt_row={}, point=0.01,
    )
    assert "break_even" not in acts_high, (
        f"min_r_win=0.7 with lock=5 < floor=7 must suppress BE; got {acts_high}"
    )


# -------------------------- (b) loser never enters BE/trail --------------------------

def test_loser_never_gets_be_action_even_with_dust_trigger():
    """Loser: profit_usd < trigger_usd OR distance <= 0 -> BE never fires."""
    cfg = _cfg(min_r_win=0.4, trigger_usd=4.0, lock_usd=10.0)
    pos = _pos(entry=2400.0, sl=2390.0, profit=-1.0)
    _, _, actions = compute_managed_sl(
        cfg, pos, current_price=2395.0, atr=10.0, mgmt_row={}, point=0.01,
    )
    assert "break_even" not in actions


def test_loser_never_gets_trail_action():
    """Loser at trail-disabled cfg still no trail."""
    cfg = _cfg(min_r_win=0.4, trigger_usd=0.5, lock_usd=4.0, trail_enabled=False)
    pos = _pos(entry=2400.0, sl=2390.0, profit=-1.0)
    _, _, actions = compute_managed_sl(
        cfg, pos, current_price=2395.0, atr=10.0, mgmt_row={}, point=0.01,
    )
    assert "trail" not in actions


# -------------------------- mgmt_row init_sl fallback --------------------------

def test_floor_picks_initial_sl_from_mgmt_row():
    """When mgmt_row['initial_sl'] exists, the floor uses that as risk-distance."""
    cfg = _cfg(min_r_win=0.4, trigger_usd=0.5, lock_usd=4.0)
    pos = _pos(entry=2400.0, sl=2398.0, profit=4.0)  # current SL tightened to 2398
    mgmt_row = {"initial_sl": 2390.0}  # actual initial was 2390
    _, _, actions = compute_managed_sl(
        cfg, pos, current_price=2404.0, atr=10.0, mgmt_row=mgmt_row, point=0.01,
    )
    # risk_sl is row["initial_sl"] (2390) -> risk=10, floor=4. lock=4 -> BE fires.
    assert "break_even" in actions


def test_floor_audit_log_when_initial_sl_missing_and_current_sl_zero():
    """If both initial_sl and current_sl are missing/zero, gate is bypassed but
    we mark an audit entry so the dashboard can flag it."""
    cfg = _cfg(min_r_win=0.4, trigger_usd=0.5, lock_usd=4.0)
    pos = _pos(entry=2400.0, sl=0.0, profit=4.0)
    new_sl, row, actions = compute_managed_sl(
        cfg, pos, current_price=2404.0, atr=10.0, mgmt_row={}, point=0.01,
    )
    audit = row.get("_audit", [])
    assert any(a.get("kind") == "payoff_paradox_unpriced" for a in audit), (
        f"expected payoff_paradox_unpriced audit entry; got {audit}"
    )


# -------------------------- config wiring --------------------------

def test_default_config_loads_with_min_r_win_in_trading_exits():
    """The shipped config.yaml must expose trading.exits.min_r_multiple_win."""
    from core.utils import load_config
    cfg = load_config()
    val = (cfg.get("trading") or {}).get("exits", {}).get("min_r_multiple_win", None)
    assert val == 0.4


def test_default_config_loads_with_min_r_win_in_micro_exits():
    """Mirror knob under practice.micro.exits."""
    # We bypass config.local.yaml by re-reading the YAML manually.
    import yaml
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    micro_val = (
        (cfg.get("practice") or {}).get("micro") or {}
    ).get("exits", {}).get("min_r_multiple_win", None)
    assert micro_val == 0.4


# -------------------------- patch shock-resistance --------------------------

def test_legacy_min_r_win_zero_does_not_change_baseline():
    """Sanity: with min_r_win=0, the BE block fires for any positive trigger."""

    cfg = _cfg(min_r_win=0.0, trigger_usd=0.01, lock_usd=0.01)
    pos = _pos(entry=2400.0, sl=2390.0, profit=0.01)
    _, _, actions = compute_managed_sl(
        cfg, pos, current_price=2400.01, atr=10.0, mgmt_row={}, point=0.01,
    )
    assert "break_even" in actions


if __name__ == "__main__":
    # Manual smoke for quick debugging.
    import json
    r = {}
    for label, fn in [
        ("dust",      test_be_floor_suppresses_dust_lock_on_winner),
        ("boundary",  test_be_floor_allows_when_lock_meets_floor),
        ("sell",      test_be_floor_sell_symmetry),
        ("th=0.5",    test_be_floor_higher_threshold_blocks_dust_lock),
        ("zero_disab",test_be_floor_zero_min_r_win_disables_gate),
        ("loser_be",  test_loser_never_gets_be_action_even_with_dust_trigger),
        ("mgmt_row",  test_floor_picks_initial_sl_from_mgmt_row),
        ("audit",     test_floor_audit_log_when_initial_sl_missing_and_current_sl_zero),
    ]:
        try:
            fn()
            r[label] = "ok"
        except AssertionError as e:
            r[label] = f"FAIL: {e}"
    print(json.dumps(r, indent=2))
