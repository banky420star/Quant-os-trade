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