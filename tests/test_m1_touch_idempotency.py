"""M1 zone-touch idempotency tests (candle-identity accounting).

Proves that replaying the same stored candle snapshot can never inflate
confirmed or provisional touch counts:

  1. same_snapshot_twice_does_not_increment_touch
  2. same_closed_touching_candle_replayed_is_counted_once
  3. new_unique_touching_candle_increments_once
  4. forming_touch_replayed_remains_one_provisional_touch
  5. forming_to_closed_converts_touch_exactly_once
  6. stale_snapshot_does_not_mutate_touch_state
  7. restart_restore_does_not_duplicate_touch
  8. last_touch_time_changes_only_for_new_touching_candle
  9. multiple_different_touching_candles_each_count_once
  10. old_ledger_without_touch_ids_loads_safely

Background defect (Windows smoke, XAUUSDm bearish OB): the zone ran from
19:31 to 19:51 — roughly 21 unique M1 candle timestamps — yet
touches_confirmed reached 100 because the engine deduplicated against a
single ``last_touch_time`` slot: every service cycle replayed the same
historical snapshot and re-counted every in-zone candle except the last.

The fix: every confirmed touch is an identity (event_id + candle timestamp)
kept in ``event.touch_times`` and persisted to the immutable ledger as
``<event_id>:TOUCH:<candle time>`` records. ``touches`` is always
``len(touch_times)``, so replay is a no-op and legacy records without the
field load safely.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure core is importable
SYS_PATH = str(Path(__file__).resolve().parents[1])
if SYS_PATH not in sys.path:
    sys.path.insert(0, SYS_PATH)

from core.m1_structure_engine import (
    LEDGER_NAME,
    M1StructureEngine,
    StructureEvent,
    read_structure_events,
    write_structure_event,
)
from core.utils import STATE_DIR, append_archive_record

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COUNTER = {"n": 0}

# Captured once per test process (same convention as test_m1_structure_engine):
# _t(13) == import moment, _t(0) == 13 minutes earlier. Relative ordering is
# what matters; the values are deterministic within a process.
_ANCHOR = datetime.now(timezone.utc).replace(microsecond=0)


def _t(minute: int) -> str:
    """ISO bar time: minute 13 ~= import time, minute 0 = 13 minutes earlier."""
    return (_ANCHOR - timedelta(minutes=13 - minute)).isoformat()


def _bar(o: float, h: float, l: float, c: float, t: str) -> dict:
    """Create an M1 candle dict."""
    return {"open": o, "high": h, "low": l, "close": c, "time": t, "volume": 1000}


def _unique_sym(prefix: str) -> str:
    # The structure ledger is persistent on disk and idempotent per event_id,
    # so the symbol (and any event_id embedding it) must be unique ACROSS test
    # processes too — otherwise a rerun would collide with earlier records.
    _COUNTER["n"] += 1
    return f"{prefix}{_COUNTER['n']}_{time.time_ns()}"


def _engine(config: dict | None = None, *, restore: bool = False) -> M1StructureEngine:
    if config is None:
        config = {"m1_structure": {"enabled": True}}
    return M1StructureEngine(config, restore_from_ledger=restore)


def _confirmed_zone(sym: str, eid: str, defining: str,
                    top: float = 100.9, bottom: float = 100.5) -> StructureEvent:
    """A CONFIRMED bullish FVG zone [bottom, top] whose defining candle closed."""
    return StructureEvent(
        event_type="fvg", symbol=sym, timeframe="M1", side="bullish",
        status="CONFIRMED", detected_at=_t(4), confirmed_at=_t(5),
        price=(top + bottom) / 2, top=top, bottom=bottom, touches=0,
        provisional_touches=0, candle_closed=True, later_modified=False,
        event_id=eid, defining_candle_time=defining,
    )


def _forming_zone(sym: str, eid: str, defining: str,
                  top: float = 100.9, bottom: float = 100.5) -> StructureEvent:
    """A FORMING (unconfirmed) bullish FVG zone with the same geometry."""
    return StructureEvent(
        event_type="fvg", symbol=sym, timeframe="M1", side="bullish",
        status="FORMING", detected_at=_t(4), confirmed_at=None,
        price=(top + bottom) / 2, top=top, bottom=bottom, touches=0,
        provisional_touches=0, candle_closed=False, later_modified=False,
        event_id=eid, defining_candle_time=defining,
    )


def _raw_ledger_records(symbol: str) -> list[dict]:
    """Read the raw (uncollapsed) ledger lines for a symbol."""
    path = STATE_DIR / f"{LEDGER_NAME}.jsonl"
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
# 1-2. Replaying the same snapshot / same closed candle never re-counts
# ===========================================================================


class TestReplayIdempotency:
    def test_same_snapshot_twice_does_not_increment_touch(self):
        """Replaying the exact same bar snapshot must not move the count."""
        sym = _unique_sym("RPT")
        eid = f"{sym}|fvg|bullish|t5"
        ev = _confirmed_zone(sym, eid, _t(5))
        eng = _engine()
        eng._events[sym] = [ev]
        bars = [
            _bar(100.2, 100.8, 100.1, 100.5, _t(6)),   # closed, in zone
            _bar(100.3, 100.9, 100.2, 100.6, _t(7)),   # closed, in zone
            _bar(100.4, 101.0, 100.3, 100.7, _t(8)),   # current/forming, in zone
        ]
        eng.update(sym, bars, 100.7, atr=1.0)
        assert ev.touches == 2
        # Same stored snapshot re-evaluated by the M1 service every cycle.
        for _ in range(5):
            eng.update(sym, list(bars), 100.7, atr=1.0)
        assert ev.touches == 2, ev.touches
        assert ev.touch_times == [_t(6), _t(7)]

    def test_same_closed_touching_candle_replayed_is_counted_once(self):
        """Each closed touching candle is counted once, no matter how many
        times the unchanged snapshot is reprocessed."""
        sym = _unique_sym("ONCE")
        eid = f"{sym}|fvg|bullish|t5"
        ev = _confirmed_zone(sym, eid, _t(5))
        eng = _engine()
        eng._events[sym] = [ev]
        bars = [
            _bar(100.2, 100.8, 100.1, 100.5, _t(6)),   # closed, in zone
            _bar(100.3, 100.9, 100.2, 100.6, _t(7)),   # closed, in zone
            _bar(100.4, 101.0, 100.3, 100.7, _t(8)),   # current/forming, in zone
        ]
        for _ in range(8):
            eng.update(sym, list(bars), 100.7, atr=1.0)
        assert ev.touches == 2, ev.touches
        assert len(ev.touch_times) == 2
        assert len(set(ev.touch_times)) == 2


# ===========================================================================
# 3. A genuinely new touching candle increments exactly once
# ===========================================================================


class TestNewTouchingCandle:
    def test_new_unique_touching_candle_increments_once(self):
        sym = _unique_sym("NEW")
        eid = f"{sym}|fvg|bullish|t5"
        ev = _confirmed_zone(sym, eid, _t(5))
        eng = _engine()
        eng._events[sym] = [ev]
        bars = [
            _bar(100.2, 100.8, 100.1, 100.5, _t(6)),   # closed, in zone
            _bar(100.3, 100.9, 100.2, 100.6, _t(7)),   # closed, in zone
            _bar(100.4, 101.0, 100.3, 100.7, _t(8)),   # current/forming, in zone
        ]
        eng.update(sym, bars, 100.7, atr=1.0)
        assert ev.touches == 2
        # A NEW closed touching candle arrives (t8 closes, t9 is current).
        bars2 = bars + [
            _bar(100.4, 101.0, 100.3, 100.8, _t(8)),   # now closed, in zone
            _bar(101.0, 101.4, 100.9, 101.2, _t(9)),   # current, above zone
        ]
        eng.update(sym, bars2, 101.2, atr=1.0)
        assert ev.touches == 3, ev.touches
        assert ev.touch_times == [_t(6), _t(7), _t(8)]
        # Replaying the advanced snapshot must not double-count t8.
        for _ in range(4):
            eng.update(sym, list(bars2), 101.2, atr=1.0)
        assert ev.touches == 3, ev.touches


# ===========================================================================
# 4-5. Provisional touches: never accumulate, convert exactly once on close
# ===========================================================================


class TestProvisionalLifecycle:
    def test_forming_touch_replayed_remains_one_provisional_touch(self):
        """A forming candle in the zone shows exactly one provisional touch —
        replaying the same snapshot never pushes it higher."""
        sym = _unique_sym("PRV")
        eid = f"{sym}|fvg|bullish|t8"
        ev = _forming_zone(sym, eid, _t(8))  # defining candle in the future
        eng = _engine()
        eng._events[sym] = [ev]
        bars = [
            _bar(100.2, 100.8, 100.1, 100.5, _t(6)),
            _bar(100.3, 100.9, 100.2, 100.6, _t(7)),
        ]
        for _ in range(4):
            eng.update(sym, bars, 100.6, atr=1.0)
        assert ev.status == "FORMING"
        assert ev.touches == 0, "forming zones never accumulate confirmed touches"
        assert ev.touch_times == []
        assert ev.provisional_touches == 1, ev.provisional_touches

    def test_confirmed_zone_current_candle_provisional_stays_one(self):
        """The current/forming candle of a CONFIRMED zone stays a single
        provisional touch across replays (never climbs)."""
        sym = _unique_sym("PRV2")
        eid = f"{sym}|fvg|bullish|t5"
        ev = _confirmed_zone(sym, eid, _t(5))
        eng = _engine()
        eng._events[sym] = [ev]
        bars = [
            _bar(100.2, 100.8, 100.1, 100.5, _t(6)),   # closed, in zone
            _bar(100.3, 100.9, 100.2, 100.6, _t(7)),   # current/forming, in zone
        ]
        for _ in range(4):
            eng.update(sym, bars, 100.6, atr=1.0)
        assert ev.touches == 1, "closed candle t6 counted once"
        assert ev.provisional_touches == 1, ev.provisional_touches

    def test_forming_to_closed_converts_touch_exactly_once(self):
        """A forming touching candle is provisional only while open; when it
        closes it converts to exactly ONE confirmed touch — never two."""
        sym = _unique_sym("CVT")
        eid = f"{sym}|fvg|bullish|t5"
        ev = _confirmed_zone(sym, eid, _t(5))
        eng = _engine()
        eng._events[sym] = [ev]
        # Candle t6 is the current/forming candle, in the zone.
        bars = [_bar(100.2, 100.8, 100.1, 100.7, _t(6))]
        eng.update(sym, bars, 100.7, atr=1.0)
        assert ev.touches == 0
        assert ev.provisional_touches == 1  # forming interaction only
        # t6 closes in the zone; t7 is the new current candle (above zone).
        bars2 = [
            _bar(100.2, 100.8, 100.1, 100.5, _t(6)),   # now CLOSED, in zone
            _bar(100.6, 101.1, 100.5, 101.2, _t(7)),   # current, above zone
        ]
        eng.update(sym, bars2, 101.2, atr=1.0)
        assert ev.touches == 1, ev.touches
        assert ev.touch_times == [_t(6)]
        assert ev.provisional_touches == 0  # current candle is outside the zone
        # Replay the closed snapshot — the conversion must not repeat.
        for _ in range(4):
            eng.update(sym, list(bars2), 101.2, atr=1.0)
        assert ev.touches == 1, ev.touches
        assert ev.touch_times == [_t(6)]


# ===========================================================================
# 6. Stale / unchanged snapshots never mutate touch state
# ===========================================================================


class TestStaleSnapshotNoMutation:
    def test_stale_snapshot_does_not_mutate_touch_state(self):
        sym = _unique_sym("STL")
        eid = f"{sym}|fvg|bullish|t5"
        ev = _confirmed_zone(sym, eid, _t(5))
        eng = _engine()
        eng._events[sym] = [ev]
        bars = [
            _bar(100.2, 100.8, 100.1, 100.5, _t(6)),   # closed, in zone
            _bar(100.3, 100.9, 100.2, 100.6, _t(7)),   # closed, in zone
            _bar(100.4, 101.0, 100.3, 100.7, _t(8)),   # current/forming, in zone
        ]
        eng.update(sym, bars, 100.7, atr=1.0)
        snapshot = (
            ev.touches,
            ev.provisional_touches,
            ev.metadata.get("last_touch_time"),
            list(ev.touch_times),
        )
        assert snapshot[0] == 2
        # Re-evaluate the identical stored snapshot repeatedly.
        for _ in range(5):
            eng.update(sym, list(bars), 100.7, atr=1.0)
        assert (ev.touches, ev.provisional_touches,
                ev.metadata.get("last_touch_time"), list(ev.touch_times)) == snapshot


# ===========================================================================
# 7. Restart / ledger restoration preserves touch identities
# ===========================================================================


class TestRestartRestore:
    def test_restart_restore_does_not_duplicate_touch(self):
        """A fresh engine restoring from the immutable ledger keeps the
        already-counted touch identities and never re-counts them."""
        sym = _unique_sym("RST")
        # The engine derives event_id from the ACTUAL defining candle time.
        eid = f"{sym}|fvg|bullish|{_t(5)}"
        eng1 = _engine()
        # Full lifecycle through the engine: create (FORMING), confirm, touch.
        bars1 = [
            _bar(100.0, 100.5, 99.8, 100.2, _t(3)),
            _bar(100.2, 100.7, 100.1, 100.5, _t(4)),
            _bar(101.0, 101.5, 100.9, 101.2, _t(5)),   # defining candle: FVG
        ]
        eng1.update(sym, bars1, 101.2, atr=1.0)        # create (FORMING)
        bars2 = bars1 + [_bar(101.1, 101.4, 100.9, 101.3, _t(6))]
        eng1.update(sym, bars2, 101.3, atr=1.0)        # confirm (CONFIRMED)
        bars3 = bars2 + [
            _bar(100.5, 101.0, 100.4, 100.7, _t(7)),   # closed, in zone
            _bar(100.6, 101.0, 100.5, 100.8, _t(8)),   # closed, in zone
            _bar(100.8, 101.2, 100.7, 101.0, _t(9)),   # current
        ]
        eng1.update(sym, bars3, 101.0, atr=1.0)        # touches counted
        events1 = [e for e in eng1._events[sym] if e.event_id == eid]
        assert events1 and events1[0].touches == 2, events1

        # "Restart": a fresh engine restores state from the ledger.
        eng2 = _engine(restore=True)
        restored = [e for e in eng2._events.get(sym, []) if e.event_id == eid]
        assert len(restored) == 1, "ledger must restore exactly one record per event"
        ev = restored[0]
        assert ev.status == "CONFIRMED"
        assert ev.touches == 2, ev.touches
        assert ev.touch_times == [_t(7), _t(8)], ev.touch_times

        # Reprocessing the same snapshot after restore: no duplication.
        eng2.update(sym, list(bars3), 101.0, atr=1.0)
        assert ev.touches == 2, ev.touches
        assert ev.touch_times == [_t(7), _t(8)]

    def test_touch_records_persist_idempotently_in_ledger(self):
        """Each touch writes exactly one <event_id>:TOUCH:<candle time> record;
        replaying the same snapshot writes nothing new."""
        sym = _unique_sym("LED")
        eid = f"{sym}|fvg|bullish|t5"
        ev = _confirmed_zone(sym, eid, _t(5))
        eng = _engine()
        eng._events[sym] = [ev]
        bars = [
            _bar(100.2, 100.8, 100.1, 100.5, _t(6)),   # closed, in zone
            _bar(100.3, 100.9, 100.2, 100.6, _t(7)),   # closed, in zone
            _bar(100.4, 101.0, 100.3, 100.7, _t(8)),   # current/forming, in zone
        ]
        eng.update(sym, bars, 100.7, atr=1.0)
        for _ in range(3):
            eng.update(sym, list(bars), 100.7, atr=1.0)
        touch_records = [r for r in _raw_ledger_records(sym)
                         if str(r.get("transition")) == "TOUCH"]
        tx_ids = {r["transaction_id"] for r in touch_records}
        assert len(touch_records) == 2, len(touch_records)
        assert tx_ids == {f"{eid}:TOUCH:{_t(6)}", f"{eid}:TOUCH:{_t(7)}"}
        # Every touch record carries the identity state for restore.
        for r in touch_records:
            assert r["touch_times"] == [_t(6), _t(7)] or r["touch_times"] == [_t(6)]
            assert r["touches"] == len(r["touch_times"])


# ===========================================================================
# 8. last_touch_time only moves for a genuinely new touching candle
# ===========================================================================


class TestLastTouchTime:
    def test_last_touch_time_changes_only_for_new_touching_candle(self):
        sym = _unique_sym("LTT")
        eid = f"{sym}|fvg|bullish|t5"
        ev = _confirmed_zone(sym, eid, _t(5))
        eng = _engine()
        eng._events[sym] = [ev]
        bars = [
            _bar(100.2, 100.8, 100.1, 100.5, _t(6)),   # closed, in zone
            _bar(100.4, 101.0, 100.3, 100.7, _t(7)),   # current, in zone
        ]
        eng.update(sym, bars, 100.7, atr=1.0)
        assert ev.metadata.get("last_touch_time") == _t(6)
        # Replays must not advance last_touch_time.
        for _ in range(3):
            eng.update(sym, list(bars), 100.7, atr=1.0)
        assert ev.metadata.get("last_touch_time") == _t(6)
        # A genuinely new touching candle advances it exactly once.
        bars2 = [
            _bar(100.2, 100.8, 100.1, 100.5, _t(6)),   # closed, in zone
            _bar(100.4, 101.0, 100.3, 100.7, _t(7)),   # closed, in zone
            _bar(100.5, 101.0, 100.4, 100.8, _t(8)),   # closed, in zone
            _bar(101.0, 101.4, 100.9, 101.2, _t(9)),   # current, above zone
        ]
        eng.update(sym, bars2, 101.2, atr=1.0)
        assert ev.metadata.get("last_touch_time") == _t(8)
        assert ev.touches == 3
        for _ in range(3):
            eng.update(sym, list(bars2), 101.2, atr=1.0)
        assert ev.metadata.get("last_touch_time") == _t(8)


# ===========================================================================
# 9. Many distinct touching candles each count exactly once
# ===========================================================================


class TestManyTouchingCandles:
    def test_multiple_different_touching_candles_each_count_once(self):
        """The Windows defect: 21 unique candles must never produce 100
        touches. Ten distinct touching candles count exactly ten, forever."""
        sym = _unique_sym("MANY")
        eid = f"{sym}|fvg|bullish|t5"
        ev = _confirmed_zone(sym, eid, _t(5))
        eng = _engine()
        eng._events[sym] = [ev]
        bars = []
        for i in range(6, 16):                       # t6..t15: 10 closed candles
            bars.append(_bar(100.2, 100.9, 100.1, 100.5 + i / 100.0, _t(i)))
        bars.append(_bar(101.0, 101.4, 100.9, 101.2, _t(16)))  # current, above zone
        eng.update(sym, bars, 101.2, atr=1.0)
        assert ev.touches == 10, ev.touches
        assert len(ev.touch_times) == 10
        assert len(set(ev.touch_times)) == 10
        # Same stored snapshot re-evaluated like the dedicated M1 service does.
        for _ in range(5):
            eng.update(sym, list(bars), 101.2, atr=1.0)
        assert ev.touches == 10, ev.touches
        assert len(ev.touch_times) == 10
        # The compressed decision must report the same (non-inflated) count.
        dec = eng.get_decision(sym)
        assert dec["touches_confirmed"] == 10, dec["touches_confirmed"]


# ===========================================================================
# 10. Legacy ledger records without touch identities load safely
# ===========================================================================


class TestLegacyLedger:
    def test_old_ledger_without_touch_ids_loads_safely(self):
        """A pre-fix ledger record (no touch_times key, possibly an inflated
        touches count) must restore without crashing and self-heal to the true
        count from candle history."""
        sym = _unique_sym("LEG")
        eid = f"{sym}|fvg|bullish|t5"
        legacy = StructureEvent(
            event_type="fvg", symbol=sym, timeframe="M1", side="bullish",
            status="CONFIRMED", detected_at=_t(4), confirmed_at=_t(5),
            price=100.7, top=100.9, bottom=100.5, touches=100,
            provisional_touches=0, candle_closed=True, later_modified=False,
            event_id=eid, defining_candle_time=_t(5),
            metadata={"last_touch_time": _t(8)},   # legacy stale marker
        )
        raw = legacy.to_dict()
        raw.pop("touch_times", None)  # simulate a pre-fix ledger line
        # from_dict must load the legacy shape safely.
        loaded = StructureEvent.from_dict(raw)
        assert loaded is not None
        assert loaded.status == "CONFIRMED"
        assert loaded.touches == 100, "legacy stored count preserved on load"
        assert loaded.touch_times == []
        # Write the legacy record exactly as an old version would have.
        raw["transaction_id"] = f"{eid}:CONFIRMED"
        append_archive_record(LEDGER_NAME, raw)

        # Restart: the legacy record restores safely.
        eng = _engine(restore=True)
        restored = [e for e in eng._events.get(sym, []) if e.event_id == eid]
        assert len(restored) == 1
        ev = restored[0]
        assert ev.status == "CONFIRMED"
        assert ev.touch_times == []

        # The next pass self-heals the inflated count from real candle history.
        bars = [
            _bar(100.2, 100.8, 100.1, 100.5, _t(6)),   # closed, in zone
            _bar(100.3, 100.9, 100.2, 100.6, _t(7)),   # closed, in zone
            _bar(101.0, 101.4, 100.9, 101.2, _t(8)),   # current, above zone
        ]
        eng.update(sym, bars, 101.2, atr=1.0)
        assert ev.touches == 2, ev.touches
        assert ev.touch_times == [_t(6), _t(7)]

    def test_event_dict_round_trips_touch_times(self):
        """touch_times survives to_dict -> from_dict exactly."""
        sym = _unique_sym("RT")
        eid = f"{sym}|fvg|bullish|t5"
        ev = _confirmed_zone(sym, eid, _t(5))
        ev.touch_times = [_t(6), _t(7)]
        ev.touches = 2
        clone = StructureEvent.from_dict(ev.to_dict())
        assert clone is not None
        assert clone.touch_times == [_t(6), _t(7)]
        assert clone.touches == 2
        assert clone.status == "CONFIRMED"


# ===========================================================================
# Existing read path still collapses touch records per event
# ===========================================================================


class TestLedgerReadPath:
    def test_read_collapses_touch_records_to_latest_event_state(self):
        sym = _unique_sym("RD")
        eid = f"{sym}|fvg|bullish|t5"
        ev = _confirmed_zone(sym, eid, _t(5))
        eng = _engine()
        eng._events[sym] = [ev]
        bars = [
            _bar(100.2, 100.8, 100.1, 100.5, _t(6)),   # closed, in zone
            _bar(100.3, 100.9, 100.2, 100.6, _t(7)),   # closed, in zone
            _bar(100.4, 101.0, 100.3, 100.7, _t(8)),   # current/forming, in zone
        ]
        eng.update(sym, bars, 100.7, atr=1.0)
        records = read_structure_events(sym, limit=10)
        assert len(records) == 1, "touch records collapse to one latest state"
        assert records[0]["event_id"] == eid
        assert records[0]["touches"] == 2
        assert records[0]["touch_times"] == [_t(6), _t(7)]
