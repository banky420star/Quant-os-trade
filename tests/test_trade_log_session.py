"""Session-scoped trade log filtering."""

from __future__ import annotations

from scripts.build_trade_log import _session_trades


def test_session_trades_only_since_baseline():
    trades = [
        {"closed_at": "2026-07-02T10:00:00+00:00", "pnl": 1},
        {"closed_at": "2026-07-02T18:50:00+00:00", "pnl": 2},
        {"closed_at": "2026-07-02T19:00:00+00:00", "pnl": 3},
    ]
    baseline = {"set_at": "2026-07-02T18:47:28+00:00"}
    session = _session_trades(trades, baseline)
    assert len(session) == 2
    assert all(t["closed_at"] >= baseline["set_at"] for t in session)


def test_session_trades_empty_after_reset_not_whole_day():
    trades = [
        {"closed_at": "2026-07-02T10:00:00+00:00", "pnl": -5},
        {"closed_at": "2026-07-02T12:00:00+00:00", "pnl": 1},
    ]
    baseline = {"set_at": "2026-07-02T18:47:28+00:00", "source": "practice_session_reset"}
    assert _session_trades(trades, baseline) == []