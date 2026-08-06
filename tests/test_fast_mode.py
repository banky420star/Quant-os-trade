"""Tests for high-frequency scalping mode (observe-only layer)."""

from __future__ import annotations

import pytest

from core.fast_entry_executor import evaluate_entry
from core.fast_mode import fast_mode_enabled, fast_mode_live, fast_mode_symbols
from core.fast_signal_cache import refresh_cache
from core.microstructure import price_in_zone, spread_ok, tick_momentum_score


@pytest.fixture
def fast_config(monkeypatch):
    monkeypatch.setenv("MT5_QUANT_PROFILE", "30-real")
    # Block runtime overrides from disk during tests.
    monkeypatch.setattr(
        "core.fast_mode_runtime.read_json_state",
        lambda name, default=None: (
            {}
            if name == "fast_mode_runtime.json"
            else default
        ),
    )
    from core.utils import load_config
    return load_config()


@pytest.fixture
def fast_live_config(fast_config):
    """30-real ships with execution.fast_mode_enabled=false (hard gate)
    and fast_mode.live_enabled=false (observe-only on the real account).
    Force the live path on so the live-blocking mechanisms stay covered."""
    import copy
    cfg = copy.deepcopy(fast_config)
    # Lift the hard gate so fast_mode_settings proceeds to merging.
    cfg.setdefault("execution", {})["fast_mode_enabled"] = True
    # Enable fast mode and allow runtime live override.
    cfg.setdefault("fast_mode", {})["enabled"] = True
    cfg.setdefault("fast_mode", {})["live_enabled"] = True
    return cfg


@pytest.fixture
def fast_observe_config(fast_config):
    """30-real has fast_mode fully disabled by the hard gate.
    This fixture re-enables observe-only mode for tests that need it."""
    import copy
    cfg = copy.deepcopy(fast_config)
    # Lift the hard gate.
    cfg.setdefault("execution", {})["fast_mode_enabled"] = True
    # Enable fast mode collection but keep live execution locked.
    cfg.setdefault("fast_mode", {})["enabled"] = True
    cfg.setdefault("fast_mode", {})["live_enabled"] = False
    return cfg


def test_fast_mode_disabled_on_30_real(fast_config):
    """30-real hard-disables fast mode via execution.fast_mode_enabled=false."""
    assert fast_mode_enabled(fast_config) is False
    assert fast_mode_live(fast_config) is False
    syms = fast_mode_symbols(fast_config)
    assert syms == []


def test_fast_mode_observe_on_observe_config(fast_observe_config):
    """With hard gate lifted and live_enabled=false, observe mode works."""
    assert fast_mode_enabled(fast_observe_config) is True
    assert fast_mode_live(fast_observe_config) is False
    syms = fast_mode_symbols(fast_observe_config)
    assert "XAUUSDm" in syms


def test_spread_ok_blocks_spike():
    ok, reason = spread_ok(50.0, 10.0, max_mult=1.2)
    assert ok is False
    assert "spread_spike" in reason


def test_price_in_zone():
    assert price_in_zone(100.0, 100.0, 2.0, zone_atr=0.1) is True
    assert price_in_zone(110.0, 100.0, 2.0, zone_atr=0.1) is False


def test_tick_momentum_buy():
    score, _ = tick_momentum_score({"rsi_14": 60, "macd_hist": 0.5, "volume_ratio": 1.2}, "BUY")
    assert score >= 55


def test_refresh_cache_from_evaluated(fast_config, tmp_path, monkeypatch):
    monkeypatch.setattr("core.utils.STATE_DIR", tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)

    evaluated = {
        "evaluated": [{
            "signal_id": "sig-1",
            "symbol": "XAUUSDm",
            "side": "BUY",
            "setup_type": "pullback",
            "entry": 2400.0,
            "evaluation": {"action": "execute", "policy_score": 72},
            "execution_policy": {"action": "execute", "entry_type": "limit"},
            "management_profile": {
                "break_even_trigger_r": 0.4,
                "trail_start_r": 0.65,
                "cancel_if_not_filled_seconds": 60,
            },
        }],
    }
    (tmp_path / "features.json").write_text(
        '{"symbols":{"XAUUSDm":{"atr_14":2.5,"spread_points":25}}}',
        encoding="utf-8",
    )
    doc = refresh_cache(fast_config, evaluated_doc=evaluated)
    assert doc["symbol_count"] == 1
    assert "XAUUSDm" in doc["symbols"]
    assert doc["symbols"]["XAUUSDm"]["anchor"] == 2400.0


def test_evaluate_entry_observe_would_enter(fast_live_config, monkeypatch):
    fast_config = fast_live_config
    monkeypatch.setattr(
        "core.fast_entry_executor.read_json_state",
        lambda name, default=None: (
            {"kill_switch": False}
            if name == "kill_switch.json"
            else {"status": "healthy"}
            if name == "health.json"
            else {"positions": []}
            if name == "paper_positions.json"
            else default
        ),
    )
    cache_entry = {
        "symbol": "XAUUSDm",
        "side": "BUY",
        "signal_id": "sig-1",
        "anchor": 2400.0,
        "entry_type": "limit",
        "trigger_zone_atr": 0.08,
    }
    feat = {"atr_14": 2.5, "spread_points": 20, "rsi_14": 58, "macd_hist": 0.2, "volume_ratio": 1.1}
    dec = evaluate_entry(
        cache_entry,
        mid=2400.1,
        spread_points=22.0,
        feat=feat,
        config=fast_config,
        state={"trades_hour": {}, "consecutive_losses": {}, "last_loss_at": {}},
    )
    assert dec["action"] in (
        "would_enter_limit",
        "would_enter_market",
        "enter_limit",
        "enter_market",
        "wait",
    )
    assert dec["live"] is True


def test_tick_prices_from_feature_price_field(fast_config, monkeypatch):
    from loops import fast_tick_loop

    monkeypatch.setattr(
        fast_tick_loop,
        "read_json_state",
        lambda name, default=None: (
            {"symbols": {"XAUUSDm": {"price": 4166.5, "atr": 7.5, "spread_points": 25}}}
            if name == "features.json"
            else default
        ),
    )
    prices = fast_tick_loop._tick_prices(fast_config, ["XAUUSDm"], fast_tick_loop._logger())
    assert "XAUUSDm" in prices
    assert prices["XAUUSDm"]["mid"] == 4166.5


def test_runtime_preset_overrides_profile(fast_config, tmp_path, monkeypatch):
    from core.fast_mode_runtime import apply_preset, clear_runtime
    from core.fast_mode import fast_mode_live, fast_mode_settings

    monkeypatch.setattr("core.fast_mode_runtime.write_json_state", lambda name, doc: None)
    monkeypatch.setattr("core.fast_mode_runtime.read_json_state", lambda name, default=None: (
        {"preset": "observe", "overrides": {"enabled": True, "live_enabled": False}}
        if name == "fast_mode_runtime.json"
        else default
    ))
    cfg = fast_mode_settings(fast_config)
    assert cfg.get("live_enabled") is False
    assert fast_mode_live(fast_config) is False


def test_runtime_live_preset_cannot_override_profile_lock(
    fast_config, monkeypatch,
):
    """An aggressive/safe/sprint runtime preset must NOT re-enable live
    execution when the profile locks fast_mode.live_enabled=false and
    does not opt into allow_fast_mode_runtime_live."""
    from core.fast_mode import fast_mode_live, fast_mode_settings

    monkeypatch.setattr(
        "core.fast_mode_runtime.read_json_state",
        lambda name, default=None: (
            {
                "preset": "aggressive",
                "overrides": {
                    "enabled": True,
                    "live_enabled": True,
                },
            }
            if name == "fast_mode_runtime.json"
            else default
        ),
    )

    settings = fast_mode_settings(fast_config)
    assert settings.get("live_enabled") is False, (
        "Runtime aggressive preset MUST NOT override profile live_enabled=false"
    )
    assert fast_mode_live(fast_config) is False, (
        "fast_mode_live must stay False when profile locks live_enabled"
    )


def test_runtime_preset_can_enable_when_profile_opts_in(fast_config, monkeypatch):
    """When the profile sets allow_fast_mode_runtime_live=true, a runtime
    preset MAY promote live_enabled back to true."""
    import copy
    from core.fast_mode import fast_mode_live, fast_mode_settings

    cfg = copy.deepcopy(fast_config)
    # Lift the hard gate so fast_mode_settings proceeds past the early return.
    cfg.setdefault("execution", {})["fast_mode_enabled"] = True
    cfg.setdefault("execution", {})["allow_fast_mode_runtime_enable"] = True
    cfg.setdefault("execution", {})["allow_fast_mode_runtime_live"] = True
    # Profile says enabled=false, live_enabled=false but opts into runtime override.
    cfg.setdefault("fast_mode", {})["enabled"] = False
    cfg.setdefault("fast_mode", {})["live_enabled"] = False

    monkeypatch.setattr(
        "core.fast_mode_runtime.read_json_state",
        lambda name, default=None: (
            {
                "preset": "safe",
                "overrides": {
                    "enabled": True,
                    "live_enabled": True,
                },
            }
            if name == "fast_mode_runtime.json"
            else default
        ),
    )

    settings = fast_mode_settings(cfg)
    assert settings.get("live_enabled") is True, (
        "Runtime preset SHOULD promote live_enabled when profile opts in"
    )
    assert fast_mode_live(cfg) is True


def test_hard_gate_bypasses_runtime_when_fast_mode_enabled_false(
    fast_config, monkeypatch,
):
    """execution.fast_mode_enabled=false must return disabled even when
    runtime says aggressive + live."""
    import copy
    from core.fast_mode import fast_mode_live, fast_mode_settings

    cfg = copy.deepcopy(fast_config)
    cfg.setdefault("execution", {})["fast_mode_enabled"] = False

    monkeypatch.setattr(
        "core.fast_mode_runtime.read_json_state",
        lambda name, default=None: (
            {
                "preset": "aggressive",
                "overrides": {
                    "enabled": True,
                    "live_enabled": True,
                },
            }
            if name == "fast_mode_runtime.json"
            else default
        ),
    )

    settings = fast_mode_settings(cfg)
    assert settings["enabled"] is False
    assert settings["live_enabled"] is False
    assert settings["symbols"] == []
    assert fast_mode_live(cfg) is False


def test_apply_preset_observe(tmp_path, monkeypatch):
    from core import fast_mode_runtime

    monkeypatch.setattr("core.utils.STATE_DIR", tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    doc = fast_mode_runtime.apply_preset("observe")
    assert doc["preset"] == "observe"
    assert doc["overrides"]["live_enabled"] is False
    saved = (tmp_path / "fast_mode_runtime.json").read_text(encoding="utf-8")
    assert "observe" in saved


def test_fast_live_blocked_without_verifier_approval(fast_live_config, monkeypatch):
    fast_config = fast_live_config
    from core.fast_live_executor import execute_fast_entry

    monkeypatch.setattr(
        "core.fast_live_executor.read_json_state",
        lambda name, default=None: (
            {"kill_switch": False}
            if name == "kill_switch.json"
            else {"orders": []}
            if name == "paper_orders.json"
            else {"positions": []}
            if name == "paper_positions.json"
            else {"trades": []}
            if name == "paper_trades.json"
            else default
        ),
    )
    monkeypatch.setattr(
        "core.fast_live_executor.read_evaluated_signals",
        lambda _cfg: {
            "evaluated": [{
                "signal_id": "sig-1",
                "symbol": "XAUUSDm",
                "side": "BUY",
                "entry": 2400.0,
                "sl": 2390.0,
                "tp1": 2410.0,
            }],
        },
    )
    monkeypatch.setattr(
        "core.fast_live_executor.read_approved_signals",
        lambda _cfg: {"approved": []},
    )
    result = execute_fast_entry(
        {"action": "enter_limit", "entry_type": "limit", "symbol": "XAUUSDm"},
        {"signal_id": "sig-1", "symbol": "XAUUSDm", "anchor": 2400.0},
        config=fast_config,
        state={"executed_signals": []},
    )
    assert result.get("blocked") is True
    assert result.get("reason") == "not_verifier_approved"


def test_evaluate_entry_blocked_by_kill_switch(fast_config, monkeypatch):
    monkeypatch.setattr(
        "core.fast_entry_executor.read_json_state",
        lambda name, default=None: {"kill_switch": True, "reason": "test"}
        if name == "kill_switch.json"
        else default,
    )
    dec = evaluate_entry(
        {"symbol": "XAUUSDm", "side": "BUY", "anchor": 2400, "trigger_zone_atr": 0.08},
        mid=2400.0,
        spread_points=10,
        feat={"atr_14": 2.5, "spread_points": 10},
        config=fast_config,
    )
    assert dec["action"] == "blocked"

def test_refresh_cache_from_approved_unwraps_verifier_record(fast_observe_config, tmp_path, monkeypatch):
    monkeypatch.setattr("core.utils.STATE_DIR", tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = dict(fast_observe_config)
    cfg["state_store"] = {"enabled": False}
    cfg["fast_mode"] = dict(fast_observe_config.get("fast_mode") or {})
    cfg["fast_mode"]["require_evaluated_signal"] = False

    (tmp_path / "features.json").write_text(
        '{"symbols":{"XAUUSDm":{"atr_14":2.5,"spread_points":25}}}',
        encoding="utf-8",
    )
    approved = {
        "approved": [{
            "signal": {
                "signal_id": "sig-approved",
                "symbol": "XAUUSDm",
                "side": "SELL",
                "setup_type": "pullback",
                "entry": 2400.0,
                "execution_policy": {"action": "execute", "entry_type": "limit"},
            }
        }]
    }
    doc = refresh_cache(cfg, evaluated_doc={"evaluated": []}, approved_doc=approved)
    assert doc["symbol_count"] == 1
    assert doc["symbols"]["XAUUSDm"]["signal_id"] == "sig-approved"


def test_fast_live_blocked_entry_does_not_increment_trade_counter(fast_live_config, monkeypatch):
    fast_config = fast_live_config
    from core.fast_live_executor import execute_fast_entry

    monkeypatch.setattr(
        "core.fast_live_executor.read_json_state",
        lambda name, default=None: (
            {"kill_switch": False}
            if name == "kill_switch.json"
            else {"orders": []}
            if name == "paper_orders.json"
            else {"positions": []}
            if name == "paper_positions.json"
            else {"trades": []}
            if name == "paper_trades.json"
            else default
        ),
    )
    monkeypatch.setattr("core.fast_live_executor.read_approved_signals", lambda _cfg: {"approved": []})
    state = {"executed_signals": []}
    result = execute_fast_entry(
        {"action": "enter_limit", "entry_type": "limit", "symbol": "XAUUSDm"},
        {"signal_id": "sig-1", "symbol": "XAUUSDm", "anchor": 2400.0},
        config=fast_config,
        state=state,
    )
    assert result.get("blocked") is True
    assert state.get("trades_hour", {}) == {}


def test_fast_guard_can_run_trail_only(fast_observe_config):
    from loops.fast_position_guard import _guard_actions

    cfg = dict(fast_observe_config)
    cfg["fast_mode"] = dict(fast_observe_config.get("fast_mode") or {})
    cfg["fast_mode"]["break_even_fast"] = {"enabled": False, "trigger_r": 0.25}
    cfg["fast_mode"]["trail_fast"] = {"enabled": True, "start_r": 0.45, "atr_mult": 0.35}
    cfg["fast_mode"]["emergency_exit"] = {"enabled": False}
    actions = _guard_actions(
        {"symbol": "XAUUSDm", "side": "BUY", "entry": 100.0, "sl": 99.0, "profit": 60.0, "ticket": 1},
        {},
        {"point": 0.01, "tick_value": 1.0, "atr": 2.0, "price": 101.0},
        cfg,
    )
    names = [a["action"] for a in actions]
    assert "would_trail" in names
    assert "would_move_be" not in names


def test_fast_guard_emergency_uses_feature_price(fast_observe_config):
    from loops.fast_position_guard import _guard_actions

    cfg = dict(fast_observe_config)
    cfg["fast_mode"] = dict(fast_observe_config.get("fast_mode") or {})
    cfg["fast_mode"]["break_even_fast"] = {"enabled": False}
    cfg["fast_mode"]["trail_fast"] = {"enabled": False}
    cfg["fast_mode"]["emergency_exit"] = {"enabled": True, "adverse_tick_move_atr": 0.25}
    actions = _guard_actions(
        {"symbol": "XAUUSDm", "side": "BUY", "entry": 100.0, "sl": 99.0, "profit": -10.0, "ticket": 1},
        {},
        {"atr": 2.0, "price": 99.0},
        cfg,
    )
    assert any(a["action"] == "would_emergency_exit" for a in actions)

def test_fast_limit_decision_forces_limit_entry_type(fast_live_config, monkeypatch):
    fast_config = fast_live_config
    monkeypatch.setattr(
        "core.fast_entry_executor.read_json_state",
        lambda name, default=None: (
            {"kill_switch": False}
            if name == "kill_switch.json"
            else {"status": "healthy"}
            if name == "health.json"
            else {"positions": []}
            if name == "paper_positions.json"
            else default
        ),
    )
    cfg = dict(fast_config)
    cfg["fast_mode"] = dict(fast_config.get("fast_mode") or {})
    cfg["fast_mode"]["allow_market_entries"] = False
    cfg["fast_mode"]["allow_limit_entries"] = True
    dec = evaluate_entry(
        {
            "symbol": "XAUUSDm",
            "side": "BUY",
            "signal_id": "sig-limit",
            "anchor": 2400.0,
            "entry_type": "market",
            "trigger_zone_atr": 0.08,
        },
        mid=2400.01,
        spread_points=10.0,
        feat={"atr_14": 2.5, "spread_points": 10, "rsi_14": 60, "macd_hist": 0.2, "volume_ratio": 1.2},
        config=cfg,
        state={"trades_hour": {}, "consecutive_losses": {}, "last_loss_at": {}},
    )
    assert dec["action"] == "enter_limit"
    assert dec["entry_type"] == "limit"


def test_prepare_signal_keeps_enter_limit_pending():
    from core.fast_live_executor import _prepare_signal

    out = _prepare_signal(
        {"signal_id": "sig-limit", "symbol": "XAUUSDm", "entry": 2401.0},
        {"anchor": 2400.0, "management_profile": {}},
        {"action": "enter_limit", "entry_type": "market"},
    )
    assert out["entry_mode"] == "limit"
    assert out["entry"] == 2400.0
    assert out["within_reach"] is True

def test_mt5_pending_limit_record_counts_as_submitted():
    from core.mt5_broker import MT5Broker

    broker = MT5Broker({"execution": {}})
    signal = {
        "signal_id": "sig-limit",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "entry": 2400.0,
        "sl": 2390.0,
        "tp1": 2410.0,
    }
    record = broker._build_order_record(
        signal,
        {
            "success": True,
            "pending": True,
            "order_kind": "buy_limit",
            "ticket": 123,
            "volume": 0.01,
            "requested_price": 2400.0,
            "retcode": 10008,
        },
    )
    assert record["type"] == "buy_limit"
    assert record["status"] == "pending"
    assert record["fill_price"] is None
    assert broker._executed_signal_ids([record]) == {"sig-limit"}



def test_fast_entry_respects_max_trades_per_10min(fast_observe_config, monkeypatch):
    """Sprint preset's 10-minute trade cap must block a third entry in the window."""
    from core.fast_entry_executor import evaluate_entry

    monkeypatch.setattr(
        "core.fast_entry_executor.read_json_state",
        lambda name, default=None: (
            {"kill_switch": False}
            if name == "kill_switch.json"
            else {"status": "healthy"}
            if name == "health.json"
            else {"positions": []}
            if name == "paper_positions.json"
            else default
        ),
    )
    cfg = dict(fast_observe_config)
    cfg["fast_mode"] = dict(fast_observe_config.get("fast_mode") or {})
    cfg["fast_mode"]["max_trades_per_10min"] = 2
    cfg["fast_mode"]["max_trades_per_symbol_per_hour"] = 99
    cfg["fast_mode"]["max_consecutive_losses_per_symbol"] = 99
    cfg["fast_mode"]["cooldown_after_loss_seconds"] = 0

    from core.utils import utc_now_iso
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    state = {
        "trades_hour": {"XAUUSDm": [recent, recent]},
        "trades_10min": {"XAUUSDm": [recent, recent]},
        "consecutive_losses": {},
        "last_loss_at": {},
    }
    dec = evaluate_entry(
        {
            "symbol": "XAUUSDm",
            "side": "BUY",
            "signal_id": "sig-10m",
            "anchor": 2400.0,
            "entry_type": "limit",
            "trigger_zone_atr": 0.08,
        },
        mid=2400.01,
        spread_points=10.0,
        feat={"atr_14": 2.5, "spread_points": 10, "rsi_14": 60, "macd_hist": 0.2, "volume_ratio": 1.2},
        config=cfg,
        state=state,
    )
    assert dec["action"] == "blocked"
    assert any(r.startswith("max_trades_per_10min") for r in dec["reasons"])


def test_record_trade_entry_populates_10min_bucket(monkeypatch):
    """record_trade_entry must keep a rolling 10-minute window alongside the 1h one."""
    from core.fast_signal_cache import record_trade_entry
    import core.fast_signal_cache as fsc

    captured = {}
    monkeypatch.setattr(fsc, "write_json_state", lambda name, doc: captured.update(doc))

    state = {
        "trades_hour": {"XAUUSDm": []},
        "trades_10min": {"XAUUSDm": []},
        "consecutive_losses": {},
        "last_loss_at": {},
    }
    record_trade_entry("XAUUSDm", state)
    assert state["trades_10min"]["XAUUSDm"]
    assert state["trades_hour"]["XAUUSDm"]
    assert len(state["trades_10min"]["XAUUSDm"]) == 1
