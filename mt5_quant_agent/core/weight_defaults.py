"""Shared subsystem weight defaults for decision and adaptive engines."""

from __future__ import annotations

SUBSYSTEM_WEIGHTS: dict[str, float] = {
    "trend_engine": 0.22,
    "structure_engine": 0.22,
    "momentum_engine": 0.15,
    "volume_engine": 0.13,
    "liquidity_engine": 0.10,
    "volatility_engine": 0.08,
    "risk_engine": 0.10,
}