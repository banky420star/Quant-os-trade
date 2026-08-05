"""Dashboard profile catalog endpoint tests."""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

from dashboard import server
from core.profile_launcher import profile_summary


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_profile_summary_uses_base_symbols_for_non_micro_profile(monkeypatch):
    monkeypatch.setattr(
        "core.profile_launcher._load_base_config",
        lambda: {"mt5": {"symbols": ["XAUUSDm", "EURUSDm"]}},
    )
    summary = profile_summary("growth")
    assert summary["symbols"] == ["XAUUSDm", "EURUSDm"]
    assert summary["description"]


def test_profiles_endpoint_returns_descriptions(monkeypatch):
    monkeypatch.setattr(server, "list_profiles", lambda: ["alpha", "beta"])
    monkeypatch.setattr(server, "active_profile_name", lambda: "beta")
    monkeypatch.setattr(
        server,
        "profile_summary",
        lambda name: {
            "name": name,
            "label": name.title(),
            "description": f"Description for {name}",
            "symbols": ["XAUUSDm"],
        },
    )

    port = _free_port()
    httpd = server.ThreadingDashboardServer(("127.0.0.1", port), server.DashboardHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/profiles", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert payload["ok"] is True
        assert payload["active"] == "beta"
        assert payload["profiles"][0]["description"] == "Description for alpha"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_switch_profile_rejects_cross_origin_and_traversal(monkeypatch):
    monkeypatch.setattr(server, "list_profiles", lambda: ["alpha"])
    monkeypatch.setattr(server, "write_json_state", lambda *_args: None)
    port = _free_port()
    httpd = server.ThreadingDashboardServer(("127.0.0.1", port), server.DashboardHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/api/switch_profile"
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps({"profile": "alpha"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:  # pragma: no cover - defensive assertion
            raise AssertionError("cross-origin profile switch should be rejected")

        request = urllib.request.Request(
            url,
            data=json.dumps({"profile": "../config"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        else:  # pragma: no cover - defensive assertion
            raise AssertionError("path traversal profile should be rejected")
    finally:
        httpd.shutdown()
        httpd.server_close()
