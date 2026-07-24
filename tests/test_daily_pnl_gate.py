"""Tests for the daily profit halt gate ($400/day).

Geometry under test:
  - core/daily_pnl.py aggregates today's closed-trade pnl correctly
  - _today_utc_midnight() rolls over at UTC midnight
  - today_realized_pnl_usd() is idempotent + skips missing closed_at / non-numeric pnl
  - today_realized_pnl_breakdown() groups by symbol cleanly
  - day_stamp key matches YYYY-MM-DD (matches what execution_loop writes to kill_switch)
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys_path_added = {}
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def tmp_state(tmp_path, monkeypatch):
    """Point core.utils.STATE_DIR at a tmp_path for the duration of one test."""
    import core.utils

    monkeypatch.setattr(core.utils, "STATE_DIR", tmp_path)
    yield tmp_path


def _make_trade(pnl, closed_at, symbol="XAUUSDm", ticket="t-1"):
    """Build a single closed-trade record with the minimum required fields."""
    return {
        "ticket": ticket,
        "symbol": symbol,
        "side": "BUY",
        "pnl": pnl,
        "closed_at": closed_at,
    }


def test_today_realized_pnl_usd_empty_state(tmp_state):
    """No state file present → returns 0.0 (halt NOT triggered)."""
    dpnl = importlib.import_module("core.daily_pnl")
    pnl = dpnl.today_realized_pnl_usd("paper_trades.json")
    assert pnl == 0.0


def test_today_realized_pnl_usd_sums_only_today(tmp_state):
    """Trades outside today's UTC window are excluded."""
    dpnl = importlib.import_module("core.daily_pnl")
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1, hours=2)

    doc = {
        "trades": [
            _make_trade(100.0, today.isoformat()),         # today
            _make_trade(50.0, yesterday.isoformat()),     # yesterday, exclude
            _make_trade(-25.0, today.isoformat(), symbol="EURUSDm"),  # today loss
        ]
    }
    (tmp_state / "paper_trades.json").write_text(json.dumps(doc), encoding="utf-8")
    pnl = dpnl.today_realized_pnl_usd("paper_trades.json")
    assert pnl == 75.0  # 100 - 25; yesterday excluded


def test_today_realized_pnl_usd_skips_missing_closed_at(tmp_state):
    """Open positions (no closed_at) are excluded from the halt gate."""
    dpnl = importlib.import_module("core.daily_pnl")
    doc = {
        "trades": [
            {"ticket": "t-1", "symbol": "XAUUSDm", "pnl": 1000.0},  # no closed_at
            {"ticket": "t-2", "symbol": "EURUSDm", "pnl": 500.0, "closed_at": ""},
            _make_trade(50.0, datetime.now(timezone.utc).isoformat()),
        ]
    }
    (tmp_state / "paper_trades.json").write_text(json.dumps(doc), encoding="utf-8")
    assert dpnl.today_realized_pnl_usd("paper_trades.json") == 50.0


def test_today_realized_pnl_usd_tolerates_non_numeric_pnl(tmp_state):
    """A garbage pnl value must NOT crash the gate (defensive read)."""
    dpnl = importlib.import_module("core.daily_pnl")
    today = datetime.now(timezone.utc).isoformat()
    doc = {
        "trades": [
            _make_trade("not-a-number", today),
            _make_trade(None, today),
            _make_trade(15.5, today),
        ]
    }
    (tmp_state / "paper_trades.json").write_text(json.dumps(doc), encoding="utf-8")
    assert dpnl.today_realized_pnl_usd("paper_trades.json") == 15.5


def test_today_realized_pnl_usd_handles_close_ts_alias(tmp_state):
    """Some trade records use close_ts instead of closed_at (alias-tolerant)."""
    dpnl = importlib.import_module("core.daily_pnl")
    today = datetime.now(timezone.utc).isoformat()
    doc = {
        "trades": [
            {"ticket": "t-1", "symbol": "BTCUSDm", "pnl": 250.0, "close_ts": today},
        ]
    }
    (tmp_state / "paper_trades.json").write_text(json.dumps(doc), encoding="utf-8")
    assert dpnl.today_realized_pnl_usd("paper_trades.json") == 250.0


def test_today_realized_pnl_breakdown_groups_by_symbol(tmp_state):
    dpnl = importlib.import_module("core.daily_pnl")
    today = datetime.now(timezone.utc).isoformat()
    doc = {
        "trades": [
            _make_trade(100.0, today, symbol="XAUUSDm", ticket="t-1"),
            _make_trade(-50.0, today, symbol="XAUUSDm", ticket="t-2"),
            _make_trade(30.0, today, symbol="EURUSDm", ticket="t-3"),
        ]
    }
    (tmp_state / "paper_trades.json").write_text(json.dumps(doc), encoding="utf-8")
    brk = dpnl.today_realized_pnl_breakdown("paper_trades.json")
    assert brk["by_symbol"]["XAUUSDm"] == 50.0
    assert brk["by_symbol"]["EURUSDm"] == 30.0
    assert brk["wins"] == 2
    assert brk["losses"] == 1
    assert brk["closed_count"] == 3
    assert brk["total_usd"] == 80.0


def test_today_utc_day_stamp_matches_dash_format():
    """The day stamp format must match YYYY-MM-DD (UTC)."""
    dpnl = importlib.import_module("core.daily_pnl")
    stamp = dpnl.today_utc_day_stamp()
    parsed = datetime.strptime(stamp, "%Y-%m-%d")
    today = datetime.now(timezone.utc).date()
    assert parsed.date() == today


def test_halt_threshold_synthetic_400():
    """Pure smoke test — if today's PnL >= 400 USD, the helper should return >= 400."""
    dpnl = importlib.import_module("core.daily_pnl")
    today = datetime.now(timezone.utc).isoformat()
    # Synthesize via the public API path (a doc with rollover window):
    # Just confirm the helper can return values >= the threshold; the actual
    # halt-trigger is in execution_loop._check_execution_allowed which is
    # covered by integration tests in run_pipeline.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path
        tmp_path = Path(tmp)
        doc = {
            "trades": [
                _make_trade(250.0, today, symbol="XAUUSDm"),
                _make_trade(200.0, today, symbol="EURUSDm"),
            ]
        }
        (tmp_path / "paper_trades.json").write_text(json.dumps(doc), encoding="utf-8")
        # Re-import under temp STATE_DIR via monkeypatching.
        import core.utils as utils
        original = utils.STATE_DIR
        utils.STATE_DIR = tmp_path
        try:
            pnl = dpnl.today_realized_pnl_usd("paper_trades.json")
        finally:
            utils.STATE_DIR = original
    assert pnl >= 400.0


# ---------------------------------------------------------------------------
# HALT-GATE INTEGRATION tests against execution_loop._check_execution_allowed
# ---------------------------------------------------------------------------


@pytest.fixture()
def halted_today_kwargs(tmp_state):
    """Common fixtures: today's UTC day stamp + $500 of profitable closed trades."""
    dpnl = importlib.import_module("core.daily_pnl")
    today = datetime.now(timezone.utc).isoformat()
    (tmp_state / "paper_trades.json").write_text(
        json.dumps(
            {
                "trades": [
                    _make_trade(500.0, today, symbol="XAUUSDm"),
                ]
            }
        ),
        encoding="utf-8",
    )
    return {
        "dom_threshold_usd": 400.0,
        "today_pnl_usd": 500.0,
        "today": today,
        "pnl_helper": dpnl,
    }


def _make_config_with_halt(halt_usd=400.0):
    return {
        "execution": {"mode": "paper", "live_trading_enabled": True},
        "risk": {"daily_profit_halt_usd": halt_usd},
    }


def test_halt_does_not_overwrite_manual_kill(halted_today_kwargs, tmp_state):
    """A pre-existing MANUAL kill_switch must NOT be clobbered by the halt gate."""
    import logging

    # Pre-set a manual halt.
    (tmp_state / "kill_switch.json").write_text(
        json.dumps({"kill_switch": True, "reason": "manual_hold_for_news"}),
        encoding="utf-8",
    )

    from loops.execution_loop import _check_execution_allowed

    cfg = _make_config_with_halt(400.0)
    logger = logging.getLogger("test_daily_pnl_gate")
    logger.handlers.clear()  # don't pollute test output

    result = _check_execution_allowed(cfg, logger)
    assert result is False, "halt must refuse to execute"

    # The manual reason must be preserved.
    after = json.loads((tmp_state / "kill_switch.json").read_text(encoding="utf-8"))
    assert after.get("reason") == "manual_hold_for_news", (
        "manual halt reason must NOT be overwritten by a daily halt"
    )


def test_halt_writes_once_per_utc_day(halted_today_kwargs, tmp_state):
    """Two cycles on the same UTC day both reach the halt, but only ONE writes
    kill_switch.json (the second is idempotent — reason unchanged)."""
    import logging

    from core.daily_pnl import today_utc_day_stamp
    from loops.execution_loop import _check_execution_allowed

    cfg = _make_config_with_halt(400.0)
    logger = logging.getLogger("test_daily_pnl_gate")
    logger.handlers.clear()

    # First cycle: no existing kill_switch → writes.
    assert _check_execution_allowed(cfg, logger) is False
    first_doc = json.loads(
        (tmp_state / "kill_switch.json").read_text(encoding="utf-8")
    )
    assert first_doc.get("kill_switch") is True
    assert "daily_profit_halt" in first_doc.get("reason", "")
    assert first_doc.get("day_stamp") == today_utc_day_stamp()
    assert first_doc.get("pnl_today_usd") == 500.0
    first_at = first_doc.get("halted_at")

    # Second cycle on same UTC day: kill_switch already True → top-of-function
    # early-return fires BEFORE the daily-gate block runs, so halted_at is
    # preserved naturally. This proves idempotency under repeat cycles.
    assert _check_execution_allowed(cfg, logger) is False
    second_doc = json.loads(
        (tmp_state / "kill_switch.json").read_text(encoding="utf-8")
    )
    assert second_doc.get("halted_at") == first_at, (
        "halted_at must NOT change on the second cycle (idempotent)"
    )


def test_halt_clears_at_utc_rollover(halted_today_kwargs, tmp_state):
    """At UTC midnight rollover, a stale-day halt must be cleared so the bot
    resumes trading on the new day."""
    import logging

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    # Pre-set a halt from yesterday.
    (tmp_state / "kill_switch.json").write_text(
        json.dumps(
            {
                "kill_switch": True,
                "reason": f"daily_profit_halt_usd:400_target_hit_pnl=520.00",
                "day_stamp": yesterday,
                "halted_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                "pnl_today_usd": 520.0,
            }
        ),
        encoding="utf-8",
    )

    from loops.execution_loop import _check_execution_allowed

    cfg = _make_config_with_halt(400.0)
    logger = logging.getLogger("test_daily_pnl_gate")
    logger.handlers.clear()

    # Today's paper_trades: only $100 (no halt on today).
    today = datetime.now(timezone.utc).isoformat()
    (tmp_state / "paper_trades.json").write_text(
        json.dumps({"trades": [_make_trade(100.0, today)]}),
        encoding="utf-8",
    )

    result = _check_execution_allowed(cfg, logger)
    assert result is True, "halt from yesterday must be cleared, trading should resume"

    after = json.loads((tmp_state / "kill_switch.json").read_text(encoding="utf-8"))
    assert after.get("kill_switch") is False, "yesterday's halt must clear"
    assert after.get("cleared_at"), "must include cleared_at timestamp"
    assert after.get("previous_day_stamp") == yesterday, (
        "must record the cleared halt's previous day_stamp for audit"
    )


def test_halt_disabled_when_threshold_is_zero(halted_today_kwargs, tmp_state):
    """risk.daily_profit_halt_usd: 0 (or absent) must NOT trigger the halt."""
    import logging

    today = datetime.now(timezone.utc).isoformat()
    (tmp_state / "paper_trades.json").write_text(
        json.dumps({"trades": [_make_trade(99999.0, today)]}),
        encoding="utf-8",
    )

    from loops.execution_loop import _check_execution_allowed

    cfg = _make_config_with_halt(halt_usd=0)
    logger = logging.getLogger("test_daily_pnl_gate")
    logger.handlers.clear()

    assert _check_execution_allowed(cfg, logger) is True, (
        "halt_usd=0 must NOT trigger regardless of pnl"
    )
