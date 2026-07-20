"""Tests for the adaptive gate throttle (adapt-to-losses layer)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.adaptive_gates import compute_adaptive_gates


def _cfg(**overrides):
    cfg = {
        "evaluation": {"min_policy_score": 35, "symbol_blocklist": ["UK100m"]},
        "signals": {"min_confidence": 50},
        "adaptation": {
            "adaptive_gates": {
                "enabled": True,
                "lookback": 15,
                "min_sample": 5,
                "cautious_win_rate": 40,
                "defensive_win_rate": 28,
                "defensive_consecutive": 4,
                "auto_block_streak": 3,
                "tighten_cautious_score": 10,
                "tighten_defensive_score": 20,
                "tighten_cautious_conf": 5,
                "tighten_defensive_conf": 10,
            }
        },
    }
    cfg["adaptation"]["adaptive_gates"].update(overrides)
    return cfg


def _losses(symbol, n, pnl=-0.02):
    return [{"symbol": symbol, "result": "loss", "pnl": pnl, "closed_at": f"2026-07-08T19:0{i}:00+00:00"} for i in range(n)]


def _wins(symbol, n, pnl=0.05):
    return [{"symbol": symbol, "result": "win", "pnl": pnl, "closed_at": f"2026-07-08T19:0{i}:00+00:00"} for i in range(n)]


def _patch(monkeypatch, trades):
    monkeypatch.setattr(
        "core.adaptive_gates.read_json_state",
        lambda name, default=None: ({"trades": trades} if name == "trade_log.json" else (default or {})),
    )
    monkeypatch.setattr("core.adaptive_gates.write_json_state", lambda name, doc: None)


def test_disabled_returns_baseline(monkeypatch):
    _patch(monkeypatch, [])
    cfg = _cfg(enabled=False)
    out = compute_adaptive_gates(cfg)
    assert out["enabled"] is False
    assert out["tier"] == "off"
    assert out["min_policy_score"] == 35
    assert out["blocked_symbols"] == ["UK100m"]


def test_winning_streak_stays_normal(monkeypatch):
    _patch(monkeypatch, _wins("USOILm", 10))
    out = compute_adaptive_gates(_cfg())
    assert out["tier"] == "normal"
    assert out["min_policy_score"] == 35
    assert out["blocked_symbols"] == ["UK100m"]


def test_losing_streak_tightens_to_defensive(monkeypatch):
    _patch(monkeypatch, _losses("XAUUSDm", 8))
    out = compute_adaptive_gates(_cfg())
    assert out["tier"] == "defensive"
    assert out["min_policy_score"] == 55  # 35 + 20
    assert out["consecutive_losses"] == 8


def test_mixed_cold_run_goes_cautious(monkeypatch):
    trades = _wins("USOILm", 3) + _losses("XAUUSDm", 7)
    _patch(monkeypatch, trades)
    out = compute_adaptive_gates(_cfg())
    # 7 losses most-recent -> defensive by consecutive (>=4) anyway
    assert out["tier"] in ("cautious", "defensive")
    assert out["min_policy_score"] >= 45


def test_auto_pauses_symbol_on_losing_streak(monkeypatch):
    trades = _losses("XAUUSDm", 3) + _wins("USOILm", 5)
    _patch(monkeypatch, trades)
    out = compute_adaptive_gates(_cfg())
    assert "XAUUSDm" in out["blocked_symbols"]
    assert "UK100m" in out["blocked_symbols"]  # static blocklist preserved
    assert "USOILm" not in out["blocked_symbols"]


def test_symbol_relaxes_after_a_win(monkeypatch):
    # XAU lost 3 (older) then won 1 (most-recent) -> streak broken, NOT blocked.
    trades = [
        {"symbol": "XAUUSDm", "result": "loss", "pnl": -0.02, "closed_at": "2026-07-08T19:00:00+00:00"},
        {"symbol": "XAUUSDm", "result": "loss", "pnl": -0.02, "closed_at": "2026-07-08T19:01:00+00:00"},
        {"symbol": "XAUUSDm", "result": "loss", "pnl": -0.02, "closed_at": "2026-07-08T19:02:00+00:00"},
        {"symbol": "XAUUSDm", "result": "win", "pnl": 0.05, "closed_at": "2026-07-08T19:30:00+00:00"},
    ]
    _patch(monkeypatch, trades)
    out = compute_adaptive_gates(_cfg())
    assert "XAUUSDm" not in out["blocked_symbols"]


def test_insufficient_sample_does_not_tighten(monkeypatch):
    _patch(monkeypatch, _losses("XAUUSDm", 3))
    out = compute_adaptive_gates(_cfg(min_sample=5))
    assert out["tier"] == "normal"
    assert out["min_policy_score"] == 35
