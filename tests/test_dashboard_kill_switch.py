"""Tests for the dashboard's explicit operator kill switch."""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

from dashboard import server
from core.risk_manager import RiskManager


def test_operator_kill_switch_on_and_off_are_explicit(monkeypatch):
    state = {"kill_switch": False}
    writes: list[tuple[str, dict]] = []

    monkeypatch.setattr(server, "read_json_state", lambda _name, default=None: dict(state))

    def fake_write(name, document):
        state.clear()
        state.update(document)
        writes.append((name, document))

    monkeypatch.setattr(server, "write_json_state", fake_write)
    monkeypatch.setattr(server, "utc_now_iso", lambda: "2026-08-04T12:00:00+00:00")

    on = server._set_operator_kill_switch("on", "operator test stop")
    assert on["kill_switch"] is True
    assert on["source"] == "operator"
    assert on["reason"] == "operator test stop"
    assert state["kill_switch"] is True

    off = server._set_operator_kill_switch("off")
    assert off["kill_switch"] is False
    assert off["source"] == "operator"
    assert off["reason"] is None
    assert state["kill_switch"] is False
    assert [name for name, _ in writes] == ["kill_switch.json", "kill_switch.json"]


def test_operator_kill_switch_rejects_unknown_action():
    try:
        server._set_operator_kill_switch("pause")
    except ValueError as exc:
        assert "action must be" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("invalid action should fail")


def test_risk_manager_does_not_clear_operator_stop(monkeypatch):
    cfg = {
        "risk": {
            "kill_switch": False,
            "max_drawdown_pct": 50.0,
            "max_total_exposure_usd": 10000.0,
            "max_symbol_exposure_usd": 10000.0,
            "max_order_age_minutes": 999999,
            "max_consecutive_losses": 0,
        },
        "execution": {"starting_cash": 1000, "mode": "paper"},
        "trading": {},
        "practice": {},
    }
    monkeypatch.setattr(
        "core.risk_manager.read_json_state",
        lambda name, default=None: {"starting_cash": 1000} if name == "mt5_baseline.json" else {},
    )
    monkeypatch.setattr("core.risk_manager.evaluate_daily_growth", lambda _equity, _cfg: {})
    monkeypatch.setattr("core.risk_manager.blue_guardian_enabled", lambda _cfg: False)
    manager = RiskManager(cfg)

    result = manager.evaluate(
        positions=[],
        orders=[],
        balance={"starting_cash": 1000, "equity": 1000, "cash": 1000},
        trades=[],
        features_data={},
        existing_kill_switch={
            "kill_switch": True,
            "reason": "operator test stop",
            "source": "operator",
            "activated_at": "2026-08-04T12:00:00+00:00",
        },
    )

    assert result["kill_switch"]["kill_switch"] is True
    assert result["kill_switch"]["source"] == "operator"
    assert result["kill_switch"]["reason"] == "operator test stop"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


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


def test_kill_switch_http_endpoint_requires_same_origin_and_stop_is_one_way(monkeypatch):
    """Phase 0: STOP is instant and one-way via /api/kill-switch; RESUME only
    via the verified /api/resume flow. Cross-origin and direct 'off' are both
    rejected, and a stopped system stays stopped."""
    state = {"kill_switch": False}
    monkeypatch.setattr(server, "read_json_state", lambda _name, default=None: dict(state))
    monkeypatch.setattr(
        server,
        "write_json_state",
        lambda _name, document: (state.clear(), state.update(document)),
    )
    monkeypatch.setattr(server, "utc_now_iso", lambda: "2026-08-04T12:00:00+00:00")

    port = _free_port()
    httpd = server.ThreadingDashboardServer(("127.0.0.1", port), server.DashboardHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}/api/kill-switch"
    try:
        # Cross-origin STOP must be rejected and leave the system untouched.
        try:
            _post_json(base, {"action": "on"}, origin="https://evil.example")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
            assert state["kill_switch"] is False
        else:  # pragma: no cover - defensive assertion
            raise AssertionError("cross-origin request should be rejected")

        # Same-origin STOP works instantly.
        response = _post_json(
            base,
            {"action": "on", "reason": "test stop"},
            origin=f"http://127.0.0.1:{port}",
        )
        payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert payload["ok"] is True
        assert payload["kill_switch"] is True
        assert state["source"] == "operator"

        # Phase 0: direct 'off' is one-way — it must be rejected and the kill
        # switch must stay ON. Resume only happens through the verified
        # /api/resume endpoint.
        try:
            _post_json(
                base,
                {"action": "off"},
                origin=f"http://127.0.0.1:{port}",
            )
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
            assert "api/resume" in exc.read().decode("utf-8")
            assert state["kill_switch"] is True, \
                "direct off must not clear the operator kill switch"
        else:  # pragma: no cover - defensive assertion
            raise AssertionError("direct 'off' must be rejected in Phase 0")
    finally:
        httpd.shutdown()
        httpd.server_close()
