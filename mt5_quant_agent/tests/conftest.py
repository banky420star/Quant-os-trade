"""Shared pytest fixtures — isolate tests from state/active_profile.json."""

from __future__ import annotations

import pytest


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