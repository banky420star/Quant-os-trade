"""Tests for MT5 live-mode account/risk handling.

Verifies:
* risk_loop refreshes account.json from MT5 in mt5 mode before evaluating risk
* missing/stale MT5 account state forces kill switch ON with explicit reason
* zero equity on a real account keeps kill switch ON (never silently hidden)
* account mode mismatch (config=real, snapshot=demo) forces kill switch ON
* paper mode ignores ``account_context`` so existing tests are unaffected
* the connection log uses the configured ``account_mode`` rather than a
  hard-coded "demo" message
* ``MT5ConnectionManager.is_account_fresh`` rejects stale snapshots
"""

from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.risk_manager import RiskManager
from core.utils import STATE_DIR, load_config, write_json_state


@pytest.fixture
def mt5_config():
    cfg = copy.deepcopy(load_config())
    cfg["execution"]["mode"] = "mt5"
    cfg["execution"]["mt5_trading_enabled"] = True
    cfg["mt5"]["account_mode"] = "real"
    return cfg


@pytest.fixture
def paper_config():
    cfg = copy.deepcopy(load_config())
    cfg["execution"]["mode"] = "paper"
    cfg["mt5"]["account_mode"] = "demo"
    return cfg


# ---------------------------------------------------------------------------
# Unit tests against RiskManager.evaluate(...) with account_context
# ---------------------------------------------------------------------------


def test_missing_mt5_account_state_activates_kill_switch(mt5_config):
    manager = RiskManager(mt5_config)
    result = manager.evaluate(
        positions=[],
        orders=[],
        balance={"starting_cash": 1000.0, "equity": 1000.0},
        account_context={
            "expected_account_mode": "real",
            "actual_account_mode": None,
            "status": "failed",
            "account_error": "connection_failed: IPC timeout (-10005)",
        },
    )
    state = result["risk_state"]
    switch = result["kill_switch"]

    assert state["drawdown"] == 100.0
    assert state["equity"] == 0.0
    assert state["account_state"]["status"] == "failed"
    assert state["account_state"]["error"] is not None
    assert switch["kill_switch"] is True
    assert "MT5 account unavailable" in switch["reason"]
    assert any(e["type"] == "mt5_account_error" for e in state["risk_events"])


def test_account_context_with_missing_login_server_activates_kill_switch(mt5_config):
    """When MT5 API returns an empty snapshot we still fail safe."""
    manager = RiskManager(mt5_config)
    result = manager.evaluate(
        positions=[],
        orders=[],
        balance={"starting_cash": 1000.0},
        account_context={
            "expected_account_mode": "real",
            "actual_account_mode": None,
            "status": "missing",
            "account_error": "account snapshot missing login/server",
        },
    )
    assert result["kill_switch"]["kill_switch"] is True
    assert "MT5 account unavailable" in result["kill_switch"]["reason"]


def test_zero_equity_on_real_account_keeps_kill_switch_on(mt5_config):
    manager = RiskManager(mt5_config)
    result = manager.evaluate(
        positions=[],
        orders=[],
        balance={"equity": 0.0, "starting_cash": 1000.0},
        account_context={
            "expected_account_mode": "real",
            "actual_account_mode": "real",
            "status": "ok",
            "account_error": None,
        },
    )
    state = result["risk_state"]
    switch = result["kill_switch"]

    assert state["drawdown"] == 100.0
    assert switch["kill_switch"] is True
    assert "Zero equity" in switch["reason"]
    assert any(e["type"] == "zero_equity_real_account" for e in state["risk_events"])
    assert state["account_state"]["status"] == "ok"


def test_account_mode_mismatch_activates_kill_switch(mt5_config):
    """Config expects real but MT5 reports demo -> trade_safe=False."""
    manager = RiskManager(mt5_config)
    result = manager.evaluate(
        positions=[],
        orders=[],
        balance={"equity": 5000.0, "starting_cash": 5000.0},
        account_context={
            "expected_account_mode": "real",
            "actual_account_mode": "demo",
            "status": "ok",
            "account_error": None,
        },
    )
    assert result["kill_switch"]["kill_switch"] is True
    reason = result["kill_switch"]["reason"]
    assert "demo" in reason
    assert "real" in reason
    assert any(e["type"] == "account_mode_mismatch" for e in result["risk_state"]["risk_events"])


def test_healthy_mt5_account_keeps_kill_switch_off(mt5_config):
    manager = RiskManager(mt5_config)
    result = manager.evaluate(
        positions=[],
        orders=[],
        balance={"equity": 5000.0, "starting_cash": 5000.0},
        account_context={
            "expected_account_mode": "real",
            "actual_account_mode": "real",
            "status": "ok",
            "account_error": None,
        },
    )
    state = result["risk_state"]
    assert state["drawdown"] == 0.0
    assert state["equity"] == 5000.0
    assert state["account_state"]["mode"] == "mt5"
    assert state["account_state"]["status"] == "ok"
    assert result["kill_switch"]["kill_switch"] is False


def test_defensive_unknown_account_mode_activates_kill_switch(mt5_config):
    """Defensive: caller reports status=ok but actual_mode is unknown.

    Should short-circuit to a kill-switch reason and a 100% drawdown so the
    risk loop never silently falls through to the normal-exposure branch.
    """
    manager = RiskManager(mt5_config)
    result = manager.evaluate(
        positions=[],
        orders=[],
        balance={"equity": 5000.0, "starting_cash": 5000.0},
        account_context={
            "expected_account_mode": "real",
            "actual_account_mode": "unknown",
            "status": "ok",
            "account_error": None,
        },
    )
    state = result["risk_state"]
    switch = result["kill_switch"]
    assert switch["kill_switch"] is True
    assert "Unknown MT5 account mode" in switch["reason"]
    assert state["drawdown"] == 100.0
    assert any(e["type"] == "unknown_account_mode" for e in state["risk_events"])


def test_paper_mode_ignores_account_context(paper_config):
    """Paper trading should NOT inherit mt5-mode fail-safe semantics."""
    manager = RiskManager(paper_config)
    result = manager.evaluate(
        positions=[],
        orders=[],
        balance={"equity": 0.0, "starting_cash": 1000.0},
        # Even with an account_error set, paper mode computes normal drawdown.
        account_context={
            "expected_account_mode": "real",
            "actual_account_mode": "real",
            "status": "failed",
            "account_error": "connection_failed",
        },
    )
    assert result["risk_state"]["account_state"]["mode"] == "paper"
    # Drawdown math still runs because account_context is ignored in paper.
    assert result["risk_state"]["drawdown"] == 100.0
    # And exposes the same kill switch via drawdown but with the matching reason.
    assert result["kill_switch"]["kill_switch"] is True
    assert "Drawdown" in result["kill_switch"]["reason"]


# ---------------------------------------------------------------------------
# Integration tests around loops/risk_loop.run() with mocked MT5
# ---------------------------------------------------------------------------


class _FakeConn:
    """Records calls and returns a configurable snapshot."""

    def __init__(self, snapshot: dict | None = None, raise_on_connect: Exception | None = None):
        self.snapshot = snapshot or {}
        self.connect_calls = 0
        self.disconnect_calls = 0
        self._raise = raise_on_connect

    def connect(self):
        self.connect_calls += 1
        if self._raise is not None:
            raise self._raise

    def disconnect(self):
        self.disconnect_calls += 1

    def account_snapshot(self):
        return dict(self.snapshot)


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Redirect state I/O to a temporary directory."""
    monkeypatch.setattr("core.utils.STATE_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def stub_mt5(monkeypatch):
    """Patch MT5ConnectionManager import inside risk_loop so tests can control it.

    Returns a callable ``install(snapshot=..., raise_on_connect=...)`` which
    monkeypatches ``loops.risk_loop.MT5ConnectionManager`` to return a fake
    connection. The fake connection is returned to the caller so tests can
    assert on its connect/disconnect counters.
    """

    def _install(snapshot: dict | None = None, raise_on_connect: Exception | None = None):
        fake = _FakeConn(snapshot=snapshot, raise_on_connect=raise_on_connect)

        def _factory(config, logger=None):
            return fake

        monkeypatch.setattr("loops.risk_loop.MT5ConnectionManager", _factory)
        return fake

    return _install


def test_risk_loop_writes_account_json_when_mt5_succeeds(isolated_state, stub_mt5, mt5_config, monkeypatch):
    fake = stub_mt5(snapshot={
        "login": 1234567,
        "server": "Exness-MT5Real27",
        "balance": 5000.0,
        "equity": 5000.0,
        "currency": "USD",
        "account_mode": "real",
        "trade_allowed": True,
    })

    monkeypatch.setattr("loops.risk_loop.load_config", lambda: mt5_config)

    # Minimal state files needed by the loop:
    write_json_state("paper_positions.json", {"positions": []})
    write_json_state("paper_orders.json", {"orders": [], "balance": {}})
    write_json_state("paper_trades.json", {"trades": []})
    write_json_state("features.json", {"symbols": {}})
    write_json_state("kill_switch.json", {"kill_switch": False})
    write_json_state("mt5_baseline.json", {"starting_cash": 5000.0})

    import loops.risk_loop as risk_loop
    result = risk_loop.run()

    assert fake.connect_calls == 1
    assert fake.disconnect_calls == 1
    saved = (isolated_state / "account.json").read_text(encoding="utf-8")
    assert '"login": 1234567' in saved
    assert '"equity": 5000.0' in saved
    assert result["kill_switch"]["kill_switch"] is False
    assert result["risk_state"]["account_state"]["status"] == "ok"
    assert result["risk_state"]["account_state"]["actual_account_mode"] == "real"
    assert result["risk_state"]["drawdown"] == 0.0


def test_risk_loop_keeps_kill_switch_on_when_mt5_unreachable(isolated_state, stub_mt5, mt5_config, monkeypatch):
    stub_mt5(raise_on_connect=ConnectionError("MT5 IPC timeout (-10005)"))

    monkeypatch.setattr("loops.risk_loop.load_config", lambda: mt5_config)
    write_json_state("paper_positions.json", {"positions": []})
    write_json_state("paper_orders.json", {"orders": [], "balance": {}})
    write_json_state("paper_trades.json", {"trades": []})
    write_json_state("features.json", {"symbols": {}})
    write_json_state("kill_switch.json", {"kill_switch": False})
    write_json_state("mt5_baseline.json", {"starting_cash": 1000.0})

    import loops.risk_loop as risk_loop
    result = risk_loop.run()

    assert result["kill_switch"]["kill_switch"] is True
    assert "MT5 account unavailable" in result["kill_switch"]["reason"]
    assert result["risk_state"]["drawdown"] == 100.0
    # account.json should NOT be rewritten when the connection fails.
    account_path = isolated_state / "account.json"
    assert not account_path.exists()


def test_risk_loop_zero_equity_real_account_keeps_kill_switch_on(isolated_state, stub_mt5, mt5_config, monkeypatch):
    # Snapshot is valid but equity == 0 -> explicit zero-equity reason.
    stub_mt5(snapshot={
        "login": 9999,
        "server": "Exness-MT5Real27",
        "balance": 0.0,
        "equity": 0.0,
        "currency": "USD",
        "account_mode": "real",
        "trade_allowed": True,
    })

    monkeypatch.setattr("loops.risk_loop.load_config", lambda: mt5_config)
    write_json_state("paper_positions.json", {"positions": []})
    write_json_state("paper_orders.json", {"orders": [], "balance": {}})
    write_json_state("paper_trades.json", {"trades": []})
    write_json_state("features.json", {"symbols": {}})
    write_json_state("kill_switch.json", {"kill_switch": False})
    write_json_state("mt5_baseline.json", {"starting_cash": 1000.0})

    import loops.risk_loop as risk_loop
    result = risk_loop.run()

    assert result["kill_switch"]["kill_switch"] is True
    assert "Zero equity" in result["kill_switch"]["reason"]
    assert result["risk_state"]["drawdown"] == 100.0
    assert any(e["type"] == "zero_equity_real_account" for e in result["risk_state"]["risk_events"])


def test_risk_loop_mode_mismatch_blocks_execution(isolated_state, stub_mt5, mt5_config, monkeypatch):
    # Config expects real, attached account is demo.
    stub_mt5(snapshot={
        "login": 8888,
        "server": "Exness-MT5Trial9",
        "balance": 1000.0,
        "equity": 1000.0,
        "currency": "USD",
        "account_mode": "demo",
        "trade_allowed": True,
    })

    monkeypatch.setattr("loops.risk_loop.load_config", lambda: mt5_config)
    write_json_state("paper_positions.json", {"positions": []})
    write_json_state("paper_orders.json", {"orders": [], "balance": {}})
    write_json_state("paper_trades.json", {"trades": []})
    write_json_state("features.json", {"symbols": {}})
    write_json_state("kill_switch.json", {"kill_switch": False})
    write_json_state("mt5_baseline.json", {"starting_cash": 1000.0})

    import loops.risk_loop as risk_loop
    result = risk_loop.run()

    assert result["kill_switch"]["kill_switch"] is True
    assert "demo" in result["kill_switch"]["reason"]
    assert result["risk_state"]["account_state"]["actual_account_mode"] == "demo"


def test_risk_loop_missing_equity_in_snapshot_activates_kill_switch(
    isolated_state, stub_mt5, mt5_config, monkeypatch
):
    """MT5 returns login/server/mode but equity is None -> must fail safe."""
    stub_mt5(snapshot={
        "login": 1234,
        "server": "Exness-MT5Real27",
        "balance": 1000.0,
        # equity intentionally omitted
        "currency": "USD",
        "account_mode": "real",
        "trade_allowed": True,
    })

    monkeypatch.setattr("loops.risk_loop.load_config", lambda: mt5_config)
    write_json_state("paper_positions.json", {"positions": []})
    write_json_state("paper_orders.json", {"orders": [], "balance": {}})
    write_json_state("paper_trades.json", {"trades": []})
    write_json_state("features.json", {"symbols": {}})
    write_json_state("kill_switch.json", {"kill_switch": False})
    write_json_state("mt5_baseline.json", {"starting_cash": 1000.0})

    import loops.risk_loop as risk_loop
    result = risk_loop.run()

    assert result["kill_switch"]["kill_switch"] is True
    assert "MT5 account unavailable" in result["kill_switch"]["reason"]
    assert "equity" in result["kill_switch"]["reason"]


def test_risk_loop_writes_account_state_into_history(
    isolated_state, stub_mt5, mt5_config, monkeypatch
):
    """``record_snapshot`` writes ``equity_history.json``; the risk_loop should
    embed ``account_state`` in the snapshot's ``extra`` dict so the dashboard
    can show the live-mode status without re-reading risk_state.json."""
    stub_mt5(snapshot={
        "login": 5555, "server": "Exness-MT5Real27",
        "balance": 200.0, "equity": 200.0,
        "currency": "USD", "account_mode": "real", "trade_allowed": True,
    })
    monkeypatch.setattr("loops.risk_loop.load_config", lambda: mt5_config)
    write_json_state("paper_positions.json", {"positions": []})
    write_json_state("paper_orders.json", {"orders": [], "balance": {}})
    write_json_state("paper_trades.json", {"trades": []})
    write_json_state("features.json", {"symbols": {}})
    write_json_state("kill_switch.json", {"kill_switch": False})
    write_json_state("mt5_baseline.json", {"starting_cash": 200.0})

    import loops.risk_loop as risk_loop
    risk_loop.run()

    history_path = isolated_state / "equity_history.json"
    assert history_path.exists(), "risk_loop should record an equity snapshot"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    assert history.get("points"), "equity history should include at least one point"
    last = history["points"][-1]
    assert last["equity"] == 200.0
    assert last["source"] == "risk_loop"
    assert last.get("account_state", {}).get("actual_account_mode") == "real"


# ---------------------------------------------------------------------------
# Connection logging + freshness helper
# ---------------------------------------------------------------------------


def test_connection_log_uses_expected_account_mode(caplog):
    """Connect log should mention the configured mode, not a hard-coded demo."""
    from core.mt5_connection_manager import MT5ConnectionManager

    cfg = {"mt5": {"account_mode": "real", "timeout_ms": 60000, "auto_launch_terminal": True}}
    mgr = MT5ConnectionManager(cfg)
    # Patch out anything that touches the terminal so the log line is emitted.
    mgr.terminal_manager.session_alignment = lambda: {
        "python_session_id": 1, "aligned": False, "mt5_processes": []
    }
    mgr.terminal_manager.ensure_terminal = lambda auto_launch=True: {"aligned": False}
    mgr.terminal_manager.discover_paths = lambda: ["C:/fake/terminal64.exe"]

    import core.mt5_connection_manager as mod

    class _Mt5Stub:
        def __init__(self):
            self._ok = False

        def shutdown(self):
            pass

        def initialize(self, **kwargs):
            return False

        def last_error(self):
            return (-1, "fake error")

    mod.mt5 = _Mt5Stub()

    with caplog.at_level("INFO", logger="mt5_connection_manager"):
        with pytest.raises(ConnectionError):
            mgr.connect()

    text = caplog.text
    assert "demo" not in text.lower(), f"connection log should not hard-code 'demo': {text}"
    assert "expected_mode=real" in text


def test_is_account_fresh_rejects_stale_and_empty():
    from core.mt5_connection_manager import MT5ConnectionManager

    fresh_ts = datetime.now(timezone.utc).isoformat()
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()

    assert MT5ConnectionManager.is_account_fresh({"timestamp": fresh_ts}) is True
    assert MT5ConnectionManager.is_account_fresh({"timestamp": stale_ts}) is False
    assert MT5ConnectionManager.is_account_fresh({}) is False
    assert MT5ConnectionManager.is_account_fresh(None) is False

    # Within max_age_seconds tolerance: a 30s-old snapshot at a 120s window is fresh.
    recent = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    assert MT5ConnectionManager.is_account_fresh({"timestamp": recent}, max_age_seconds=120) is True
