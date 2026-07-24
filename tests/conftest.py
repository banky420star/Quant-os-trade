"""Shared pytest fixtures — isolate tests from state/active_profile.json."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_STATE_DIR = Path(__file__).resolve().parent.parent / "state"
_ACTIVE_PROFILE = _STATE_DIR / "active_profile.json"


@pytest.fixture(autouse=True)
def _isolate_active_profile():
    """Undo profile activation leaked by tests that call set_active_profile().

    Without this, a test that activates e.g. the growth profile leaves
    state/active_profile.json behind and every later load_config() in the
    suite silently runs with that overlay applied (order-dependent failures).
    """
    before = _ACTIVE_PROFILE.read_bytes() if _ACTIVE_PROFILE.exists() else None
    yield
    os.environ.pop("MT5_QUANT_PROFILE", None)
    if before is None:
        _ACTIVE_PROFILE.unlink(missing_ok=True)
    else:
        _ACTIVE_PROFILE.write_bytes(before)


@pytest.fixture
def growth_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force full growth demo overlay (14 symbols, base trailing/BG caps)."""
    monkeypatch.setenv("MT5_QUANT_PROFILE", "growth")


@pytest.fixture
def micro_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force $30-real micro live overlay."""
    monkeypatch.setenv("MT5_QUANT_PROFILE", "30-real")


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers so the @pytest.mark.integration tests under
    tests/test_payoff_paradox_meter.py don't trip the
    PytestUnknownMarkWarning. The integration marker opt-in to
    production-state tests; the default pytest invocation ('not integration')
    skips them so unit assertions stay cheap."""
    config.addinivalue_line(
        "markers",
        "integration: integration test that requires real state files "
        "(state/trade_log.json). Run with -m integration explicitly.",
    )