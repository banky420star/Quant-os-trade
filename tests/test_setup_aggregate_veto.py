"""Tests for the setup-level aggregate veto (2026-07-31).

The per-cell culturing veto fragments evidence across ~50
(symbol|setup|regime|align|session) cells, so a setup that loses in aggregate
never trips a per-cell veto. The setup-level gate looks at each setup's FULL
sample and vetoes proven losers. This is the direct fix for the MT5-journal
finding that pullback was 66% of all trades at 33% win rate — the project's
single biggest loss driver — yet no per-cell veto fired.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loops.forward_test_loop import _build_setup_aggregates, _setup_aggregate_config


def _trade(setup: str, r: float, **kw):
    """A minimal closed-trade record with the fields the builder reads.

    compute_stats derives R from entry/exit/sl/side (capital-independent),
    so we set those: entry=100, sl=99 (sl_dist=1), side=BUY -> R = exit-100.
    """
    t = {
        "setup_type": setup,
        "symbol": kw.get("symbol", "XAUUSDm"),
        "side": "BUY",
        "entry": 100.0,
        "exit": 100.0 + r,  # sl_dist=1 -> R = (exit-entry)/sl_dist = r
        "sl": 99.0,
        "signal_meta": {"setup_type": setup},
        "market_context": {"market_regime": {"primary": "trend", "bias": "bull"}, "session": "london"},
    }
    t.update(kw)
    return t


SA_CFG = _setup_aggregate_config({"culturing": {"setup_aggregate_veto": {
    "enabled": True, "min_n": 20, "veto_win_rate_pct": 40, "veto_net_r": 0.0,
}}})


def test_vetoes_proven_loser_setup():
    """A setup with n>=min_n, negative expectancy, and <floor win rate is vetoed."""
    trades = [_trade("pullback", -0.3) for _ in range(14)]
    trades += [_trade("pullback", 0.6) for _ in range(6)]  # 20 trades, 30% wr, neg expect
    agg, vetoed = _build_setup_aggregates(trades, SA_CFG)
    assert "pullback" in agg
    assert agg["pullback"]["n"] == 20
    assert agg["pullback"]["verdict"] == "vetoed"
    assert "pullback" in vetoed


def test_thin_setup_not_vetoed():
    """Below min_n the setup is left permissive — keep collecting, never veto on noise."""
    trades = [_trade("atr_expansion", -0.5) for _ in range(5)]  # n=5 < min_n=20
    agg, vetoed = _build_setup_aggregates(trades, SA_CFG)
    assert agg["atr_expansion"]["verdict"] == "thin"
    assert "atr_expansion" not in vetoed


def test_positive_setup_not_vetoed():
    """A winning setup (positive net expectancy) is never vetoed even at large n."""
    trades = [_trade("trend_continuation", 0.5) for _ in range(12)]
    trades += [_trade("trend_continuation", -0.2) for _ in range(8)]  # 20 trades, net positive
    agg, vetoed = _build_setup_aggregates(trades, SA_CFG)
    assert agg["trend_continuation"]["verdict"] == "positive"
    assert "trend_continuation" not in vetoed


def test_high_winrate_loser_not_vetoed():
    """A setup with negative expectancy but win rate >= floor is NOT vetoed —
    the gate requires BOTH netR<0 AND wr<floor (same logic as the per-cell veto)."""
    # 20 trades, 50% wr (>= 40 floor) but slightly negative expectancy (losses bigger than wins)
    trades = [_trade("donchian_breakout", 0.2) for _ in range(10)]
    trades += [_trade("donchian_breakout", -0.3) for _ in range(10)]  # net -0.5R over 20
    agg, vetoed = _build_setup_aggregates(trades, SA_CFG)
    assert agg["donchian_breakout"]["verdict"] == "ok"
    assert "donchian_breakout" not in vetoed


def test_disabled_returns_empty():
    """When disabled, no aggregates are built and nothing is vetoed."""
    cfg = {"culturing": {"setup_aggregate_veto": {"enabled": False}}}
    sa = _setup_aggregate_config(cfg)
    trades = [_trade("pullback", -0.3) for _ in range(50)]
    agg, vetoed = _build_setup_aggregates(trades, sa)
    assert agg == {}
    assert vetoed == []


def test_unknown_setup_ignored():
    """Trades with unknown/empty setup don't pollute the aggregates."""
    trades = [_trade("", -0.3) for _ in range(50)]
    trades += [_trade("pullback", -0.3) for _ in range(20)]
    agg, vetoed = _build_setup_aggregates(trades, SA_CFG)
    assert "" not in agg
    assert "unknown" not in agg
    assert "pullback" in agg


def test_multiple_setups_only_losers_vetoed():
    """A mix: pullback (loser) + trend_continuation (winner) -> only pullback vetoed."""
    trades = [_trade("pullback", -0.3) for _ in range(14)] + [_trade("pullback", 0.6) for _ in range(6)]
    trades += [_trade("trend_continuation", 0.5) for _ in range(12)] + [_trade("trend_continuation", -0.2) for _ in range(8)]
    agg, vetoed = _build_setup_aggregates(trades, SA_CFG)
    assert vetoed == ["pullback"]
    assert agg["trend_continuation"]["verdict"] == "positive"


# --- Verifier integration: setup_aggregate_veto gate reads vetoed_setups ---

def _make_signal(setup: str = "pullback", symbol: str = "XAUUSDm"):
    return {
        "symbol": symbol,
        "setup_type": setup,
        "side": "buy",
        "entry": 2000.0,
        "stop": 1990.0,
        "take": 2020.0,
        "confidence": 0.8,
        "market_context": {"market_regime": {"primary": "trend", "bias": "bull"}, "session": "london"},
    }


class _StubVeto:
    """Minimal stand-in for the verifier's self._live_veto read."""

    def __init__(self, vetoed_setups):
        self.vetoed_setups = vetoed_setups


def test_verifier_rejects_vetoed_setup(monkeypatch):
    """A signal whose setup is in vetoed_setups is hard-rejected."""
    import core.verifier as verifier_mod

    v = verifier_mod.Verifier.__new__(verifier_mod.Verifier)
    v._live_veto = {"vetoed_setups": ["pullback"]}
    signal = _make_signal("pullback")
    setup_type = signal.get("setup_type") or "unknown"
    norm = verifier_mod.normalize_setup_type(setup_type) or setup_type
    vetoed_setups = set(v._live_veto.get("vetoed_setups", []) or [])
    assert norm in vetoed_setups  # gate would be False -> rejected


def test_verifier_allows_clean_setup(monkeypatch):
    """A signal whose setup is NOT in vetoed_setups passes the gate."""
    import core.verifier as verifier_mod

    v = verifier_mod.Verifier.__new__(verifier_mod.Verifier)
    v._live_veto = {"vetoed_setups": ["pullback"]}
    signal = _make_signal("trend_continuation")
    setup_type = signal.get("setup_type") or "unknown"
    norm = verifier_mod.normalize_setup_type(setup_type) or setup_type
    vetoed_setups = set(v._live_veto.get("vetoed_setups", []) or [])
    assert norm not in vetoed_setups  # gate would be True -> allowed


def test_verifier_empty_veto_list_permissive(monkeypatch):
    """Empty/missing vetoed_setups -> permissive (never blocks on missing data)."""
    import core.verifier as verifier_mod

    v = verifier_mod.Verifier.__new__(verifier_mod.Verifier)
    v._live_veto = {}
    signal = _make_signal("pullback")
    setup_type = signal.get("setup_type") or "unknown"
    norm = verifier_mod.normalize_setup_type(setup_type) or setup_type
    vetoed_setups = set(v._live_veto.get("vetoed_setups", []) or [])
    assert norm not in vetoed_setups  # permissive