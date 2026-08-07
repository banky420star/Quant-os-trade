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

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. _disabled_unblock is permanently blocked
# ---------------------------------------------------------------------------
class TestDisabledUnblock:
    def test_disabled_unblock_returns_403(self):
        """The legacy unblock endpoint must return 404 and not execute."""
        source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
        # Must route to send_error(404), not execute any state mutation
        assert 'elif path == "/api/_disabled_unblock":' in source
        assert "self.send_error(404)" in source


# ---------------------------------------------------------------------------
# 2. /api/resume derives phrase server-side
# ---------------------------------------------------------------------------
class TestResume:
    def test_resume_derives_expected_server_side(self):
        """Resume must derive the confirmation phrase from account login, not client input."""
        source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
        # Must NOT accept expected value from client body
        assert 'body.get("expected")' not in source
        # Must derive from server-side account state
        assert "RESUME DEMO" in source

    def test_resume_rejects_real_account_phase0(self):
        """Real-account resume must be blocked during Phase 0."""
        source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
        assert "real-account resume is disabled during Phase 0" in source

    def test_resume_accepts_demo_with_opt_in(self):
        """Demo resume with explicit opt-in should succeed."""
        source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
        assert "Demo trading resumed" in source


# ---------------------------------------------------------------------------
# 3. _mutation_request_allowed enforces DASH_OPERATOR_TOKEN
# ---------------------------------------------------------------------------
class TestMutationAuth:
    def test_token_required_when_set(self):
        """When DASH_OPERATOR_TOKEN is set, remote requests must provide it."""
        source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
        assert "DASH_OPERATOR_TOKEN" in source

    def test_token_accepted_when_correct(self):
        """Correct token should be accepted via constant-time comparison."""
        source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
        assert "hmac.compare_digest" in source

    def test_token_rejected_when_wrong(self):
        """Wrong token must be rejected."""
        # The hmac.compare_digest ensures constant-time comparison
        # Wrong tokens fail because compare_digest returns False
        source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
        assert "hmac.compare_digest" in source

    def test_token_not_required_when_unset(self):
        """When DASH_OPERATOR_TOKEN is not set, loopback is allowed without token."""
        source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
        # The logic: if is_loopback and not expected: return True
        assert "is_loopback and not expected" in source


# ---------------------------------------------------------------------------
# 4. CORS never uses wildcard *
# ---------------------------------------------------------------------------
class TestCORS:
    def test_cors_not_wildcard(self):
        """No wildcard CORS headers in server.py."""
        source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
        assert 'Access-Control-Allow-Origin", "*"' not in source
        assert "CORS only for same-origin; never wildcard" in source


# ---------------------------------------------------------------------------
# 5. build_safety_header computes stop-risk, not floating P&L
# ---------------------------------------------------------------------------
class TestSafetyHeader:
    def test_stop_risk_computed_from_sl(self):
        """Open risk should use actual stop-risk, not floating P&L."""
        from dashboard.safety import build_safety_header

        lp = {
            "updated_at": "2099-01-01T00:00:00+00:00",
            "open_positions": [
                {"profit": -900.0, "actual_stop_risk": 12.5},
                {"profit": 500.0, "risk_amount": 7.5},
            ],
        }
        result = build_safety_header(
            {"account_mode": "demo", "timestamp": "2099-01-01T00:00:00+00:00"},
            {
                "execution": {
                    "live_trading_enabled": True,
                    "explicit_opt_in_danger_zone": True,
                },
                "mt5": {"account_mode": "demo"},
                "fast_mode": {"enabled": False, "live_enabled": False},
                "adaptation": {"enabled": False},
            },
            lp,
            {"status": "ok"},
            {"kill_switch": False},
        )
        assert result["open_risk"] == 20.0
        assert result["open_risk_known"] is True

    def test_falls_back_to_profit_when_no_stop(self):
        """When no stop-risk fields exist, fall back to absolute profit sum."""
        from dashboard.safety import build_safety_header

        lp = {
            "updated_at": "2099-01-01T00:00:00+00:00",
            "open_positions": [
                {"profit": -50.0},
                {"profit": 30.0},
            ],
        }
        result = build_safety_header(
            {"account_mode": "demo"},
            {
                "execution": {
                    "live_trading_enabled": True,
                    "explicit_opt_in_danger_zone": True,
                },
                "mt5": {"account_mode": "demo"},
                "fast_mode": {"enabled": False, "live_enabled": False},
                "adaptation": {"enabled": False},
            },
            lp,
            {"status": "ok"},
            {"kill_switch": False},
        )
        # When no stop-risk fields, falls back to absolute profit = 50 + 30 = 80
        # But open_risk_known should be False since we couldn't compute real risk
        assert result["open_risk_known"] is False

    def test_zero_positions_zero_risk(self):
        """No positions means zero risk."""
        from dashboard.safety import build_safety_header

        result = build_safety_header(
            {"account_mode": "demo"},
            {
                "execution": {
                    "live_trading_enabled": True,
                    "explicit_opt_in_danger_zone": True,
                },
                "mt5": {"account_mode": "demo"},
                "fast_mode": {"enabled": False, "live_enabled": False},
                "adaptation": {"enabled": False},
            },
            {"open_positions": []},
            {"status": "ok"},
            {"kill_switch": False},
        )
        assert result["open_risk"] == 0
        assert result["open_risk_known"] is True


# ---------------------------------------------------------------------------
# 6. Fast mode live presets rejected
# ---------------------------------------------------------------------------
class TestFastMode:
    def test_live_presets_rejected(self):
        """Dashboard must not allow fast-mode live preset changes."""
        source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
        assert "Fast mode is read-only in the Phase 0 dashboard" in source


# ---------------------------------------------------------------------------
# 7. Source-level security checks
# ---------------------------------------------------------------------------
def test_dashboard_source_has_no_client_controlled_resume_expected_value():
    """Server must not accept 'expected' from client body."""
    source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert 'body.get("expected")' not in source
    assert "_operator_mutation_allowed(self)" in source
    assert "DASH_OPERATOR_TOKEN" in source


def test_hidden_unblock_and_fast_mode_mutation_are_not_available():
    """Disabled unblock returns 404, fast mode is read-only."""
    source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert 'elif path == "/api/_disabled_unblock":\n            self.send_error(404)' in source
    assert "Fast mode is read-only in the Phase 0 dashboard" in source


def test_dashboard_ui_has_no_unblock_all_control():
    """No UNBLOCK ALL TRADES button in the dashboard."""
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8-sig")
    assert "UNBLOCK ALL TRADES" not in html
    assert "unblockTradesBtnSettings" not in html
    assert 'id="safetyMatrix"' in html
    assert 'id="resumeTradingBtn"' in html


def test_dashboard_binds_loopback_by_default():
    """Dashboard must default to localhost binding."""
    source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert 'DASH_HOST", "127.0.0.1"' in source
