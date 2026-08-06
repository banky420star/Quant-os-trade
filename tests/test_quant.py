"""Tests for edge database, adaptive weights, strategy ranker, and research helpers."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.adaptive_weights import AdaptiveWeightOptimizer, load_weights
from core.account_mode import validate_runtime_profile
from core.blue_guardian import blue_guardian_settings, max_lot_for_symbol
from core.edge_database import EdgeDatabase
from core.symbol_manager import SymbolManager
from core.strategy_ranker import StrategyRanker
from core.trade_enrichment import enrich_trade
from core.session_scorer import session_score
from loops.research_loop import _find_patterns, _replay_score


@pytest.fixture
def config():
    from core.utils import load_config
    return copy.deepcopy(load_config())


def _sample_trade(trade_id: str, result: str, tree: dict) -> dict:
    return {
        "trade_id": trade_id,
        "signal_id": f"sig-{trade_id}",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "setup_type": "trend_continuation",
        "entry": 2000.0,
        "exit": 2010.0 if result == "win" else 1990.0,
        "sl": 1990.0,
        "tp1": 2015.0,
        "pnl": 10.0 if result == "win" else -10.0,
        "result": result,
        "confidence": 75,
        "confidence_tree": tree,
        "market_context": {
            "session": "London",
            "market_regime": {"primary": "strong_trend"},
        },
        "closed_at": "2026-06-25T10:00:00Z",
    }


def test_edge_database_ingest_and_query(tmp_path, monkeypatch):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    db = EdgeDatabase()
    features = {"symbols": {"XAUUSDm": {"volatility_regime": "high", "spread_points": 12}}}
    context = {"symbols": {"XAUUSDm": {"session": "London", "regime": "trending"}}}

    trade = _sample_trade("t1", "win", {"trend_engine": 80, "structure_engine": 70})
    rec = db.ingest_trade(trade, features=features, context=context, source="test")
    assert rec is not None
    assert rec["setup_type"] == "trend_continuation"
    assert rec["session"] == "London"
    assert rec["volatility"] == "high"

    dup = db.ingest_trade(trade, features=features, context=context)
    assert dup is None

    stats = db.query_win_rate(setup_type="trend_continuation", session="London")
    assert stats["total"] == 1
    assert stats["win_rate_pct"] == 100.0


def test_edge_ranking_symbol_isolated(tmp_path, monkeypatch):
    """Symbol B rankings must not inherit symbol A pooled stats."""
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    db = EdgeDatabase()
    ctx_xau = {"symbols": {"XAUUSDm": {"session": "London"}}}
    ctx_eur = {"symbols": {"EURUSDm": {"session": "London"}}}
    tree = {"trend_engine": 70, "structure_engine": 70}

    for i in range(10):
        db.ingest_trade(_sample_trade(f"xau-w{i}", "win", tree), context=ctx_xau, source="test")
    for i in range(4):
        trade = _sample_trade(f"xau-l{i}", "loss", tree)
        trade["setup_type"] = "range_fade"
        db.ingest_trade(trade, context=ctx_xau, source="test")

    eur_trade = _sample_trade("eur-w0", "win", tree)
    eur_trade["symbol"] = "EURUSDm"
    db.ingest_trade(eur_trade, context=ctx_eur, source="test")

    xau_rank = db.rank_setups_for_context("XAUUSDm", "strong_trend", "London", min_samples=3)
    eur_rank = db.rank_setups_for_context("EURUSDm", "strong_trend", "London", min_samples=3)

    assert xau_rank
    assert xau_rank[0]["setup_type"] == "trend_continuation"
    assert xau_rank[0]["total"] >= 10
    assert not eur_rank or eur_rank[0]["total"] < xau_rank[0]["total"]
    if eur_rank:
        assert eur_rank[0]["total"] <= 1


def test_edge_database_rank_setups(tmp_path, monkeypatch):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    db = EdgeDatabase()
    ctx = {"symbols": {"XAUUSDm": {"session": "London"}}}
    tree = {"trend_engine": 70, "structure_engine": 70}

    for i in range(4):
        db.ingest_trade(
            _sample_trade(f"w{i}", "win", tree),
            context=ctx,
            source="test",
        )
    for i in range(2):
        trade = _sample_trade(f"l{i}", "loss", tree)
        trade["setup_type"] = "range_fade"
        db.ingest_trade(trade, context=ctx, source="test")

    rankings = db.rank_setups_for_context("XAUUSDm", "strong_trend", "London", min_samples=3)
    assert rankings
    assert rankings[0]["setup_type"] == "trend_continuation"


def test_adaptive_weights_optimize(tmp_path, monkeypatch, config):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    db = EdgeDatabase()
    for i in range(10):
        db.ingest_trade(
            _sample_trade(
                f"t{i}",
                "win" if i % 2 == 0 else "loss",
                {
                    "trend_engine": 85 if i % 2 == 0 else 40,
                    "structure_engine": 80 if i % 2 == 0 else 45,
                    "momentum_engine": 60,
                    "volume_engine": 55,
                    "liquidity_engine": 50,
                    "volatility_engine": 50,
                    "risk_engine": 70,
                },
            ),
            source="test",
        )

    opt = AdaptiveWeightOptimizer(config)
    result = opt.optimize(min_trades=8)
    assert result["status"] == "candidate"
    assert "trend_engine" in result["weights"]
    assert abs(sum(result["weights"].values()) - 1.0) < 0.02


def test_load_weights_defaults(config):
    weights = load_weights(config)
    assert "trend_engine" in weights
    assert sum(weights.values()) > 0.9


def test_symbol_manager_discovers_expanded_symbols(config, monkeypatch):
    from core import symbol_manager as smod

    fake_names = [
        SimpleNamespace(name="EURUSDm"),
        SimpleNamespace(name="GBPUSDm"),
        SimpleNamespace(name="USDJPYm"),
        SimpleNamespace(name="US500m"),
        SimpleNamespace(name="UK100m"),
    ]

    fake_mt5 = SimpleNamespace(
        symbols_get=lambda: fake_names,
        symbol_select=lambda symbol, enabled: True,
        copy_rates_from_pos=lambda symbol, timeframe, pos, count: [1],
        TIMEFRAME_M5=1,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setattr(smod, "mt5", fake_mt5)

    config["mt5"]["symbols"] = ["EURUSDm", "US500m", "UK100m"]
    manager = SymbolManager(config)
    result = manager.discover()

    assert result["resolved"]["EURUSDm"] == "EURUSDm"
    assert result["resolved"]["US500m"] == "US500m"
    assert result["resolved"]["UK100m"] == "UK100m"


def test_session_scorer_boosts_expanded_symbols():
    eur = session_score("london_open", "EURUSDm")
    idx = session_score("new_york", "US500m")
    aud = session_score("sydney", "AUDUSDm")

    assert eur["boost"] > 0
    assert idx["boost"] > 0
    assert aud["boost"] > 0


def test_strategy_ranker_allow_setup(config):
    config["quant"]["strategy_ranking_enabled"] = True
    config["signals"]["symbol_rules"] = {}
    ranker = StrategyRanker(config)
    # Mock rankings so the test doesn't depend on real edge-database state.
    ranker.rank_for_symbol = lambda *args, **kwargs: [
        {"setup_type": "trend_continuation", "score": 72.7, "win_rate_pct": 72.7,
         "total": 44, "rank": 1, "insufficient_data": False},
    ]
    ctx = {"session": "London", "market_regime": {"primary": "strong_trend"}}
    allowed, info = ranker.allow_setup("trend_continuation", "XAUUSDm", ctx, {})
    assert allowed is True
    assert info["allowed"] is True


def test_ranking_not_required_path_returns_reason(config):
    # Explicitly exercise the opt-out branch so its response schema remains
    # stable for callers that use a deliberately non-ranking profile.
    # Lock its info schema: allowed -> reason "ranking_not_required"; an
    # unranked setup -> "setup_unranked" (not a misleading win-rate label).
    config["quant"]["strategy_ranking_enabled"] = True
    config["quant"]["require_top_ranked_setup"] = False
    config["quant"]["min_rank_win_rate"] = 35
    config["quant"]["per_symbol"]["XAUUSDm"] = {}
    config["signals"]["symbol_rules"] = {}

    ranker = StrategyRanker(config)
    rankings = [
        {
            "setup_type": "pullback",
            "score": 47.1,
            "win_rate_pct": 47.1,
            "total": 68,
            "rank": 1,
            "insufficient_data": False,
        },
    ]
    ranker.rank_for_symbol = lambda *args, **kwargs: rankings  # type: ignore[method-assign]
    ctx = {"session": "London", "market_regime": {"primary": "expansion"}}

    allowed, info = ranker.allow_setup("pullback", "XAUUSDm", ctx, {})
    assert allowed is True
    assert info["reason"] == "ranking_not_required"

    allowed_unranked, info_unranked = ranker.allow_setup("mean_reversion", "XAUUSDm", ctx, {})
    assert allowed_unranked is False
    assert info_unranked["reason"] == "setup_unranked"


def test_active_profile_enables_bounded_ranking_flex(config):
    """The active profile must not silently disable the configured flex gate."""
    quant = config["quant"]
    assert quant["strategy_ranking_enabled"] is True
    assert quant["require_top_ranked_setup"] is True
    assert quant["ranking_flex_enabled"] is True
    assert quant["ranking_flex_min_win_rate"] > 0
    assert quant["ranking_flex_min_samples"] >= 1
    assert quant["ranking_flex_top_n"] >= 2

    ranker = StrategyRanker(config)
    assert ranker._allowed_rank_depth(
        [
            {"win_rate_pct": 41, "total": 20},
            {"win_rate_pct": 60, "total": 20},
        ]
    ) == quant["ranking_flex_top_n"]
    assert ranker._allowed_rank_depth(
        [
            {"win_rate_pct": 70, "total": 20},
            {"win_rate_pct": 60, "total": 20},
        ]
    ) == 1
    for symbol, policy in (quant.get("per_symbol") or {}).items():
        assert policy.get("require_top_ranked_setup") is True, symbol


def test_ranking_flex_allows_second_when_leader_is_weak(config):
    # sync_practice_gates can force strategy_ranking_enabled=False while the
    # aggressive growth plan is enabled. These tests exercise the flex/context-
    # align branches directly, so re-enable ranking explicitly here.
    config["quant"]["strategy_ranking_enabled"] = True
    config["quant"]["require_top_ranked_setup"] = True
    config["quant"]["ranking_flex_enabled"] = True
    config["quant"]["ranking_flex_min_win_rate"] = 55
    config["quant"]["ranking_flex_min_samples"] = 10
    config["quant"]["ranking_flex_top_n"] = 2
    # Keep this unit test independent of any profile-specific symbol override
    # so it drives the intended flex branch deterministically.
    config["quant"]["per_symbol"]["BTCUSDm"] = {}

    ranker = StrategyRanker(config)
    rankings = [
        {
            "setup_type": "pullback",
            "score": 47.1,
            "win_rate_pct": 47.1,
            "total": 68,
            "rank": 1,
            "insufficient_data": False,
        },
        {
            "setup_type": "trend_continuation",
            "score": 28.6,
            "win_rate_pct": 28.6,
            "total": 28,
            "rank": 2,
            "insufficient_data": False,
        },
    ]
    ranker.rank_for_symbol = lambda *args, **kwargs: rankings  # type: ignore[method-assign]
    ctx = {"session": "London", "market_regime": {"primary": "expansion"}}

    allowed_top, info_top = ranker.allow_setup("pullback", "BTCUSDm", ctx, {})
    allowed_second, info_second = ranker.allow_setup("trend_continuation", "BTCUSDm", ctx, {})
    allowed_third, _ = ranker.allow_setup("mean_reversion", "BTCUSDm", ctx, {})

    assert allowed_top is True
    assert info_top["reason"] == "top_n_flex"
    assert allowed_second is True
    assert info_second["allowed_depth"] == 2
    assert allowed_third is False


def test_ranking_flex_strict_when_leader_is_strong(config):
    # See note in test_ranking_flex_allows_second_when_leader_is_weak:
    # re-enable ranking (forced off by sync_practice_gates under growth plan).
    config["quant"]["strategy_ranking_enabled"] = True
    config["quant"]["require_top_ranked_setup"] = True
    config["quant"]["ranking_flex_enabled"] = True
    config["quant"]["ranking_flex_min_win_rate"] = 55
    config["quant"]["ranking_flex_min_samples"] = 10
    # Keep the strict branch independent of profile-specific symbol overrides.
    config["quant"]["per_symbol"]["XAUUSDm"] = {}
    config["signals"]["symbol_rules"] = {}

    ranker = StrategyRanker(config)
    rankings = [
        {
            "setup_type": "trend_continuation",
            "score": 72.7,
            "win_rate_pct": 72.7,
            "total": 44,
            "rank": 1,
            "insufficient_data": False,
        },
        {
            "setup_type": "pullback",
            "score": 45.5,
            "win_rate_pct": 45.5,
            "total": 44,
            "rank": 2,
            "insufficient_data": False,
        },
    ]
    ranker.rank_for_symbol = lambda *args, **kwargs: rankings  # type: ignore[method-assign]
    ctx = {"session": "London", "market_regime": {"primary": "weak_trend"}}

    allowed_top, info_top = ranker.allow_setup("trend_continuation", "XAUUSDm", ctx, {})
    allowed_second, info_second = ranker.allow_setup("pullback", "XAUUSDm", ctx, {})

    assert allowed_top is True
    assert info_top["reason"] == "top_ranked"
    assert allowed_second is False
    assert info_second["reason"] == "not_top_ranked"


def test_context_align_allows_pullback_in_pullback_market(config):
    # See note in test_ranking_flex_allows_second_when_leader_is_weak:
    # re-enable ranking (forced off by sync_practice_gates under growth plan).
    config["quant"]["strategy_ranking_enabled"] = True
    config["quant"]["require_top_ranked_setup"] = True
    config["quant"]["ranking_flex_enabled"] = True
    config["quant"]["ranking_context_align"] = True
    config["quant"]["ranking_flex_top_n"] = 2
    # Keep this branch independent of profile-specific symbol overrides.
    config["quant"]["per_symbol"]["XAUUSDm"] = {}
    # XAUUSDm's live symbol rule restricts allowed_setups to
    # [pullback, trend_continuation], which short-circuits mean_reversion with
    # "symbol_setup_restricted" before the ranking gate ever runs. Neutralize
    # the rules so the test drives the context-align rejection path.
    config["signals"]["symbol_rules"] = {}

    ranker = StrategyRanker(config)
    rankings = [
        {
            "setup_type": "trend_continuation",
            "score": 57.1,
            "win_rate_pct": 57.1,
            "total": 28,
            "rank": 1,
            "insufficient_data": False,
        },
        {
            "setup_type": "pullback",
            "score": 46.7,
            "win_rate_pct": 46.7,
            "total": 60,
            "rank": 2,
            "insufficient_data": False,
        },
    ]
    ranker.rank_for_symbol = lambda *args, **kwargs: rankings  # type: ignore[method-assign]
    ctx = {
        "session": "new_york",
        "move_type": "pullback",
        "market_regime": {"primary": "strong_trend"},
    }

    allowed_pullback, info_pullback = ranker.allow_setup("pullback", "XAUUSDm", ctx, {})
    allowed_other, info_other = ranker.allow_setup("mean_reversion", "XAUUSDm", ctx, {})

    assert allowed_pullback is True
    assert info_pullback["reason"] == "context_aligned"
    assert allowed_other is False
    assert info_other["reason"] == "not_top_ranked"


def test_resolve_trading_session_london_open():
    from core.session_scorer import resolve_trading_session

    assert resolve_trading_session(8) == "london_open"
    assert resolve_trading_session(13) == "overlap_london_ny"
    assert resolve_trading_session(17) == "new_york"
    assert resolve_trading_session(23) == "rollover"


def test_session_score_xau_overlap_boost(config):
    from core.session_scorer import session_score

    result = session_score("overlap_london_ny", "XAUUSDm", config)
    assert result["raw_score"] == 10
    assert result["quality"] == "excellent"


def test_trade_score_blocks_low_session(config):
    from core.trade_score import compute_trade_score

    config["session_scoring"]["enabled"] = True
    config["session_scoring"]["min_trade_score"] = 80
    config["trading"]["aggressive_mode"] = False

    feat = {
        "volume_ratio": 0.3,
        "volatility_regime": "high",
        "atr_ratio": 0.002,
    }
    ctx = {"trend_strength": "weak", "session": "sydney"}
    rank_info = {"score": 40, "win_rate_pct": 40}

    score = compute_trade_score("XAUUSDm", feat, ctx, rank_info, config, session="sydney")
    assert score["enabled"] is True
    assert score["total"] < 80
    assert score["passed"] is False


def test_trade_score_passes_gold_london_open(config):
    from core.trade_score import compute_trade_score

    config["session_scoring"]["enabled"] = True
    config["session_scoring"]["min_trade_score"] = 80
    config["trading"]["aggressive_mode"] = False

    feat = {
        "volume_ratio": 0.9,
        "volatility_regime": "high",
        "atr_ratio": 0.002,
        "spread_points": 50,
    }
    ctx = {"trend_strength": "strong", "session": "london_open"}
    rank_info = {"score": 72, "win_rate_pct": 72}

    score = compute_trade_score("XAUUSDm", feat, ctx, rank_info, config, session="london_open")
    assert score["passed"] is True
    assert score["total"] >= 80


def test_trade_enrichment():
    trade = {"trade_id": "t1", "signal_id": "sig-1", "symbol": "XAUUSDm", "result": "win"}
    index = {
        "sig-1": {
            "signal_id": "sig-1",
            "confidence": 82,
            "confidence_tree": {"trend_engine": 90},
            "setup_type": "breakout",
            "market_context": {"session": "Asia"},
        }
    }
    enriched = enrich_trade(trade, index)
    assert enriched["confidence"] == 82
    assert enriched["setup_type"] == "breakout"
    assert enriched["signal_meta"]["confidence_tree"]["trend_engine"] == 90


def test_runtime_profile_guard_passes_real_live(config):
    config["mt5"]["account_mode"] = "real"
    config["performance"]["apply_when"] = "real"
    config["practice"]["growth"]["enabled"] = False
    result = validate_runtime_profile(config)
    assert result["account_mode"] == "real"
    assert result["performance_plan_active"] is True


def test_blue_guardian_caps_new_fx_symbols(growth_profile):
    from core.utils import load_config

    config = copy.deepcopy(load_config())
    config["blue_guardian"]["enabled"] = True
    bg = blue_guardian_settings(config)
    symbols = list((config.get("mt5") or {}).get("symbols") or [])
    per_sym = int((config.get("blue_guardian") or {}).get("max_open_per_symbol", 1))
    assert bg["max_total_open_positions"] == len(symbols) * per_sym
    assert max_lot_for_symbol(config, "EURUSDm", 0.1) == 0.02
    assert max_lot_for_symbol(config, "GBPUSDm", 0.1) == 0.02
    assert max_lot_for_symbol(config, "USOILm", 0.1) == 0.01


def test_cell_memory_veto_prefers_cell_over_global(config):
    from core.consensus_gates import ConsensusGates

    # config.yaml zeroes the memory-veto win-rate thresholds, which disables
    # the veto entirely (wr < 0 is impossible). Set explicit thresholds so
    # this test deterministically exercises the cell veto path.
    config["intelligence"]["memory_veto_win_rate"] = 40
    config["intelligence"]["memory_veto_cell_win_rate"] = 40
    config["intelligence"]["memory_veto_min_trades"] = 5
    config["intelligence"]["memory_veto_cell_min_trades"] = 4

    gates = ConsensusGates(config)
    signal = {
        "symbol": "XAUUSDm",
        "side": "SELL",
        "setup_type": "trend_continuation",
        "market_context": {"session": "london_open"},
    }
    votes = {"risk_engine": 80, "trend_engine": 70, "structure_engine": 70}
    edge_scores = {
        "setup_stats": {
            "global": {
                "trend_continuation": {"total": 100, "win_rate_pct": 62.0},
            },
            "by_symbol": {
                "XAUUSDm": {
                    "trend_continuation": {"total": 100, "win_rate_pct": 62.0},
                }
            },
            "by_cell": {
                "XAUUSDm|trend_continuation|strong_trend|london_open": {
                    "trend_continuation": {"total": 12, "win_rate_pct": 33.3},
                }
            },
        }
    }
    passed, vetoes = gates.evaluate(signal, votes, edge_scores=edge_scores, market_regime={"primary": "strong_trend"})
    assert passed is False
    assert any(v.startswith("cell_memory_veto") for v in vetoes)


def test_memory_veto_requires_full_min_trades_for_global_fallback(config):
    from core.consensus_gates import ConsensusGates

    # Same explicit thresholds as test_cell_memory_veto_prefers_cell_over_global
    # so this test is deterministic regardless of config.yaml drift.
    config["intelligence"]["memory_veto_win_rate"] = 40
    config["intelligence"]["memory_veto_cell_win_rate"] = 40
    config["intelligence"]["memory_veto_min_trades"] = 5
    config["intelligence"]["memory_veto_cell_min_trades"] = 4

    gates = ConsensusGates(config)
    signal = {
        "symbol": "XAUUSDm",
        "side": "BUY",
        "setup_type": "pullback",
        "market_context": {"session": "london_open"},
    }
    votes = {"risk_engine": 80, "trend_engine": 70, "structure_engine": 70}
    # Samples below intelligence.memory_veto_min_trades (5 since 2026-07-21)
    # must not trigger the memory veto — too little evidence to act on.
    edge_scores = {
        "setup_stats": {
            "global": {
                "pullback": {"total": 4, "win_rate_pct": 20.0},
            },
            "by_symbol": {
                "XAUUSDm": {
                    "pullback": {"total": 4, "win_rate_pct": 20.0},
                }
            },
            "by_cell": {},
        }
    }
    passed, vetoes = gates.evaluate(signal, votes, edge_scores=edge_scores, market_regime={"primary": "strong_trend"})
    assert passed is True
    assert not any(v.startswith("memory_veto") for v in vetoes)


def test_replay_score_and_patterns(tmp_path, monkeypatch):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    db = EdgeDatabase()
    ctx = {"symbols": {"XAUUSDm": {"session": "London"}}}
    tree = {"trend_engine": 70, "structure_engine": 70}
    for i in range(6):
        db.ingest_trade(_sample_trade(f"p{i}", "win", tree), context=ctx, source="test")

    patterns = _find_patterns(db)
    assert patterns
    assert patterns[0]["type"] == "high_edge"
    assert _replay_score({"pnl_total": 50, "win_rate_pct": 60, "trades_closed": 10}) > 0
