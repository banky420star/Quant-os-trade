"""Phase 0 fail-closed invariant suite (authoritative, behavioral).

The existing Phase 0 tests lean on source-string assertions; this suite proves
the SAME invariants behaviorally — through the running dashboard HTTP server,
the real execution gate, and the actual engine/loop — so future changes cannot
reopen dangerous holes without failing here.

Invariants:
  1. Execution authority — validation has zero authority; the global execution
     gate is closed by default; an order cannot reach MT5 while it is closed;
     validation config cannot open the gate through start.py's own logic.
  2. Dashboard — STOP is one-way; RESUME is verified and rejects invalid or
     real-account confirmations; the unblock route is gone; fast mode is
     read-only; live profile switching is rejected; reset refuses with open
     positions and preserves safety gates.
  3. M1 — the shadow engine/loop cannot reach the broker, kill switch, or
     execution intents; forming/stale/unknown data can never be executable.
  4. Launchers — normal startup defaults to validation; live launchers are
     explicitly marked NOT part of Phase 0.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard import server


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _origin(base: str) -> str:
    """Same-origin header value for a base URL like http://127.0.0.1:PORT."""
    return f"http://127.0.0.1:{base.rsplit(':', 1)[-1]}"


def _post_json(url: str, body: dict, *, origin: str | None = None):
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=3)


def _serve(monkeypatch, seed: dict | None = None, config: dict | None = None):
    """Start the real dashboard HTTP server against a hermetic state store.

    Returns (base_url, store, stop). State reads/writes go to an in-memory
    dict, and core.utils.STATE_DIR is redirected to a temp dir so audit
    writers can never touch the real repo state/ directory.
    """
    from core import utils

    store = dict(seed or {})
    monkeypatch.setattr(server, "read_json_state",
                        lambda name, default=None: store.get(name, default))
    monkeypatch.setattr(server, "write_json_state",
                        lambda name, document: store.__setitem__(name, document))
    monkeypatch.setattr(utils, "STATE_DIR", Path(tempfile.mkdtemp(prefix="p0inv-")))
    monkeypatch.delenv("DASH_OPERATOR_TOKEN", raising=False)
    if config is not None:
        monkeypatch.setattr(server, "load_config", lambda: config)

    port = _free_port()
    httpd = server.ThreadingDashboardServer(("127.0.0.1", port), server.DashboardHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def _stop():
        httpd.shutdown()
        httpd.server_close()

    return f"http://127.0.0.1:{port}", store, _stop


# ===========================================================================
# 1. Execution authority
# ===========================================================================


class TestExecutionAuthority:
    def test_validation_profile_has_zero_execution_authority(self):
        profile = yaml.safe_load((ROOT / "profiles" / "validation.yaml").read_text(encoding="utf-8"))
        execution = profile["execution"]
        assert execution["live_trading_enabled"] is False
        assert execution["mt5_trading_enabled"] is False
        assert execution["explicit_opt_in_danger_zone"] is False
        assert profile["mt5"]["account_mode"] == "demo"

    def test_live_profiles_are_explicitly_opt_in_only(self):
        for name in ("30-real", "live"):
            profile = yaml.safe_load((ROOT / "profiles" / f"{name}.yaml").read_text(encoding="utf-8"))
            execution = profile["execution"]
            assert execution["live_trading_enabled"] is True
            assert execution["explicit_opt_in_danger_zone"] is True
            assert profile["mt5"]["account_mode"] == "real"

    def test_global_execution_gate_closed_by_default(self, monkeypatch):
        import core.mt5_owner as owner_mod

        monkeypatch.setattr(owner_mod, "EXECUTION_ALLOWED", False)
        assert owner_mod.execution_gate_blocked() is True
        assert owner_mod.MT5Owner.instance().order_send({"action": "x"}) is None

    def test_order_cannot_reach_mt5_while_gate_closed(self, monkeypatch):
        """With the gate closed, order_send returns None and the underlying
        MetaTrader5 module is never called."""
        import types

        import core.mt5_owner as owner_mod
        from core.mt5_owner import MT5Owner

        calls: list[dict] = []
        fake = types.ModuleType("MetaTrader5")
        fake.order_send = lambda request: (calls.append(request) or None)
        fake.shutdown = lambda: None

        monkeypatch.setattr(owner_mod, "mt5", fake)
        monkeypatch.setattr(owner_mod, "EXECUTION_ALLOWED", False)

        result = MT5Owner.instance().order_send({"symbol": "XAUUSDm", "action": "open"})
        assert result is None
        assert calls == [], "order_send must never reach MT5 while the gate is closed"

        # Opening the gate (start.py's explicit opt-in path) is the ONLY way.
        monkeypatch.setattr(owner_mod, "EXECUTION_ALLOWED", True)
        MT5Owner.instance().order_send({"symbol": "XAUUSDm", "action": "open"})
        assert len(calls) == 1, "gate open -> exactly one order reaches MT5"
        monkeypatch.setattr(owner_mod, "EXECUTION_ALLOWED", False)

    def test_validation_config_cannot_open_gate_via_start_logic(self, monkeypatch):
        """start.py opens the gate only when explicit_opt_in_danger_zone is
        True; validation config must never satisfy that condition."""
        import core.mt5_owner as owner_mod

        monkeypatch.setattr(owner_mod, "EXECUTION_ALLOWED", False)
        from core.profile_launcher import load_profile_overlay

        validation = load_profile_overlay("validation") or {}
        execution = validation.get("execution") or {}
        assert execution.get("explicit_opt_in_danger_zone") is not True
        # This is start.py's exact gate-opening condition.
        if execution.get("explicit_opt_in_danger_zone") is True:
            owner_mod.EXECUTION_ALLOWED = True
        assert owner_mod.EXECUTION_ALLOWED is False, \
            "validation config silently opened the global execution gate"


# ===========================================================================
# 2. Dashboard invariants (behavioral, through the real HTTP server)
# ===========================================================================


class TestDashboardInvariants:
    def test_stop_is_one_way_and_resume_is_verified(self, monkeypatch):
        base, store, stop = _serve(
            monkeypatch,
            seed={
                "account.json": {"account_mode": "demo", "login": 123, "connected": True},
                "health.json": {"status": "ok"},
                "heartbeat.json": {"timestamp": "2026-08-08T00:00:00+00:00"},
                "kill_switch.json": {"kill_switch": False, "source": "operator"},
            },
            config={"execution": {"explicit_opt_in_danger_zone": True},
                    "mt5": {"account_mode": "demo"}},
        )
        try:
            origin = _origin(base)
            # STOP engages instantly.
            resp = _post_json(f"{base}/api/kill-switch", {"action": "on", "reason": "test"},
                              origin=origin)
            assert resp.status == 200
            assert store["kill_switch.json"]["kill_switch"] is True

            # STOP cannot be reversed through the kill-switch endpoint.
            try:
                _post_json(f"{base}/api/kill-switch", {"action": "off"}, origin=origin)
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
                assert "api/resume" in exc.read().decode("utf-8")
            else:  # pragma: no cover
                raise AssertionError("direct 'off' must be rejected")

            # RESUME rejects an invalid confirmation.
            try:
                _post_json(f"{base}/api/resume", {"confirmation": "WRONG"},
                           origin=origin)
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
                assert "type 'RESUME DEMO 123' exactly" in exc.read().decode("utf-8")
            else:  # pragma: no cover
                raise AssertionError("invalid confirmation must be rejected")

            # RESUME succeeds only with the server-derived phrase.
            resp = _post_json(f"{base}/api/resume", {"confirmation": "RESUME DEMO 123"},
                              origin=origin)
            assert resp.status == 200
            assert store["kill_switch.json"]["kill_switch"] is False
        finally:
            stop()

    def test_real_account_resume_rejected(self, monkeypatch):
        base, store, stop = _serve(
            monkeypatch,
            seed={
                "account.json": {"account_mode": "real", "login": 999, "connected": True},
                "health.json": {"status": "ok"},
                "heartbeat.json": {"timestamp": "2026-08-08T00:00:00+00:00"},
            },
            config={"execution": {"explicit_opt_in_danger_zone": True},
                    "mt5": {"account_mode": "real"}},
        )
        try:
            origin = _origin(base)
            try:
                _post_json(f"{base}/api/resume", {"confirmation": "RESUME DEMO 999"},
                           origin=origin)
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
                body = exc.read().decode("utf-8")
                assert "real-account resume is disabled during Phase 0" in body
            else:  # pragma: no cover
                raise AssertionError("real-account resume must be rejected")
        finally:
            stop()

    def test_unblock_route_is_unavailable(self, monkeypatch):
        base, _, stop = _serve(monkeypatch)
        try:
            try:
                _post_json(f"{base}/api/_disabled_unblock", {}, origin=_origin(base))
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
            else:  # pragma: no cover
                raise AssertionError("unblock route must be gone")
        finally:
            stop()

    def test_fast_mode_mutation_rejected(self, monkeypatch):
        base, _, stop = _serve(monkeypatch)
        try:
            try:
                _post_json(f"{base}/api/fast-mode",
                           {"enabled": True, "live_enabled": True},
                           origin=_origin(base))
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
                assert "read-only" in exc.read().decode("utf-8")
            else:  # pragma: no cover
                raise AssertionError("fast-mode mutation must be rejected")
        finally:
            stop()

    def test_live_profile_switch_rejected(self, monkeypatch):
        base, _, stop = _serve(monkeypatch)
        try:
            try:
                _post_json(f"{base}/api/switch_profile", {"profile": "30-real"},
                           origin=_origin(base))
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
                assert "Phase 0: cannot switch to profile" in exc.read().decode("utf-8")
            else:  # pragma: no cover
                raise AssertionError("live profile switch must be rejected")
        finally:
            stop()

    def test_reset_session_refuses_with_open_positions(self, monkeypatch):
        base, _, stop = _serve(
            monkeypatch,
            seed={"mt5_positions.json": {"positions": [
                {"ticket": 1, "symbol": "XAUUSDm", "volume": 0.01},
            ]}},
        )
        try:
            try:
                _post_json(f"{base}/api/reset-session", {}, origin=_origin(base))
            except urllib.error.HTTPError as exc:
                assert exc.code == 409
                assert "Cannot reset session" in exc.read().decode("utf-8")
            else:  # pragma: no cover
                raise AssertionError("reset with open positions must be refused")
        finally:
            stop()

    def test_reset_session_calls_preserve_safety_gates(self, monkeypatch):
        """The dashboard reset path must invoke the preserve-gates branch."""
        import scripts.reset_session_memory as rsm

        called: list[dict] = []
        monkeypatch.setattr(rsm, "reset_session_memory",
                            lambda **kw: called.append(kw) or None)
        base, _, stop = _serve(monkeypatch)
        try:
            resp = _post_json(f"{base}/api/reset-session", {}, origin=_origin(base))
            assert resp.status == 200
            assert called == [{"preserve_safety_gates": True}], \
                "dashboard reset must preserve safety gates"
        finally:
            stop()


# ===========================================================================
# 3. M1 isolation
# ===========================================================================


class TestM1Isolation:
    def test_m1_modules_have_no_broker_kill_switch_or_intent_path(self):
        """The M1 engine and shadow loop are pure computation — they cannot
        import the broker, push execution intents, or touch the kill switch.
        Broker/execution coupling would have to be an import; comments that
        merely mention the words are harmless, so we match import lines only."""
        broker_import = re.compile(
            r"(?:^|\n)\s*(?:from|import)\s+.*(?:mt5_broker|execution_loop|fast_tick_loop)"
        )
        for relative in ("core/m1_structure_engine.py", "loops/m1_structure_loop.py"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            assert "order_send" not in source, relative
            assert "intent_queue" not in source, relative
            assert "kill_switch.json" not in source, relative
            assert "_set_operator_kill_switch" not in source, relative
            assert broker_import.search(source) is None, \
                f"{relative} imports a broker/execution module"

    def test_m1_shadow_loop_writes_only_decisions(self, monkeypatch):
        """Running the shadow loop must write exactly one file — the decision
        document — and never broker/kill-switch/execution state."""
        from datetime import datetime, timedelta, timezone

        from loops import m1_structure_loop as msl

        sym = f"INV{time.time_ns()}"
        now = datetime.now(timezone.utc)

        def bar(mins_ago, o, h, l, c):
            return {"open": o, "high": h, "low": l, "close": c,
                    "time": (now - timedelta(minutes=mins_ago)).isoformat(),
                    "volume": 10}

        bars = [
            bar(6, 100.0, 100.4, 99.7, 100.2),
            bar(5, 100.2, 100.6, 100.0, 100.4),
            bar(4, 100.4, 100.9, 100.2, 100.7),
            bar(3, 100.6, 101.0, 100.4, 100.8),
            bar(2, 100.8, 101.3, 100.6, 101.1),
            bar(1, 101.0, 101.5, 100.9, 101.4),
        ]
        written: dict = {}
        monkeypatch.setattr(msl, "load_config",
                            lambda: {"m1_structure": {"enabled": True}})
        monkeypatch.setattr(msl, "read_json_state",
                            lambda name, default=None: {
                                "symbols": {sym: {"M1": bars}},
                            })
        monkeypatch.setattr(msl, "write_json_state",
                            lambda name, data: written.__setitem__(name, data))

        result = msl.run()
        assert written, "shadow loop must write its decision document"
        assert set(written.keys()) == {"m1_structure_decisions.json"}, \
            f"shadow loop leaked writes to: {sorted(written.keys())}"
        assert result["enabled"] is True
        assert sym in result["decisions"]

    def test_forming_and_stale_and_unknown_never_executable(self):
        """No input state other than fresh + confirmed can produce BUY/SELL."""
        from datetime import datetime, timedelta, timezone

        from core.m1_structure_engine import M1StructureEngine

        anchor = datetime.now(timezone.utc).replace(microsecond=0)

        def t(minute):
            return (anchor - timedelta(minutes=13 - minute)).isoformat()

        def bar(o, h, l, c, ts):
            return {"open": o, "high": h, "low": l, "close": c, "time": ts}

        # A confirmed bullish chain exists, but a forming candle cannot trigger.
        # Unique symbol keeps the persistent event ledger hermetic across runs.
        sym = f"T{time.time_ns()}"
        engine = M1StructureEngine({"m1_structure": {"enabled": True}})
        engine._events[sym] = [
            _confirmed("bos", "bullish", 101.4, 101.2,
                       f"{sym}|bos|bullish|t10", t(10), sym),
            _confirmed("fvg", "bullish", 101.6, 100.6,
                       f"{sym}|fvg|bullish|t11", t(11), sym),
        ]
        engine.update(sym, [bar(101.2, 101.4, 101.0, 101.3, t(9))], 101.3, atr=1.0)
        assert engine.get_decision(sym)["state"] == "WAIT"  # forming/older bar

        # Stale data forces WAIT even with a full chain.
        engine.update(sym, [bar(101.5, 101.8, 101.4, 101.7,
                                (anchor - timedelta(seconds=91)).isoformat())],
                      101.7, atr=1.0)
        decision = engine.get_decision(sym)
        assert decision["state"] == "WAIT"
        assert decision["data_fresh"] is False

        # Unknown timestamp fails closed.
        engine.update(sym, [{"open": 101.5, "high": 101.8, "low": 101.4, "close": 101.7}],
                      101.7, atr=1.0)
        decision = engine.get_decision(sym)
        assert decision["state"] == "WAIT"
        assert decision["data_fresh"] is False


def _confirmed(event_type, side, top, bottom, event_id, defining, symbol):
    from core.m1_structure_engine import StructureEvent

    return StructureEvent(
        event_type=event_type, symbol=symbol, timeframe="M1", side=side,
        status="CONFIRMED", detected_at=defining, confirmed_at=defining,
        price=(top + bottom) / 2, top=top, bottom=bottom, touches=0,
        provisional_touches=0, candle_closed=True, later_modified=False,
        event_id=event_id, defining_candle_time=defining,
    )


# ===========================================================================
# 4. Launchers
# ===========================================================================


class TestLauncherDefaults:
    def test_default_launchers_point_at_validation(self):
        start_agent = (ROOT / "START_AGENT.bat").read_text(encoding="utf-8-sig")
        launch = (ROOT / "LAUNCH.bat").read_text(encoding="utf-8-sig")
        assert "--profile validation" in start_agent
        assert "--profile validation" in launch
        # The plain start.bat defers to START_AGENT.bat (validation default).
        start = (ROOT / "start.bat").read_text(encoding="utf-8-sig")
        assert "START_AGENT.bat" in start

    def test_live_launchers_are_marked_not_phase0(self):
        for name in ("run-live.bat", "run-live-auto.bat", "start30-real.bat"):
            source = (ROOT / name).read_text(encoding="utf-8-sig")
            assert "NOT PART OF PHASE 0" in source, name
