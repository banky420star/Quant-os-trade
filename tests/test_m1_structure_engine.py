"""M1 Structure Engine — comprehensive validation tests (correctness closure).

Covers:
  1. BOS/CHoCH/FVG/OB detection with the provisional/confirmed state machine
  2. Genuine FORMING semantics: confirmation only when the defining M1 candle
     closes (a strictly newer bar arrives), never before
  3. Non-repainting proof: once CONFIRMED, identity fields never change
  4. Touch lifecycle: only closed candles strictly after the defining candle,
     deduplicated per bar time; forming zones never count
  5. Invalidation: zones become INVALIDATED and can never re-trigger
  6. Decision chain: WAIT until 2+ confirmed on one side, newest confirmation
     on that side, and a close beyond the newest structure level
  7. Ledger: every create/confirm/invalidate transition appended, idempotent
     per (event_id, status), detected_at preserved, confirmed_at set once
  8. Loop wiring: shadow loop enabled/no-data behavior
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Ensure core is importable
SYS_PATH = str(Path(__file__).resolve().parents[1])
if SYS_PATH not in sys.path:
    sys.path.insert(0, SYS_PATH)

from core.m1_structure_engine import (
    M1StructureEngine,
    StructureEvent,
    read_structure_events,
    write_structure_event,
)
from core.utils import STATE_DIR

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COUNTER = {"n": 0}

# Captured once per test process, so every _t(minute) call returns an IDENTICAL
# timestamp (tests compare regenerated times against the engine's captured
# defining_candle_time) while staying inside the freshness window: _t(13) is
# the import moment (age ~0s) and the M1 suite runs in well under the
# 90s max_data_age_seconds gate.
_ANCHOR = datetime.now(timezone.utc).replace(microsecond=0)


def _t(minute: int) -> str:
    """ISO bar time: minute 13 ~= import time, minute 0 = 13 minutes earlier.

    Relative ordering is preserved: larger minutes are always later than
    smaller ones, and the values are deterministic within a test process.
    """
    return (_ANCHOR - timedelta(minutes=13 - minute)).isoformat()


def _fresh_iso(age_seconds: float) -> str:
    """ISO timestamp `age_seconds` before now — explicit freshness control."""
    return (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()


def _bar(o: float, h: float, l: float, c: float, t: str) -> dict:
    """Create an M1 candle dict."""
    return {"open": o, "high": h, "low": l, "close": c, "time": t, "volume": 1000}


def _unique_sym(prefix: str) -> str:
    # The structure ledger is persistent on disk and idempotent per event_id,
    # so the symbol (and any event_id embedding it) must be unique ACROSS test
    # processes too — otherwise a rerun would collide with (and be suppressed
    # by) records written by an earlier run.
    _COUNTER["n"] += 1
    return f"{prefix}{_COUNTER['n']}_{time.time_ns()}"


def _engine(config: dict | None = None) -> M1StructureEngine:
    if config is None:
        config = {"m1_structure": {"enabled": True}}
    return M1StructureEngine(config)


def _bull_swings() -> list[dict]:
    """Higher-high/higher-low series ending BELOW the last swing high (101.2)."""
    return [
        _bar(100.0, 100.4, 99.7, 100.2, _t(0)),
        _bar(100.2, 100.6, 100.0, 100.4, _t(1)),
        _bar(100.4, 100.8, 100.2, 100.6, _t(2)),   # swing high #1 (100.8)
        _bar(100.5, 100.7, 100.1, 100.3, _t(3)),
        _bar(100.2, 100.5, 100.0, 100.2, _t(4)),
        _bar(100.2, 100.7, 100.1, 100.5, _t(5)),
        _bar(100.5, 101.0, 100.3, 100.8, _t(6)),
        _bar(100.8, 101.2, 100.6, 101.0, _t(7)),   # swing high #2 (101.2)
        _bar(101.0, 101.15, 100.7, 100.9, _t(8)),
        _bar(100.8, 101.0, 100.5, 100.7, _t(9)),
    ]


def _bear_swings() -> list[dict]:
    """Lower-low/lower-high series ending ABOVE the last swing low (109.0)."""
    return [
        _bar(110.0, 110.3, 109.6, 109.8, _t(0)),
        _bar(109.8, 110.1, 109.4, 109.6, _t(1)),
        _bar(109.6, 109.9, 109.2, 109.4, _t(2)),   # swing low #1 (109.2)
        _bar(109.5, 109.8, 109.3, 109.7, _t(3)),
        _bar(109.8, 110.1, 109.6, 110.0, _t(4)),
        _bar(109.9, 110.2, 109.5, 109.7, _t(5)),   # swing high (110.2)
        _bar(109.6, 109.9, 109.2, 109.4, _t(6)),
        _bar(109.4, 109.7, 109.0, 109.2, _t(7)),   # swing low #2 (109.0)
        _bar(109.3, 109.6, 109.1, 109.5, _t(8)),
        _bar(109.6, 109.9, 109.4, 109.7, _t(9)),
    ]


def _confirmed_event(event_type, side, top, bottom, eid, defining, price=None):
    return StructureEvent(
        event_type=event_type, symbol="T", timeframe="M1", side=side,
        status="CONFIRMED", detected_at="2026-08-07T12:00:00+00:00",
        confirmed_at="2026-08-07T12:01:00+00:00",
        price=price if price is not None else (top + bottom) / 2,
        top=top, bottom=bottom, touches=0, provisional_touches=0,
        candle_closed=True, later_modified=False,
        event_id=eid, defining_candle_time=defining,
    )


def _forming_event(event_type, side, top, bottom, eid, defining):
    return StructureEvent(
        event_type=event_type, symbol="T", timeframe="M1", side=side,
        status="FORMING", detected_at="2026-08-07T12:00:00+00:00",
        confirmed_at=None, price=(top + bottom) / 2, top=top, bottom=bottom,
        touches=0, provisional_touches=0, candle_closed=False,
        later_modified=False, event_id=eid, defining_candle_time=defining,
    )


def _raw_ledger_records(symbol: str) -> list[dict]:
    """Read the raw (uncollapsed) ledger lines for a symbol."""
    path = STATE_DIR / "m1_structure_events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("symbol") == symbol:
            out.append(rec)
    return out


# ===========================================================================
# 1. BOS / CHoCH detection + provisional/confirmed state machine
# ===========================================================================


class TestBOSDetection:
    def test_bos_detected_above_swing_high_and_stays_forming(self):
        """BOS fires on the break bar and stays FORMING until a newer bar."""
        eng = _engine()
        bars = _bull_swings()
        eng.update("T", bars, bars[-1]["close"], atr=1.0)
        bars.append(_bar(100.9, 101.6, 100.8, 101.4, _t(10)))  # break above 101.2
        eng.update("T", bars, 101.4, atr=1.0)
        bos = [e for e in eng._events["T"]
               if e.event_type == "bos" and e.side == "bullish"]
        assert len(bos) >= 1, "BOS should be detected on the break bar"
        for e in bos:
            assert e.status == "FORMING", "freshly detected BOS must be FORMING"
            assert e.candle_closed is False
            assert e.confirmed_at is None
            assert e.defining_candle_time == _t(10)
            assert e.event_id == f"T|bos|bullish|{_t(10)}"
        dec = eng.get_decision("T")
        assert dec["state"] == "WAIT", "forming-only evidence must stay WAIT"

    def test_bos_confirmed_only_after_defining_candle_closes(self):
        """BOS confirms only when a strictly newer bar arrives."""
        eng = _engine()
        bars = _bull_swings()
        eng.update("T", bars, bars[-1]["close"], atr=1.0)
        bars.append(_bar(100.9, 101.6, 100.8, 101.4, _t(10)))
        eng.update("T", bars, 101.4, atr=1.0)
        bars.append(_bar(101.4, 101.7, 101.2, 101.5, _t(11)))  # candle close
        eng.update("T", bars, 101.5, atr=1.0)
        bos = [e for e in eng._events["T"]
               if e.event_type == "bos" and e.side == "bullish"]
        confirmed = [e for e in bos if e.status == "CONFIRMED"]
        assert len(confirmed) >= 1, "BOS must confirm after the defining candle closes"
        assert all(e.candle_closed for e in confirmed)
        assert all(e.confirmed_at for e in confirmed)

    def test_forming_never_confirmed_without_newer_bar(self):
        """Re-running with the same last bar never confirms a forming event."""
        eng = _engine()
        bars = _bull_swings()
        eng.update("T", bars, bars[-1]["close"], atr=1.0)
        bars.append(_bar(100.9, 101.6, 100.8, 101.4, _t(10)))
        # Feed the same bar list repeatedly — no newer bar ever appears.
        for _ in range(3):
            eng.update("T", list(bars), 101.4, atr=1.0)
        bos = [e for e in eng._events["T"]
               if e.event_type == "bos" and e.side == "bullish"]
        assert all(e.status == "FORMING" for e in bos)

    def test_choch_bullish_on_bearish_break(self):
        """CHoCH fires when bearish structure is broken above the swing high."""
        eng = _engine()
        bars = _bear_swings()
        eng.update("T", bars, bars[-1]["close"], atr=1.0)
        bars.append(_bar(109.2, 109.6, 108.4, 108.6, _t(10)))  # BOS bearish
        eng.update("T", bars, 108.6, atr=1.0)
        bars.append(_bar(110.0, 110.5, 109.9, 110.4, _t(11)))  # CHoCH bullish
        eng.update("T", bars, 110.4, atr=1.0)
        choch = [e for e in eng._events["T"]
                 if e.event_type == "choch" and e.side == "bullish"]
        assert len(choch) >= 1, "CHoCH bullish should fire after the break above"
        assert all(e.status == "FORMING" for e in choch)


# ===========================================================================
# 2. FVG and Order Block deterministic detection
# ===========================================================================


class TestFVGDetection:
    def test_bullish_fvg_detected(self):
        eng = _engine()
        bars = [
            _bar(100, 101, 99, 100.5, _t(0)),
            _bar(100.5, 101.5, 100, 101, _t(1)),
            _bar(102, 103, 101.5, 102.5, _t(2)),  # gap up (101.0 -> 101.5)
        ]
        eng.update("T", bars, 102.5, atr=1.0)
        fvg = [e for e in eng.get_events("T")
               if e["event_type"] == "fvg" and e["side"] == "bullish"]
        assert len(fvg) >= 1
        assert fvg[0]["bottom"] == 101.0
        assert fvg[0]["top"] == 101.5
        assert fvg[0]["status"] == "FORMING"
        assert fvg[0]["defining_candle_time"] == _t(2)

    def test_bearish_fvg_detected(self):
        eng = _engine()
        bars = [
            _bar(102, 103, 101.5, 102, _t(0)),
            _bar(101.5, 102, 100.5, 101, _t(1)),
            _bar(100, 100.5, 99, 99.5, _t(2)),  # gap down
        ]
        eng.update("T", bars, 99.5, atr=1.0)
        fvg = [e for e in eng.get_events("T")
               if e["event_type"] == "fvg" and e["side"] == "bearish"]
        assert len(fvg) >= 1

    def test_fvg_requires_min_atr_gap(self):
        eng = _engine({"m1_structure": {"enabled": True, "min_fvg_atr": 1.0}})
        bars = [
            _bar(100, 101, 99, 100.5, _t(0)),
            _bar(100.5, 101.5, 100, 101, _t(1)),
            _bar(100.6, 101, 100.5, 100.8, _t(2)),  # tiny gap
        ]
        eng.update("T", bars, 100.8, atr=1.0)
        fvg = [e for e in eng.get_events("T") if e["event_type"] == "fvg"]
        assert len(fvg) == 0

    def test_fvg_metadata_includes_gap_atr(self):
        eng = _engine()
        bars = [
            _bar(100, 101, 99, 100.5, _t(0)),
            _bar(100.5, 101.5, 100, 101, _t(1)),
            _bar(102, 103, 101.5, 102.5, _t(2)),
        ]
        eng.update("T", bars, 102.5, atr=1.0)
        fvg = [e for e in eng.get_events("T") if e["event_type"] == "fvg"]
        assert len(fvg) >= 1
        assert "gap_size" in fvg[0]["metadata"]
        assert "gap_atr" in fvg[0]["metadata"]


class TestOrderBlockDetection:
    def test_bullish_ob_detected(self):
        eng = _engine()
        bars = [
            _bar(101, 101.5, 100.5, 100.6, _t(0)),   # bearish
            _bar(100.6, 102, 100.5, 101.8, _t(1)),   # bullish displacement
            _bar(101.8, 102, 101.5, 101.9, _t(2)),
        ]
        eng.update("T", bars, 101.9, atr=1.0)
        ob = [e for e in eng.get_events("T")
              if e["event_type"] == "order_block" and e["side"] == "bullish"]
        assert len(ob) >= 1

    def test_bearish_ob_detected(self):
        eng = _engine()
        bars = [
            _bar(100, 101, 99.5, 100.8, _t(0)),   # bullish
            _bar(100.8, 101, 99, 99.2, _t(1)),    # bearish displacement
            _bar(99.2, 99.5, 98.5, 99.3, _t(2)),
        ]
        eng.update("T", bars, 99.3, atr=1.0)
        ob = [e for e in eng.get_events("T")
              if e["event_type"] == "order_block" and e["side"] == "bearish"]
        assert len(ob) >= 1

    def test_ob_requires_min_displacement(self):
        eng = _engine({"m1_structure": {"enabled": True, "min_displacement_atr": 2.0}})
        bars = [
            _bar(101, 101.5, 100.5, 100.6, _t(0)),
            _bar(100.6, 101, 100.5, 100.9, _t(1)),  # weak displacement
            _bar(100.9, 101, 100.8, 100.95, _t(2)),
        ]
        eng.update("T", bars, 100.95, atr=1.0)
        ob = [e for e in eng.get_events("T") if e["event_type"] == "order_block"]
        assert len(ob) == 0


# ===========================================================================
# 3. Non-repainting proof
# ===========================================================================


class TestNonRepainting:
    def test_incremental_feed_never_repaints_confirmed_events(self):
        """Once CONFIRMED, identity fields never change as future bars arrive."""
        eng = _engine()
        series = _bull_swings() + [
            _bar(100.9, 101.6, 100.8, 101.4, _t(10)),  # break
            _bar(101.4, 101.7, 101.2, 101.5, _t(11)),  # close -> confirm
            _bar(101.4, 101.6, 101.2, 101.5, _t(12)),
            _bar(101.4, 101.5, 101.2, 101.4, _t(13)),
        ]
        eng.update("T", series[:11], 101.4, atr=1.0)
        eng.update("T", series[:12], 101.5, atr=1.0)
        snapshot = {
            e.event_id: (e.detected_at, e.confirmed_at, e.top, e.bottom, e.side)
            for e in eng._events["T"] if e.status == "CONFIRMED"
        }
        assert snapshot, "expected at least one confirmed event to snapshot"
        # Append future bars — previously confirmed events must not change.
        for i in range(12, len(series)):
            eng.update("T", series[:i + 1], series[i]["close"], atr=1.0)
        for e in eng._events["T"]:
            if e.event_id in snapshot:
                assert (
                    e.detected_at, e.confirmed_at, e.top, e.bottom, e.side
                ) == snapshot[e.event_id], f"CONFIRMED event repainted: {e.event_id}"
                assert e.later_modified is False

    def test_confirmed_fields_immutable_under_repeated_updates(self):
        eng = _engine()
        ev = _confirmed_event("fvg", "bullish", 101.5, 101.0, "T|fvg|bullish|t2", _t(2))
        eng._events["T"] = [ev]
        bars = [
            _bar(101.2, 101.6, 101.1, 101.5, _t(3)),
            _bar(101.4, 101.7, 101.2, 101.5, _t(4)),
            _bar(101.3, 101.6, 101.2, 101.4, _t(5)),
        ]
        for bar in bars:
            eng.update("T", [bar], bar["close"], atr=1.0)
        assert ev.detected_at == "2026-08-07T12:00:00+00:00"
        assert ev.confirmed_at == "2026-08-07T12:01:00+00:00"
        assert ev.top == 101.5 and ev.bottom == 101.0
        assert ev.side == "bullish" and not ev.later_modified


# ===========================================================================
# 4. Touch lifecycle
# ===========================================================================


class TestTouchCounting:
    def test_touches_only_count_closed_candles_after_defining_candle(self):
        eng = _engine()
        ev = _confirmed_event("fvg", "bullish", 101.0, 100.0, "T|fvg|bullish|t5", _t(5))
        ev.touches = 0
        eng._events["T"] = [ev]
        bars = [
            _bar(100.5, 100.7, 100.3, 100.5, _t(5)),   # defining candle itself: never
            _bar(100.4, 100.7, 100.3, 100.6, _t(6)),   # CLOSED, after defining: touch 1
            _bar(100.5, 100.8, 100.4, 100.6, _t(7)),   # CLOSED, after defining: touch 2
            _bar(100.5, 100.8, 100.4, 100.6, _t(7)),   # same bar time: dedupe
            _bar(100.7, 101.1, 100.6, 100.9, _t(8)),   # CURRENT/forming candle
        ]
        eng.update("T", bars, 100.9, atr=1.0)
        assert ev.touches == 2, ev.touches
        assert ev.provisional_touches == 1  # 100.9 in zone right now -> provisional only

    def test_current_candle_never_double_dips_as_confirmed_touch(self):
        """The forming candle's in-zone close is provisional ONLY — it must not
        also be counted as a confirmed touch (no semantic double-dip)."""
        eng = _engine()
        ev = _confirmed_event("fvg", "bullish", 101.0, 100.0, "T|fvg|bullish|t5", _t(5))
        ev.touches = 0
        eng._events["T"] = [ev]
        bars = [
            _bar(100.4, 100.7, 100.3, 100.6, _t(6)),   # CLOSED: touch 1
            _bar(100.7, 101.1, 100.6, 100.9, _t(7)),   # CURRENT/forming, in zone
        ]
        eng.update("T", bars, 100.9, atr=1.0)
        assert ev.touches == 1, "current candle must not add a confirmed touch"
        assert ev.provisional_touches == 1

    def test_forming_zone_never_counts_confirmed_touches(self):
        eng = _engine()
        ev = _forming_event("fvg", "bullish", 101.0, 100.0, "T|fvg|bullish|t8", _t(8))
        eng._events["T"] = [ev]  # defining candle is in the FUTURE -> stays FORMING
        bars = [
            _bar(100.5, 100.7, 100.3, 100.6, _t(5)),
            _bar(100.4, 100.7, 100.3, 100.6, _t(6)),
            _bar(100.5, 100.8, 100.4, 100.6, _t(7)),
        ]
        eng.update("T", bars, 100.6, atr=1.0)
        assert ev.status == "FORMING"
        assert ev.touches == 0

    def test_provisional_touch_resets_each_tick(self):
        eng = _engine()
        ev = _confirmed_event("fvg", "bullish", 101.0, 100.0, "T|fvg|bullish|t5", _t(5))
        eng._events["T"] = [ev]
        bar = _bar(100.4, 100.7, 100.3, 100.6, _t(6))
        eng.update("T", [bar], 100.6, atr=1.0)   # inside zone
        assert ev.provisional_touches == 1
        eng.update("T", [bar], 102.0, atr=1.0)   # outside zone
        assert ev.provisional_touches == 0


# ===========================================================================
# 5. Invalidation
# ===========================================================================


class TestInvalidation:
    def test_fvg_invalidated_when_filled(self):
        eng = _engine()
        ev = _confirmed_event("fvg", "bullish", 101.5, 101.0, "T|fvg|bullish|t2", _t(2))
        eng._events["T"] = [ev]
        bars = [_bar(100.8, 101.2, 100.6, 100.8, _t(3))]  # fills the gap
        eng.update("T", bars, 100.8, atr=1.0)
        assert ev.later_modified is True
        assert ev.status == "INVALIDATED"
        assert ev.invalidated_at
        assert eng.get_events("T") == []  # invalidated zones are not exposed

    def test_ob_invalidated_when_closed_through(self):
        eng = _engine()
        ev = _confirmed_event("order_block", "bearish", 101.0, 100.5,
                              "T|order_block|bearish|t1", _t(1))
        eng._events["T"] = [ev]
        bars = [_bar(101.0, 101.4, 100.9, 101.2, _t(2))]  # closes above the zone
        eng.update("T", bars, 101.2, atr=1.0)
        assert ev.status == "INVALIDATED"
        assert eng.get_events("T") == []

    def test_invalidated_zone_cannot_retrigger(self):
        eng = _engine()
        ev = _confirmed_event("order_block", "bullish", 101.0, 100.5,
                              "T|order_block|bullish|t1", _t(1))
        ev.later_modified = True
        ev.status = "INVALIDATED"
        eng._events["T"] = [ev]
        # Same shape that would normally re-detect this OB at the same defining candle.
        bars = [
            _bar(101.5, 101.8, 101.2, 101.3, _t(0)),   # bearish
            _bar(101.3, 102.5, 101.2, 102.2, _t(1)),   # bullish displacement
        ]
        eng.update("T", bars, 102.2, atr=1.0)
        assert len(eng._events["T"]) == 1, "invalidated zone must not re-trigger"


# ===========================================================================
# 6. Decision chain (WAIT until full confirmation chain)
# ===========================================================================


class TestDecisionChain:
    def test_no_structure_produces_wait(self):
        eng = _engine()
        bars = [_bar(100, 101, 99, 100.5, _t(i)) for i in range(5)]
        eng.update("T", bars, 100.5, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["state"] == "WAIT"
        assert dec["bias"] == "Neutral"

    def test_single_confirmed_event_is_wait(self):
        """One confirmed bullish structure is NOT enough to go BUY."""
        eng = _engine()
        eng._events["T"] = [
            _confirmed_event("bos", "bullish", 101.4, 101.2, "T|bos|bullish|t10", _t(10)),
        ]
        bars = [_bar(101.5, 101.8, 101.4, 101.7, _t(12))]
        eng.update("T", bars, 101.7, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["state"] == "WAIT"
        assert dec["bias"] == "Bullish"

    def test_forming_only_is_wait(self):
        """Forming structures can never produce an executable state."""
        eng = _engine()
        eng._events["T"] = [
            _forming_event("bos", "bullish", 101.4, 101.2, "T|bos|bullish|t10", _t(10)),
            _forming_event("fvg", "bullish", 101.6, 101.4, "T|fvg|bullish|t11", _t(11)),
        ]
        bars = [_bar(101.5, 101.8, 101.4, 101.7, _t(9))]  # older than defining candles
        eng.update("T", bars, 101.7, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["state"] == "WAIT"
        assert dec["bias"] == "Neutral"

    def test_full_bullish_chain_produces_buy(self):
        """2+ confirmed bullish, newest confirmation bullish, close above level."""
        eng = _engine()
        eng._events["T"] = [
            _confirmed_event("bos", "bullish", 101.4, 101.2, "T|bos|bullish|t10", _t(10)),
            _confirmed_event("fvg", "bullish", 101.6, 100.6, "T|fvg|bullish|t11", _t(11)),
        ]
        bars = [_bar(101.5, 101.8, 101.4, 101.7, _fresh_iso(30))]  # fresh: close 101.7 >= fvg top 101.6
        eng.update("T", bars, 101.7, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["state"] == "BUY", dec
        assert dec["bias"] == "Bullish"

    def test_full_bearish_chain_produces_sell(self):
        eng = _engine()
        eng._events["T"] = [
            _confirmed_event("bos", "bearish", 109.0, 108.6, "T|bos|bearish|t10", _t(10)),
            _confirmed_event("fvg", "bearish", 109.2, 108.8, "T|fvg|bearish|t11", _t(11)),
        ]
        bars = [_bar(108.7, 108.9, 108.4, 108.6, _fresh_iso(30))]  # fresh: close <= fvg bottom 108.8
        eng.update("T", bars, 108.6, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["state"] == "SELL", dec
        assert dec["bias"] == "Bearish"

    def test_conflicting_confirmed_evidence_stays_wait(self):
        """A newer bearish confirmation breaks the bullish chain -> WAIT."""
        eng = _engine()
        eng._events["T"] = [
            _confirmed_event("bos", "bullish", 101.4, 101.2, "T|bos|bullish|t10", _t(10)),
            _confirmed_event("fvg", "bullish", 101.6, 100.6, "T|fvg|bullish|t11", _t(11)),
            _confirmed_event("choch", "bearish", 101.2, 100.8, "T|choch|bearish|t12", _t(12)),
        ]
        bars = [_bar(100.8, 101.1, 100.7, 100.9, _t(13))]
        eng.update("T", bars, 100.9, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["state"] == "WAIT", dec
        assert dec["bias"] == "Mixed"

    def test_chain_uses_last_closed_candle_not_forming_candle(self):
        """A forming candle closing beyond the level must NOT trigger BUY —
        the confirmation chain uses the last CLOSED candle's close."""
        eng = _engine()
        eng._events["T"] = [
            _confirmed_event("bos", "bullish", 101.4, 101.2, "T|bos|bullish|t10", _t(10)),
            _confirmed_event("fvg", "bullish", 101.6, 100.6, "T|fvg|bullish|t11", _t(11)),
        ]
        bars = [
            _bar(101.2, 101.4, 101.0, 101.3, _fresh_iso(120)),  # CLOSED: below level 101.6
            _bar(101.6, 101.9, 101.5, 101.8, _fresh_iso(30)),   # FORMING: above level
        ]
        eng.update("T", bars, 101.8, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["state"] == "WAIT", dec
        assert dec["data_fresh"] is True

    def test_close_back_below_zone_breaks_chain(self):
        """Close below the newest bullish level (reclaim failed) -> WAIT."""
        eng = _engine()
        eng._events["T"] = [
            _confirmed_event("bos", "bullish", 101.4, 101.2, "T|bos|bullish|t10", _t(10)),
            _confirmed_event("fvg", "bullish", 101.6, 100.6, "T|fvg|bullish|t11", _t(11)),
        ]
        bars = [_bar(101.2, 101.4, 101.0, 101.3, _fresh_iso(30))]  # fresh; close 101.3 < fvg top 101.6
        eng.update("T", bars, 101.3, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["state"] == "WAIT", dec
        assert dec["bias"] == "Bullish"
        assert dec["data_fresh"] is True

    def test_disabled_engine_returns_wait(self):
        eng = _engine({"m1_structure": {"enabled": False}})
        eng.update("T", _bull_swings(), 105.0, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["state"] == "WAIT"
        assert dec["bias"] == "Neutral"
        assert dec["bull_trigger"] == "Engine disabled"

    def test_decision_has_required_fields(self):
        eng = _engine()
        bars = [_bar(100, 101, 99, 100.5, _t(i)) for i in range(5)]
        eng.update("T", bars, 100.5, atr=1.0)
        dec = eng.get_decision("T")
        required = [
            "symbol", "state", "bias", "active_zone", "zone_type",
            "zone_age_minutes", "touches_confirmed", "touches_provisional",
            "price_location", "bull_trigger", "bear_trigger",
            "candle_close_seconds", "data_age_seconds", "data_fresh", "updated_at",
        ]
        for field in required:
            assert field in dec, f"Missing field: {field}"

    def test_candle_close_countdown_is_live_utc(self):
        """Countdown derives from the UTC clock: 0..60 seconds to minute close."""
        eng = _engine()
        bars = [_bar(100, 101, 99, 100.5, _fresh_iso(30))]  # fresh: not gated
        eng.update("T", bars, 100.5, atr=1.0)
        dec = eng.get_decision("T")
        assert 0 <= dec["candle_close_seconds"] <= 60
        assert dec["data_fresh"] is True

    def test_data_age_uses_market_timestamp(self):
        """data_age reflects the market bar age, not the update call time."""
        eng = _engine()
        now = time.time()
        old_iso = datetime.fromtimestamp(now - 120, tz=timezone.utc).isoformat()
        bars = [_bar(100, 101, 99, 100.5, old_iso)]
        eng.update("T", bars, 100.5, atr=1.0)
        dec = eng.get_decision("T")
        assert 60 <= dec["data_age_seconds"] <= 180, dec["data_age_seconds"]

    def test_data_age_unknown_when_no_bar_time(self):
        eng = _engine()
        bars = [{"open": 100, "high": 101, "low": 99, "close": 100.5}]  # no time
        eng.update("T", bars, 100.5, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["data_age_seconds"] == -1.0
        assert dec["data_fresh"] is False


# ===========================================================================
# 6b. Stale-data fail-closed: a stale feed ALWAYS produces WAIT
# ===========================================================================


class TestStaleDataFailClosed:
    """Stale M1 data is an independent safety gate.

    A connected terminal with stale prices is not valid trading input, so no
    confirmed structure chain may publish BUY/SELL while the last market bar
    is older than max_data_age_seconds. The gate also zeroes the candle-close
    countdown (no live candle is actually in flight) and exposes the reason in
    the decision output (data_fresh + stale triggers).
    """

    @staticmethod
    def _bullish_chain(eng: M1StructureEngine) -> None:
        eng._events["T"] = [
            _confirmed_event("bos", "bullish", 101.4, 101.2, "T|bos|bullish|t10", _t(10)),
            _confirmed_event("fvg", "bullish", 101.6, 100.6, "T|fvg|bullish|t11", _t(11)),
        ]

    @staticmethod
    def _bearish_chain(eng: M1StructureEngine) -> None:
        eng._events["T"] = [
            _confirmed_event("bos", "bearish", 109.0, 108.6, "T|bos|bearish|t10", _t(10)),
            _confirmed_event("fvg", "bearish", 109.2, 108.8, "T|fvg|bearish|t11", _t(11)),
        ]

    def test_fresh_bullish_chain_produces_buy(self):
        """Fresh data + full bullish chain -> BUY."""
        eng = _engine()
        self._bullish_chain(eng)
        bars = [_bar(101.5, 101.8, 101.4, 101.7, _fresh_iso(30))]
        eng.update("T", bars, 101.7, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["state"] == "BUY", dec
        assert dec["data_fresh"] is True

    def test_fresh_bearish_chain_produces_sell(self):
        """Fresh data + full bearish chain -> SELL."""
        eng = _engine()
        self._bearish_chain(eng)
        bars = [_bar(108.7, 108.9, 108.4, 108.6, _fresh_iso(30))]
        eng.update("T", bars, 108.6, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["state"] == "SELL", dec
        assert dec["data_fresh"] is True

    def test_91s_stale_bullish_chain_forced_wait(self):
        """91s-old bar + full bullish chain -> WAIT with stale semantics."""
        eng = _engine()
        self._bullish_chain(eng)
        bars = [_bar(101.5, 101.8, 101.4, 101.7, _fresh_iso(91))]
        eng.update("T", bars, 101.7, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["state"] == "WAIT", dec
        assert dec["data_fresh"] is False
        assert dec["candle_close_seconds"] == 0
        assert dec["bull_trigger"] == "Stale M1 data"
        assert dec["bear_trigger"] == "Stale M1 data"

    def test_91s_stale_bearish_chain_forced_wait(self):
        """91s-old bar + full bearish chain -> WAIT."""
        eng = _engine()
        self._bearish_chain(eng)
        bars = [_bar(108.7, 108.9, 108.4, 108.6, _fresh_iso(91))]
        eng.update("T", bars, 108.6, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["state"] == "WAIT", dec
        assert dec["data_fresh"] is False
        assert dec["candle_close_seconds"] == 0
        assert dec["bull_trigger"] == "Stale M1 data"
        assert dec["bear_trigger"] == "Stale M1 data"

    def test_12h_stale_any_structure_is_wait(self):
        """12h-old bars: even a full bullish chain stays WAIT."""
        eng = _engine()
        self._bullish_chain(eng)
        bars = [_bar(101.5, 101.8, 101.4, 101.7, _fresh_iso(12 * 3600))]
        eng.update("T", bars, 101.7, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["state"] == "WAIT", dec
        assert dec["data_fresh"] is False
        assert dec["candle_close_seconds"] == 0

    def test_stale_threshold_is_configurable(self):
        """max_data_age_seconds is config-driven, not hard-coded at 90."""
        eng = _engine({"m1_structure": {"enabled": True, "max_data_age_seconds": 10}})
        self._bullish_chain(eng)
        bars = [_bar(101.5, 101.8, 101.4, 101.7, _fresh_iso(30))]
        eng.update("T", bars, 101.7, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["state"] == "WAIT", dec  # 30s > 10s threshold -> stale
        assert dec["data_fresh"] is False

    def test_default_threshold_is_90_seconds(self):
        eng = _engine()
        assert eng.max_data_age_seconds == 90.0

    def test_unknown_data_age_fails_closed(self):
        """Bars without a timestamp cannot be confirmed fresh -> WAIT."""
        eng = _engine()
        self._bullish_chain(eng)
        bars = [{"open": 101.5, "high": 101.8, "low": 101.4, "close": 101.7}]
        eng.update("T", bars, 101.7, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["state"] == "WAIT", dec
        assert dec["data_fresh"] is False
        assert dec["candle_close_seconds"] == 0
        assert dec["bull_trigger"] == "M1 data age unknown"

    def test_disabled_engine_decision_reports_not_fresh(self):
        eng = _engine({"m1_structure": {"enabled": False}})
        eng.update("T", _bull_swings(), 105.0, atr=1.0)
        dec = eng.get_decision("T")
        assert dec["data_fresh"] is False
        assert dec["state"] == "WAIT"


# ===========================================================================
# 7. Deduplication: legit later signals are allowed, same instance is not
# ===========================================================================


class TestDeduplication:
    def test_same_type_allowed_at_later_defining_candle(self):
        """A second BOS at a NEW defining candle must not be suppressed."""
        eng = _engine()
        bars = _bull_swings()
        eng.update("T", bars, bars[-1]["close"], atr=1.0)
        bars.append(_bar(100.9, 101.6, 100.8, 101.4, _t(10)))
        eng.update("T", bars, 101.4, atr=1.0)
        bars.append(_bar(101.4, 101.7, 101.2, 101.5, _t(11)))
        eng.update("T", bars, 101.5, atr=1.0)
        bars.append(_bar(101.3, 101.5, 101.0, 101.2, _t(12)))  # pullback
        eng.update("T", bars, 101.2, atr=1.0)
        bars.append(_bar(101.2, 101.9, 101.1, 101.8, _t(13)))  # breaks higher again
        eng.update("T", bars, 101.8, atr=1.0)
        bos_ids = {e.event_id for e in eng._events["T"]
                   if e.event_type == "bos" and e.side == "bullish"}
        assert len(bos_ids) >= 2, "later BOS at a new defining candle was suppressed"

    def test_same_event_id_not_duplicated(self):
        eng = _engine()
        bars = _bull_swings() + [_bar(100.9, 101.6, 100.8, 101.4, _t(10))]
        eng.update("T", bars, 101.4, atr=1.0)
        n_before = len(eng._events["T"])
        eng.update("T", bars, 101.4, atr=1.0)  # same bars again
        n_after = len(eng._events["T"])
        assert n_after == n_before


# ===========================================================================
# 8. Event ledger (immutable, idempotent per transition)
# ===========================================================================


class TestEventLedger:
    def test_engine_writes_create_confirm_invalidate_transitions(self):
        sym = _unique_sym("LG")
        eng = _engine()
        bars = _bull_swings()
        eng.update(sym, bars, bars[-1]["close"], atr=1.0)
        bars.append(_bar(100.9, 101.6, 100.8, 101.4, _t(10)))
        eng.update(sym, bars, 101.4, atr=1.0)   # create (FORMING)
        bars.append(_bar(101.4, 101.7, 101.2, 101.5, _t(11)))
        eng.update(sym, bars, 101.5, atr=1.0)   # confirm (CONFIRMED)
        bars.append(_bar(100.0, 100.3, 99.6, 99.8, _t(12)))  # crash
        eng.update(sym, bars, 99.8, atr=1.0)    # invalidate (INVALIDATED)

        raw = _raw_ledger_records(sym)
        # Scope to the FIRST BOS (defining candle t10). A second, later BOS at
        # t11 is expected and proves the dedupe fix allows legit later signals.
        first_bos = [r for r in raw
                     if r["event_type"] == "bos" and r["side"] == "bullish"
                     and r["defining_candle_time"] == _t(10)]
        assert first_bos, "BOS lifecycle must be in the ledger"
        statuses = {r["status"] for r in first_bos}
        assert {"FORMING", "CONFIRMED", "INVALIDATED"} <= statuses, statuses
        # detected_at preserved across all transitions; confirmed_at set once.
        detected = {r["detected_at"] for r in first_bos}
        assert len(detected) == 1
        confirmed_records = [r for r in first_bos if r["status"] == "CONFIRMED"]
        assert len(confirmed_records) == 1
        assert confirmed_records[0]["confirmed_at"]
        # A later BOS at a new defining candle is allowed (no over-dedupe).
        later_bos = [r for r in raw
                     if r["event_type"] == "bos" and r["side"] == "bullish"
                     and r["defining_candle_time"] == _t(11)]
        assert later_bos, "later BOS at a new defining candle must be allowed"
        # unique event ids
        eids = {r["event_id"] for r in raw}
        assert all(eids), "every ledger record needs an event_id"

    def test_ledger_transition_is_idempotent(self):
        """The same (event_id, status) transition is appended exactly once."""
        sym = _unique_sym("ID")
        eid = f"{sym}|bos|bullish|t5"
        ev = StructureEvent(
            event_type="bos", symbol=sym, timeframe="M1", side="bullish",
            status="CONFIRMED", detected_at=_t(5), confirmed_at=_t(6),
            price=101.0, top=101.0, bottom=100.5, touches=0,
            provisional_touches=0, candle_closed=True, later_modified=False,
            event_id=eid, defining_candle_time=_t(5),
        )
        write_structure_event(ev)
        write_structure_event(ev)  # identical transition -> dedup no-op
        raw = _raw_ledger_records(sym)
        matching = [r for r in raw if r["transaction_id"] == f"{eid}:CONFIRMED"]
        assert len(matching) == 1

    def test_read_collapses_to_latest_record_per_event(self):
        sym = _unique_sym("CL")
        eid = f"{sym}|bos|bullish|t5"
        base = dict(
            event_type="bos", symbol=sym, timeframe="M1", side="bullish",
            price=101.0, top=101.0, bottom=100.5, touches=0,
            provisional_touches=0, later_modified=False, event_id=eid,
            defining_candle_time=_t(5),
        )
        forming = StructureEvent(**{**base, "status": "FORMING",
                                    "detected_at": _t(5), "confirmed_at": None,
                                    "candle_closed": False})
        confirmed = StructureEvent(**{**base, "status": "CONFIRMED",
                                      "detected_at": _t(5), "confirmed_at": _t(6),
                                      "candle_closed": True})
        write_structure_event(forming)
        write_structure_event(confirmed)
        records = read_structure_events(sym, limit=10)
        assert len(records) == 1
        assert records[0]["status"] == "CONFIRMED"
        assert records[0]["detected_at"] == _t(5)
        assert records[0]["confirmed_at"] == _t(6)

    def test_ledger_filters_by_symbol(self):
        sym_a, sym_b = _unique_sym("FA"), _unique_sym("FB")
        for sym in (sym_a, sym_b):
            ev = StructureEvent(
                event_type="fvg", symbol=sym, timeframe="M1", side="bullish",
                status="CONFIRMED", detected_at=_t(0), confirmed_at=_t(1),
                price=1.1, top=1.101, bottom=1.099, touches=0,
                provisional_touches=0, candle_closed=True, later_modified=False,
                event_id=f"{sym}|fvg|bullish|t0", defining_candle_time=_t(0),
            )
            write_structure_event(ev)
        a_records = read_structure_events(sym_a, limit=10)
        b_records = read_structure_events(sym_b, limit=10)
        assert a_records and b_records
        assert all(r["symbol"] == sym_a for r in a_records)
        assert all(r["symbol"] == sym_b for r in b_records)


# ===========================================================================
# 9. Loop wiring
# ===========================================================================


class TestM1StructureLoop:
    def test_loop_disabled_writes_disabled_doc(self, monkeypatch):
        from loops import m1_structure_loop as msl

        written = {}
        monkeypatch.setattr(msl, "load_config",
                            lambda: {"m1_structure": {"enabled": False}})
        monkeypatch.setattr(msl, "write_json_state",
                            lambda name, data: written.__setitem__(name, data))
        res = msl.run()
        assert res["enabled"] is False
        assert written["m1_structure_decisions.json"]["enabled"] is False

    def test_loop_no_m1_data_is_graceful(self, monkeypatch):
        from loops import m1_structure_loop as msl

        written = {}
        monkeypatch.setattr(msl, "load_config",
                            lambda: {"m1_structure": {"enabled": True}})
        monkeypatch.setattr(msl, "read_json_state",
                            lambda name, default=None: {"symbols": {"XAUUSDm": {"M5": []}}})
        monkeypatch.setattr(msl, "write_json_state",
                            lambda name, data: written.__setitem__(name, data))
        res = msl.run()
        assert res["reason"] == "no_m1_data"
        assert written["m1_structure_decisions.json"]["enabled"] is True

    def test_loop_processes_m1_bars_into_decisions(self, monkeypatch):
        """With M1 bars present (collector now feeds M1 when enabled), the
        loop writes per-symbol decisions to m1_structure_decisions.json."""
        from loops import m1_structure_loop as msl

        # Fresh bar times (last bar well inside max_data_age_seconds) so the
        # loop exercises a real decision path rather than the stale WAIT gate.
        m1_bars = [
            {"open": 100.0, "high": 100.4, "low": 99.7, "close": 100.2,
             "time": _fresh_iso(360)},
            {"open": 100.2, "high": 100.6, "low": 100.0, "close": 100.4,
             "time": _fresh_iso(300)},
            {"open": 100.4, "high": 100.9, "low": 100.2, "close": 100.7,
             "time": _fresh_iso(240)},
            {"open": 100.6, "high": 101.0, "low": 100.4, "close": 100.8,
             "time": _fresh_iso(180)},
            {"open": 100.8, "high": 101.3, "low": 100.6, "close": 101.1,
             "time": _fresh_iso(120)},
            {"open": 101.0, "high": 101.5, "low": 100.9, "close": 101.4,
             "time": _fresh_iso(60)},
        ]
        written = {}
        monkeypatch.setattr(msl, "load_config",
                            lambda: {"m1_structure": {"enabled": True}})
        monkeypatch.setattr(msl, "read_json_state",
                            lambda name, default=None: {"symbols": {"XAUUSDm": {"M1": m1_bars}}})
        monkeypatch.setattr(msl, "write_json_state",
                            lambda name, data: written.__setitem__(name, data))
        res = msl.run()
        assert res["enabled"] is True
        assert "XAUUSDm" in res["decisions"], res
        dec = res["decisions"]["XAUUSDm"]
        assert dec["state"] in ("WAIT", "BUY", "SELL")
        assert dec["symbol"] == "XAUUSDm"
        assert written["m1_structure_decisions.json"]["decisions"]["XAUUSDm"] == dec

    def test_collect_m1_bars_extracts_only_m1(self):
        from loops.m1_structure_loop import _collect_m1_bars

        doc = {
            "symbols": {
                "XAUUSDm": {
                    "M5": [{"open": 1, "high": 2, "low": 1, "close": 1.5, "time": "t"}],
                    "M1": [
                        {"open": 1, "high": 2, "low": 1, "close": 1.5, "time": "t0"},
                        {"open": 1, "high": 2, "low": 1, "close": 1.5, "time": "t1"},
                        {"open": 1, "high": 2, "low": 1, "close": 1.5, "time": "t2"},
                    ],
                }
            }
        }
        out = _collect_m1_bars(doc)
        assert set(out.keys()) == {"XAUUSDm"}
        assert len(out["XAUUSDm"]) == 3


# ===========================================================================
# 9b. M1 feed wiring (collector + payload validation)
# ===========================================================================


class TestFeedWiring:
    def test_validate_payload_accepts_m1_when_enabled(self):
        # loops.data_loop transitively imports pandas (core.history_manager),
        # which is not installed in this sandbox — skip here, run where the
        # production env (Windows/MT5) has it.
        pytest.importorskip("pandas")
        from loops.data_loop import _validate_payload

        data = {
            "timestamp": "t", "source": "mt5", "symbol_map": {}, "account": {},
            "symbols": {"XAUUSDm": {"broker_symbol": "XAUUSDm",
                                     "M5": [], "M15": [], "M1": []}},
        }
        _validate_payload(data, "M5", "M15", m1_tf="M1")  # must not raise

    def test_validate_payload_tolerates_missing_m1_when_enabled(self):
        """M1 is shadow/optional — its absence must not fail the payload."""
        pytest.importorskip("pandas")
        from loops.data_loop import _validate_payload

        data = {
            "timestamp": "t", "source": "mt5", "symbol_map": {}, "account": {},
            "symbols": {"XAUUSDm": {"broker_symbol": "XAUUSDm",
                                     "M5": [], "M15": []}},
        }
        _validate_payload(data, "M5", "M15", m1_tf="M1")  # must not raise

    def test_validate_payload_ignores_m1_when_disabled(self):
        pytest.importorskip("pandas")
        from loops.data_loop import _validate_payload

        data = {
            "timestamp": "t", "source": "mt5", "symbol_map": {}, "account": {},
            "symbols": {"XAUUSDm": {"broker_symbol": "XAUUSDm",
                                     "M5": [], "M15": []}},
        }
        _validate_payload(data, "M5", "M15", m1_tf=None)  # must not raise

    def test_pull_latest_fetches_m1_only_when_enabled(self):
        from core.data_collector import DataCollector

        class _Conn:
            connected = True

            def account_snapshot(self):
                return {"login": 1, "mode": "demo", "server": "s"}

        class _Symbols:
            symbol_map = {"XAUUSDm": "XAUUSDm"}

        def _make(enabled: bool):
            cfg = {
                "mt5": {"timeframes": {"entry": "M5", "bias": "M15"},
                        "candles": 300},
                "m1_structure": {"enabled": enabled},
            }
            collector = DataCollector(cfg, _Conn(), _Symbols(), None)
            seen: list[str] = []
            collector.fetch_candles = lambda sym, tf, count: (seen.append(tf), [])[1]
            data = collector.pull_latest()
            return seen, data

        seen_off, data_off = _make(False)
        assert seen_off == ["M5", "M15"]
        assert "M1" not in data_off["symbols"]["XAUUSDm"]

        seen_on, data_on = _make(True)
        assert seen_on == ["M5", "M15", "M1"]
        assert data_on["symbols"]["XAUUSDm"]["M1"] == []


# ===========================================================================
# 10. Edge cases
# ===========================================================================


class TestEdgeCases:
    def test_insufficient_bars_no_crash(self):
        eng = _engine()
        eng.update("T", [_bar(100, 101, 99, 100.5, _t(0))], 100.5, atr=1.0)
        assert eng.get_decision("T")["state"] == "WAIT"

    def test_zero_atr_no_crash(self):
        eng = _engine()
        eng.update("T", _bull_swings(), 105.0, atr=0.0)
        assert eng.get_decision("T")["state"] == "WAIT"

    def test_get_events_empty_symbol(self):
        eng = _engine()
        assert eng.get_events("UNKNOWN") == []

    def test_get_decision_empty_symbol(self):
        eng = _engine()
        assert eng.get_decision("UNKNOWN")["state"] == "WAIT"
