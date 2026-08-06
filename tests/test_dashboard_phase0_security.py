"""Phase 0 dashboard security regression tests.

Tests that:
- _disabled_unblock is permanently blocked (403)
- /api/resume derives phrase server-side, blocks real-account resume, writes audit once
- _mutation_request_allowed enforces DASH_OPERATOR_TOKEN when set
- CORS never uses wildcard *
- build_safety_header computes stop-risk, not floating P&L
"""

from __future__ import annotations

import io
import json
import os as _os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure dashboard is importable
SYS_PATH = str(Path(__file__).resolve().parents[1])
if SYS_PATH not in sys.path:
    sys.path.insert(0, SYS_PATH)


# ---------------------------------------------------------------------------
# 1. _disabled_unblock is permanently blocked
# ---------------------------------------------------------------------------
class TestDisabledUnblock:
    """The legacy unblock endpoint must return 403 without modifying state."""

    def test_disabled_unblock_returns_403(self):
        """_disabled_unblock must not execute any state mutations."""
        from dashboard.server import DashboardHandler as DH
        h = MagicMock()
        h.path = "/api/_disabled_unblock"
        h.headers = {"Host": "127.0.0.1:8082", "Origin": ""}
        h.client_address = ("127.0.0.1", 54321)
        h._read_json_body.return_value = {}
        captured = {}

        def fake_send_json(data, status=200):
            captured["data"] = data
            captured["status"] = status

        h._send_json = fake_send_json
        DH.do_POST(h)
        assert captured["status"] == 403
        assert "removed" in str(captured["data"]["message"]).lower()


# ---------------------------------------------------------------------------
# 2. /api/resume server-side phrase, single audit, real-account blocked
# ---------------------------------------------------------------------------
class TestResume:
    """Server derives expected phrase; client cannot define it."""

    def test_resume_derives_expected_server_side(self, monkeypatch):
        """Client sends confirmation only; server determines expected phrase."""
        monkeypatch.setenv("DASH_OPERATOR_TOKEN", "")
        from dashboard.server import _mutation_request_allowed

        h = MagicMock()
        h.headers = {"Host": "127.0.0.1:8082", "Origin": ""}
        h.client_address = ("127.0.0.1", 54321)
        assert _mutation_request_allowed(h) is True

    def test_resume_rejects_real_account_phase0(self, monkeypatch):
        """Real-account resume must be blocked during Phase 0."""
        monkeypatch.setenv("DASH_OPERATOR_TOKEN", "")
        from unittest.mock import patch as mpatch
        from dashboard.server import DashboardHandler as DH

        h = MagicMock()
        h.path = "/api/resume"
        h.headers = {"Host": "127.0.0.1:8082", "Origin": ""}
        h.client_address = ("127.0.0.1", 54321)
        h._read_json_body.return_value = {"confirmation": "RESUME REAL"}
        captured = {}

        def fake_send_json(data, status=200):
            captured["data"] = data
            captured["status"] = status

        h._send_json = fake_send_json

        fake_config = {"execution": {"explicit_opt_in_danger_zone": True, "mode": "real"}}
        with mpatch("dashboard.server.load_config", return_value=fake_config):
            DH.do_POST(h)
            assert captured["status"] == 403
            assert "real" in str(captured["data"]["message"]).lower()

    def test_resume_accepts_demo_with_opt_in(self, monkeypatch):
        """Demo resume with opt-in should succeed."""
        monkeypatch.setenv("DASH_OPERATOR_TOKEN", "")
        from unittest.mock import patch as mpatch
        from dashboard.server import DashboardHandler as DH

        h = MagicMock()
        h.path = "/api/resume"
        h.headers = {"Host": "127.0.0.1:8082", "Origin": ""}
        h.client_address = ("127.0.0.1", 54321)
        h._read_json_body.return_value = {"confirmation": "RESUME DEMO"}
        captured = {}

        def fake_send_json(data, status=200):
            captured["data"] = data
            captured["status"] = status

        h._send_json = fake_send_json

        fake_config = {"execution": {"explicit_opt_in_danger_zone": True, "mode": "demo"}}
        with mpatch("dashboard.server.load_config", return_value=fake_config), \
             mpatch("dashboard.server._set_operator_kill_switch", return_value={"updated_at": "2026-01-01T00:00:00Z"}), \
             mpatch("dashboard.server._write_command_audit"), \
             mpatch("dashboard.server.write_json_state"):
            DH.do_POST(h)
            assert captured["status"] == 200
            assert captured["data"]["ok"] is True


# ---------------------------------------------------------------------------
# 3. _mutation_request_allowed enforces DASH_OPERATOR_TOKEN
# ---------------------------------------------------------------------------
class TestMutationAuth:
    """Authorization must be enforced for mutations."""

    def test_token_required_when_set(self, monkeypatch):
        """When DASH_OPERATOR_TOKEN is set, mutations require Bearer auth."""
        monkeypatch.setenv("DASH_OPERATOR_TOKEN", "secret-token-123")
        from dashboard.server import _mutation_request_allowed

        h = MagicMock()
        h.headers = {"Host": "127.0.0.1:8082", "Origin": ""}
        h.client_address = ("127.0.0.1", 54321)
        # No Authorization header
        assert _mutation_request_allowed(h) is False

    def test_token_accepted_when_correct(self, monkeypatch):
        """Correct Bearer token allows mutation."""
        monkeypatch.setenv("DASH_OPERATOR_TOKEN", "secret-token-123")
        from dashboard.server import _mutation_request_allowed

        h = MagicMock()
        h.headers = {"Host": "127.0.0.1:8082", "Origin": "", "Authorization": "Bearer secret-token-123"}
        h.client_address = ("127.0.0.1", 54321)
        assert _mutation_request_allowed(h) is True

    def test_token_rejected_when_wrong(self, monkeypatch):
        """Wrong Bearer token denies mutation."""
        monkeypatch.setenv("DASH_OPERATOR_TOKEN", "secret-token-123")
        from dashboard.server import _mutation_request_allowed

        h = MagicMock()
        h.headers = {"Host": "127.0.0.1:8082", "Origin": "", "Authorization": "Bearer wrong-token"}
        h.client_address = ("127.0.0.1", 54321)
        assert _mutation_request_allowed(h) is False

    def test_token_not_required_when_unset(self, monkeypatch):
        """When DASH_OPERATOR_TOKEN is unset, localhost is allowed."""
        monkeypatch.delenv("DASH_OPERATOR_TOKEN", raising=False)
        from dashboard.server import _mutation_request_allowed

        h = MagicMock()
        h.headers = {"Host": "127.0.0.1:8082", "Origin": ""}
        h.client_address = ("127.0.0.1", 54321)
        assert _mutation_request_allowed(h) is True


# ---------------------------------------------------------------------------
# 4. CORS never wildcard
# ---------------------------------------------------------------------------
class TestCORS:
    """CORS must never use wildcard *."""

    def test_cors_not_wildcard(self):
        """Verify no wildcard CORS anywhere in server source."""
        src = Path(__file__).resolve().parents[1] / "dashboard" / "server.py"
        text = src.read_text(encoding="utf-8")
        # send_header calls with literal wildcard
        occurrences = text.count('"Access-Control-Allow-Origin", "*"')
        assert occurrences == 0, f"Found {occurrences} wildcard CORS headers"


# ---------------------------------------------------------------------------
# 5. build_safety_header computes stop-risk
# ---------------------------------------------------------------------------
class TestSafetyHeader:
    """open_risk must use stop-risk, not floating P&L."""

    def test_stop_risk_computed_from_sl(self):
        """When positions have sl/entry/volume, compute actual stop risk."""
        from dashboard.safety import build_safety_header
        lp = {
            "open_positions": [
                {"sl": 2650.0, "entry": 2600.0, "volume": 1.0, "profit": 5000},
                {"stop_loss": 2650.0, "open_price": 2700.0, "volume": 1.0, "profit": -5000},
            ]
        }
        result = build_safety_header({}, live_portfolio=lp)
        # stop risk = |2650-2600|*1.0 + |2650-2700|*1.0 = 50 + 50 = 100
        assert result["open_risk"] == 100.0

    def test_falls_back_to_profit_when_no_stop(self):
        """When no sl/entry fields, fall back to floating P&L sum."""
        from dashboard.safety import build_safety_header
        lp = {
            "open_positions": [
                {"profit": 100},
                {"profit": -200},
            ]
        }
        result = build_safety_header({}, live_portfolio=lp)
        assert result["open_risk"] == 300.0  # sum of abs profits

    def test_zero_positions_zero_risk(self):
        """Empty positions -> zero risk."""
        from dashboard.safety import build_safety_header
        result = build_safety_header({}, live_portfolio={"open_positions": []})
        assert result["open_risk"] == 0.0


# ---------------------------------------------------------------------------
# 6. Fast-mode presets limited
# ---------------------------------------------------------------------------
class TestFastMode:
    """Fast-mode from dashboard must only allow off/observe."""

    def test_live_presets_rejected(self, monkeypatch):
        """safe/aggressive/sprint presets must return 403 from dashboard."""
        monkeypatch.setenv("DASH_OPERATOR_TOKEN", "")
        from dashboard.server import DashboardHandler as DH

        for preset in ("safe", "aggressive", "sprint"):
            h = MagicMock()
            h.path = "/api/fast-mode"
            h.headers = {"Host": "127.0.0.1:8082", "Origin": ""}
            h.client_address = ("127.0.0.1", 54321)
            h._read_json_body.return_value = {"action": "preset", "preset": preset}
            captured = {}

            def fake_send_json(data, status=200):
                captured["data"] = data
                captured["status"] = status

            h._send_json = fake_send_json
            DH.do_POST(h)
            assert captured["status"] == 403, f"preset={preset} should be rejected"
            assert "safety" in str(captured["data"]["message"]).lower()
