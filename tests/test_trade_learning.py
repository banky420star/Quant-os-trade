"""Trade learning pipeline — enrichment, archive, culturing cells."""

from __future__ import annotations

from core.learning_health import assess_trade_learning
from core.signal_archive import archive_signals, lookup_archive
from core.strategy_policy import culturing_cell_from_trade
from core.trade_enrichment import enrich_trade, build_signal_index
from core.utils import write_json_state


def test_archive_and_enrich_trade():
    sig = {
        "signal_id": "sig-1",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "setup_type": "pullback",
        "confidence": 88,
        "market_context": {
            "session": "london_open",
            "market_regime": {"primary": "trending", "bias": "bullish"},
            "move_type": "pullback",
        },
        "trigger_summary": "pullback retest",
    }
    archive_signals([sig])
    assert lookup_archive("sig-1") is not None

    trade = {
        "trade_id": "t1",
        "signal_id": "sig-1",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "pnl": 10.0,
        "result": "win",
    }
    idx = build_signal_index(include_archive=True)
    enriched = enrich_trade(trade, idx)
    assert enriched["market_context"]["session"] == "london_open"
    assert enriched["trigger_summary"] == "pullback retest"
    assert enriched.get("learning_enriched") is True


def test_culturing_cell_from_signal_meta():
    trade = {
        "setup_type": "pullback",
        "side": "BUY",
        "signal_meta": {
            "market_context": {
                "session": "london_open",
                "market_regime": {"primary": "trending", "bias": "bullish"},
            },
        },
    }
    cell = culturing_cell_from_trade(trade)
    assert "pullback|trending|align|london_open" == cell


def test_learning_health_empty():
    report = assess_trade_learning([])
    assert report["total_trades"] == 0
    assert report["sufficient"] is False