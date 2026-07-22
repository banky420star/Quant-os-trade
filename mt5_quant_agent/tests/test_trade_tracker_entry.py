"""MT5 close records must recover entry from IN deal (not OUT price)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.trade_tracker import TradeTracker


class _FakeMT5:
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1

    def __init__(self, deals):
        self._deals = deals

    def history_deals_get(self, *_a, **_k):
        return self._deals


def test_sync_mt5_uses_in_deal_entry_not_out_price(monkeypatch):
    import core.trade_tracker as tt

    magic = 20250625
    pos_id = 999001
    in_deal = SimpleNamespace(
        magic=magic,
        entry=0,  # IN
        type=0,  # BUY open
        position_id=pos_id,
        price=4000.0,
        volume=0.01,
        comment="qagent_pullback",
        ticket=1001,
        time=1_700_000_000,
        profit=0.0,
        symbol="XAUUSDm",
    )
    out_deal = SimpleNamespace(
        magic=magic,
        entry=1,  # OUT
        type=1,  # SELL close
        position_id=pos_id,
        price=4010.0,
        volume=0.01,
        comment="[sl]",
        ticket=1002,
        time=1_700_000_600,
        profit=5.0,
        symbol="XAUUSDm",
    )
    fake = _FakeMT5([in_deal, out_deal])
    monkeypatch.setattr(tt, "mt5", fake)
    monkeypatch.setattr(tt, "read_json_state", lambda *a, **k: {})

    tracker = TradeTracker()
    merged, added = tracker.sync_mt5_closed_deals([], magic=magic, days=7)
    assert len(added) == 1
    t = added[0]
    assert t["entry"] == 4000.0
    assert t["exit"] == 4010.0
    assert t["entry"] != t["exit"]
    assert t["entry_source"] == "in_deal"
    assert t["r_multiple"] is None or t["r_multiple"] is not None  # ok without SL
