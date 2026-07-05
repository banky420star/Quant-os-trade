"""Tests for entry confirmation and re-entry cooldown."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.entry_staging import clear_symbol, entry_confirm_seconds, touch_and_check
from core.risk_cap import effective_risk_cap, risk_per_trade_cap
from core.trade_limits import symbol_reentry_available
from core.utils import load_config, write_json_state


def test_micro_risk_cap_is_ten_dollars():
    config = load_config()
    assert risk_per_trade_cap(config) == 10.0


def test_effective_risk_cap_never_exceeds_balance():
    config = load_config()
    assert effective_risk_cap(config, 7.5) == 7.5
    assert effective_risk_cap(config, 30.0) == 10.0


def test_reentry_cooldown_blocks_immediate_reentry():
    config = load_config()
    now = datetime.now(timezone.utc)
    closed = [{"symbol": "XAUUSDm", "closed_at": now.isoformat()}]
    ok, remaining = symbol_reentry_available(config, "XAUUSDm", closed)
    assert ok is False
    assert remaining > 0


def test_reentry_allowed_after_cooldown():
    config = load_config()
    old = datetime.now(timezone.utc) - timedelta(seconds=45)
    closed = [{"symbol": "XAUUSDm", "closed_at": old.isoformat()}]
    ok, _ = symbol_reentry_available(config, "XAUUSDm", closed)
    assert ok is True


def test_entry_confirm_requires_hold_time():
    config = load_config()
    write_json_state("entry_staging.json", {"entries": {}})
    signal = {"symbol": "USOILm", "side": "BUY", "setup_type": "pullback", "signal_id": "a"}
    first = touch_and_check(signal, config)
    assert first["ready"] is False
    second = touch_and_check({**signal, "signal_id": "b"}, config)
    assert second["ready"] is False
    assert entry_confirm_seconds(config) == 30.0


def test_clear_symbol_removes_staging():
    write_json_state(
        "entry_staging.json",
        {"entries": {"XAUUSDm|BUY|pullback": {"first_seen_at": "2026-07-03T00:00:00+00:00"}}},
    )
    clear_symbol("XAUUSDm")
    from core.utils import read_json_state
    state = read_json_state("entry_staging.json", default={})
    assert state.get("entries") == {}