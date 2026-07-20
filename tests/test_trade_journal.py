"""Trade journal — conditions snapshot and per-symbol organized indexes."""

from __future__ import annotations

from core.trade_journal import (
    build_conditions,
    build_mgmt_narratives,
    build_organized_index,
    build_symbol_journal_payload,
    enrich_trade_record,
    group_trades_by_symbol,
    safe_journal_name,
    safe_symbol_name,
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
        "symbol": "EURUSDm",
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
    assert cond["symbol"] == "EURUSDm"
    assert cond["symbol_cell"].startswith("EURUSDm|")
    assert cond["entry"]["session"] == "new_york"
    assert cond["risk"]["volume"] == 0.02
    assert cond["exit"]["narratives"]["trail"] == "Trail active."


def test_build_mgmt_narratives():
    narr = build_mgmt_narratives(
        side="BUY",
        entry=100.0,
        sl=100.5,
        mgmt_row={"break_even": True, "trailing": True, "peak_price": 102.0},
    )
    assert narr["be"] and "Break-even" in narr["be"]
    assert narr["trail"] and "Trailing" in narr["trail"]


def test_per_symbol_organized_index_isolated():
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
            "conditions": {"culturing_cell": "breakout|strong|align|london", "symbol_cell": "XAUUSDm|breakout|strong|align|london"},
        },
        {
            "trade_id": "b",
            "symbol": "USOILm",
            "setup": "breakout",
            "session": "london_open",
            "regime_primary": "strong_trend",
            "result": "loss",
            "won": False,
            "pnl": -5,
            "r_multiple": -0.5,
            "closed_at": "2026-07-01T13:00:00+00:00",
            "conditions": {"culturing_cell": "breakout|strong|align|london", "symbol_cell": "USOILm|breakout|strong|align|london"},
        },
    ]
    org = build_organized_index(trades, configured_symbols=["XAUUSDm", "USOILm", "BTCUSDm"])
    assert org["by_symbol"]["XAUUSDm"]["n"] == 1
    assert org["by_symbol"]["USOILm"]["n"] == 1
    assert org["by_symbol"]["BTCUSDm"]["n"] == 0
    assert "by_setup" not in org
    xau = org["per_symbol"]["XAUUSDm"]
    oil = org["per_symbol"]["USOILm"]
    assert xau["organized"]["by_setup"]["breakout"]["n"] == 1
    assert oil["organized"]["by_setup"]["breakout"]["n"] == 1
    assert xau["organized"]["by_setup"]["breakout"]["wins"] == 1
    assert oil["organized"]["by_setup"]["breakout"]["wins"] == 0


def test_group_trades_by_symbol():
    trades = [{"symbol": "XAUUSDm", "trade_id": "1"}, {"symbol": "XAUUSDm", "trade_id": "2"}, {"symbol": "EURUSDm", "trade_id": "3"}]
    groups = group_trades_by_symbol(trades)
    assert len(groups["XAUUSDm"]) == 2
    assert len(groups["EURUSDm"]) == 1


def test_build_symbol_journal_payload():
    trades = [{"trade_id": "z", "symbol": "XAUUSDm", "won": True, "pnl": 1, "closed_at": "2026-07-02T10:00:00+00:00", "result": "win"}]
    payload = build_symbol_journal_payload("XAUUSDm", trades)
    assert payload["symbol"] == "XAUUSDm"
    assert payload["summary"]["n"] == 1
    assert payload["trade_ids"] == ["z"]


def test_safe_names():
    assert safe_journal_name("deal-12345") == "deal-12345"
    assert "/" not in safe_journal_name("ticket/999")
    assert safe_symbol_name("XAU/USD") == "XAU_USD"