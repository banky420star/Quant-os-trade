"""Trade journal — conditions snapshot and organized indexes."""

from __future__ import annotations

from core.trade_journal import (
    build_conditions,
    build_mgmt_narratives,
    build_organized_index,
    enrich_trade_record,
    safe_journal_name,
    snapshot_signal_meta,
)


def test_snapshot_signal_meta_captures_context():
    signal = {
        "signal_id": "sig-1",
        "symbol": "XAUUSDm",
        "side": "BUY",
        "setup_type": "breakout",
        "entry": 2400.0,
        "sl": 2390.0,
        "tp1": 2420.0,
        "confidence": 78,
        "confidence_tree": {"trend_engine": 85},
        "market_context": {
            "session": "london_open",
            "phase": "expansion",
            "market_regime": {"primary": "strong_trend", "bias": "bullish"},
        },
        "trade_score": {"total": 82, "passed": True, "threshold": 80},
        "strategy_rank": {"allowed": True, "reason": "top_setup", "rank": 1},
        "entry_narrative": "BUY breakout on London open.",
    }
    feat = {"price": 2400.0, "atr": 5.0, "m5_trend": "up"}
    meta = snapshot_signal_meta(signal, features_at_entry=feat, kelly={"fraction": 1.5, "gated": False})
    assert meta["session"] == "london_open"
    assert meta["regime_primary"] == "strong_trend"
    assert meta["features_at_entry"]["atr"] == 5.0
    assert meta["trade_score"]["total"] == 82


def test_build_conditions_and_enrich():
    meta = {
        "signal_id": "sig-2",
        "side": "SELL",
        "setup_type": "trend_continuation",
        "confidence": 80,
        "market_context": {
            "session": "new_york",
            "market_regime": {"primary": "range", "bias": "neutral"},
        },
        "entry_narrative": "SELL continuation.",
        "explain": {"gates": ["consensus"]},
    }
    order = {"order_id": "ord-1", "fill_price": 1.08, "created_at": "2026-07-01T10:00:00+00:00", "signal_meta": meta}
    rec = {
        "trade_id": "t-100",
        "symbol": "EURUSDm",
        "side": "SELL",
        "setup": "trend_continuation",
        "result": "win",
        "won": True,
        "pnl": 12.5,
        "r_multiple": 1.2,
        "volume": 0.02,
        "sl_initial": 1.082,
        "sl": 1.081,
        "risk_price": 0.002,
        "opened_at": "2026-07-01T10:00:00+00:00",
        "closed_at": "2026-07-01T11:00:00+00:00",
        "exit_reason": "trailing_stop",
        "be_narrative": "BE armed.",
        "trail_narrative": "Trail active.",
        "exit_narrative": "Exit +1.2R.",
    }
    enrich_trade_record(rec, order=order, raw_trade={"signal_meta": meta})
    cond = rec["conditions"]
    assert cond["culturing_cell"]
    assert cond["entry"]["session"] == "new_york"
    assert cond["entry"]["explain"] == {"gates": ["consensus"]}
    assert cond["risk"]["volume"] == 0.02
    assert cond["exit"]["narratives"]["trail"] == "Trail active."
    assert rec["trade_score_total"] is None
    assert rec["entry_narrative"] == "SELL continuation."


def test_build_mgmt_narratives():
    narr = build_mgmt_narratives(
        side="BUY",
        entry=100.0,
        sl=100.5,
        mgmt_row={"break_even": True, "trailing": True, "peak_price": 102.0},
    )
    assert narr["be"] and "Break-even" in narr["be"]
    assert narr["trail"] and "Trailing" in narr["trail"]


def test_build_organized_index():
    trades = [
        {
            "trade_id": "a",
            "symbol": "XAUUSDm",
            "setup": "breakout",
            "session": "london_open",
            "regime_primary": "strong_trend",
            "result": "win",
            "won": True,
            "pnl": 10,
            "r_multiple": 1.0,
            "closed_at": "2026-07-01T12:00:00+00:00",
            "conditions": {"culturing_cell": "XAU|breakout|strong|london"},
        },
        {
            "trade_id": "b",
            "symbol": "XAUUSDm",
            "setup": "fade",
            "session": "asia",
            "regime_primary": "range",
            "result": "loss",
            "won": False,
            "pnl": -5,
            "r_multiple": -0.5,
            "closed_at": "2026-07-01T13:00:00+00:00",
            "conditions": {"culturing_cell": "XAU|fade|range|asia"},
        },
    ]
    org = build_organized_index(trades)
    assert org["by_symbol"]["XAUUSDm"]["n"] == 2
    assert org["by_symbol"]["XAUUSDm"]["wins"] == 1
    assert org["by_setup"]["breakout"]["n"] == 1
    assert org["by_cell"]["XAU|breakout|strong|london"]["n"] == 1


def test_safe_journal_name():
    assert safe_journal_name("deal-12345") == "deal-12345"
    assert "/" not in safe_journal_name("ticket/999")