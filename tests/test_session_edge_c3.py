"""Session Edge Specialist — profile 30-c3, cell ranking, adaptation evolution."""

from __future__ import annotations

import pytest

from core.micro_profile import micro_profile_enabled
from core.profile_launcher import load_profile_overlay, set_active_profile
from core.strategy_ranker import StrategyRanker
from core.utils import load_config, write_json_state
from quant.research.adaptation_evolution import evolve_symbol_policy
from quant.research.cell_ranking import cell_matches_seed, promotion_candidates, rank_cells_for_symbol
from quant.research.trade_log_cells import cell_expectancy_report


@pytest.fixture
def c3_config(monkeypatch):
    monkeypatch.setenv("MT5_QUANT_PROFILE", "30-c3")
    return load_config()


def test_profile_30_c3_session_filters(c3_config):
    assert micro_profile_enabled(c3_config)
    assert c3_config["mt5"]["symbols"] == ["XAUUSDm", "USOILm", "UK100m"]
    xau = c3_config["signals"]["symbol_rules"]["XAUUSDm"]
    assert xau["allowed_setups"] == ["pullback"]
    assert xau["strict_sessions"] is True
    assert set(xau["preferred_sessions"]) == {"rollover", "tokyo"}
    oil = c3_config["signals"]["symbol_rules"]["USOILm"]
    assert oil["preferred_sessions"] == ["tokyo"]
    uk = c3_config["signals"]["symbol_rules"]["UK100m"]
    assert uk["preferred_sessions"] == ["london_open"]
    assert c3_config["adaptation"]["auto_evolve_cells"] is True


def test_profile_overlay_loads():
    overlay = load_profile_overlay("30-c3")
    assert overlay["label"] == "$30 Session Edge (c3)"
    assert overlay["quant"]["cell_veto_demote"] is True


def test_cell_matches_seed_wildcard():
    seed = {"setup": "pullback", "sessions": ["tokyo"]}
    assert cell_matches_seed("pullback|strong_trend|align|tokyo", seed)
    assert not cell_matches_seed("breakout|strong_trend|align|tokyo", seed)


def test_promotion_and_veto_evolution():
    config = {
        "active_profile": "30-c3",
        "mt5": {"symbols": ["XAUUSDm", "USOILm", "UK100m"]},
        "culturing": {
            "min_n": 5,
            "promote_min_n": 4,
            "promote_min_win_rate_pct": 48,
            "promote_min_expectancy_r": 0.03,
        },
        "adaptation": {
            "auto_evolve_cells": True,
            "evolve_symbols": ["XAUUSDm", "USOILm", "UK100m"],
            "seed_edge_cells": [
                {"symbol": "XAUUSDm", "setup": "pullback", "sessions": ["tokyo"]},
            ],
        },
    }
    ledger = {
        "cells": {
            "XAUUSDm": {
                "pullback|strong_trend|align|tokyo": {
                    "n": 6, "win_rate_pct": 66.7, "expectancy_net_r": 0.25, "verdict": "ok",
                },
                "breakout|range|counter|new_york": {
                    "n": 8, "win_rate_pct": 25.0, "expectancy_net_r": -0.4, "verdict": "vetoed",
                },
            },
        },
        "vetoed": {"XAUUSDm": ["breakout|range|counter|new_york"]},
    }
    promoted = promotion_candidates(
        "XAUUSDm",
        ledger["cells"]["XAUUSDm"],
        config,
        vetoed={"breakout|range|counter|new_york"},
    )
    assert "pullback|strong_trend|align|tokyo" in promoted

    evolved = evolve_symbol_policy(ledger, config)
    sym = evolved["symbols"]["XAUUSDm"]
    assert "pullback|strong_trend|align|tokyo" in sym["promoted_cells"]
    assert "breakout|range|counter|new_york" in sym["vetoed_cells"]
    assert evolved["evolution"]["positive_evolution"] is True


def test_rank_cells_demotes_vetoed():
    config = {
        "quant": {
            "per_symbol": {
                "XAUUSDm": {
                    "preferred_setups": ["pullback"],
                    "preferred_sessions": ["tokyo"],
                },
            },
        },
        "adaptation": {
            "seed_edge_cells": [
                {"symbol": "XAUUSDm", "setup": "pullback", "sessions": ["tokyo"]},
            ],
        },
    }
    cells = {
        "pullback|strong_trend|align|tokyo": {
            "n": 5, "win_rate_pct": 60, "expectancy_net_r": 0.2, "verdict": "ok",
        },
        "breakout|range|counter|new_york": {
            "n": 6, "win_rate_pct": 30, "expectancy_net_r": -0.3, "verdict": "vetoed",
        },
    }
    ranked = rank_cells_for_symbol(
        "XAUUSDm",
        cells,
        config=config,
        vetoed={"breakout|range|counter|new_york"},
    )
    assert ranked[0]["cell"].startswith("pullback|")


def test_trade_log_cell_expectancy(tmp_path, monkeypatch):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    write_json_state("trade_log.json", {
        "trades": [
            {
                "trade_id": "t1",
                "symbol": "XAUUSDm",
                "setup": "pullback",
                "side": "BUY",
                "session": "tokyo",
                "regime_primary": "strong_trend",
                "regime_bias": "bullish",
                "result": "win",
                "won": True,
                "r_multiple": 1.2,
                "pnl": 2.5,
                "culturing_cell": "pullback|strong_trend|align|tokyo",
            },
            {
                "trade_id": "t2",
                "symbol": "USOILm",
                "setup": "pullback",
                "side": "SELL",
                "session": "tokyo",
                "regime_primary": "weak_trend",
                "regime_bias": "bearish",
                "result": "loss",
                "won": False,
                "r_multiple": -1.0,
                "pnl": -1.0,
                "culturing_cell": "pullback|weak_trend|align|tokyo",
            },
        ],
    })

    report = cell_expectancy_report(symbols=["XAUUSDm", "USOILm", "UK100m"])
    assert report["trade_count"] == 2
    assert report["edge_target_total"] == 5
    xau_target = next(r for r in report["edge_targets"] if r["target"] == "XAUUSDm pullback@tokyo")
    assert xau_target["n"] == 1
    assert xau_target["expectancy_r"] == 1.2


def test_ranker_boosts_promoted_session_cell(c3_config, tmp_path, monkeypatch):
    from core import utils

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(utils, "STATE_DIR", state_dir)

    c3_config["quant"]["strategy_ranking_enabled"] = True
    write_json_state("symbol_policy_live.json", {
        "symbols": {
            "XAUUSDm": {
                "promoted_cells": ["pullback|*|align|tokyo"],
                "vetoed_cells": [],
                "cell_rankings": [
                    {
                        "setup": "pullback",
                        "cell": "pullback|strong_trend|align|tokyo",
                        "expectancy_net_r": 0.3,
                        "verdict": "ok",
                        "n": 5,
                    },
                ],
            },
        },
    })

    ranker = StrategyRanker(c3_config)
    rankings = [
        {"setup_type": "trend_continuation", "score": 55, "win_rate_pct": 55, "total": 10, "insufficient_data": False},
        {"setup_type": "pullback", "score": 50, "win_rate_pct": 50, "total": 8, "insufficient_data": False},
    ]
    ranker.edge_db.rank_setups_for_context = lambda *args, **kwargs: rankings  # type: ignore[method-assign]
    ctx = {"session": "tokyo", "market_regime": {"primary": "strong_trend"}}
    out = ranker.rank_for_symbol("XAUUSDm", ctx, {})
    pullback = next(r for r in out if r["setup_type"] == "pullback")
    assert pullback.get("quant_boost", 0) >= 12.0