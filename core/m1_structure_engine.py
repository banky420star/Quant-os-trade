"""M1 Smart-Money Structure Engine — ICT/SMC structure detection on M1.

Detects Break of Structure (BOS), Change of Character (CHoCH), Fair Value Gaps
(FVG), and Order Blocks (OB) on M1 candles with a provisional/confirmed state
machine. Every lifecycle transition (create -> confirm -> invalidate) is
appended to an immutable, idempotent JSONL ledger.

Design principles (Phase 0 + M1 correctness closure):
  - An event is tagged FORMING at detection and stays FORMING until a bar with
    a timestamp strictly newer than its DEFINING candle arrives. The defining
    candle time is recorded on every event, so no execution logic can treat a
    forming M1 candle as closed.
  - Each event has a deterministic ``event_id`` (symbol|type|side|defining
    candle time), which makes re-detection idempotent across pipeline cycles
    and lets the same structure type/side fire again at a later candle.
  - Touches are counted only for CLOSED candles strictly after the defining
    candle, deduplicated per bar time. Current-candle interaction is reported
    separately as a provisional touch.
  - Invalidated zones get status ``INVALIDATED`` and can never trigger again.
  - The compressed decision stays WAIT until a full confirmation chain holds:
    2+ confirmed structures on one side, the most recent confirmation on that
    side, and a close beyond the newest structure level (zone hold + reclaim).
  - ``data_age_seconds`` is derived from the last market bar timestamp (not the
    wall-clock time of the update), and the candle-close countdown is computed
    from the current UTC clock vs the next minute boundary.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.utils import STATE_DIR, utc_now_iso

_LOG = logging.getLogger("m1_structure")

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _ts_epoch(value: Any) -> float:
    """Normalize a bar time / ISO timestamp to epoch seconds (0 when unknown)."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0
    return 0.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class StructureEvent:
    """One detected structure event (BOS, CHoCH, FVG, or OB)."""

    event_type: str  # "bos", "choch", "fvg", "order_block"
    symbol: str
    timeframe: str  # always "M1"
    side: str  # "bullish" or "bearish"
    status: str  # "FORMING", "CONFIRMED", or "INVALIDATED"
    detected_at: str  # ISO timestamp when first detected (never changes)
    confirmed_at: str | None  # ISO timestamp when defining candle closed
    price: float  # price at detection
    top: float  # upper boundary of zone
    bottom: float  # lower boundary of zone
    touches: int  # confirmed touches (closed candles after defining candle)
    provisional_touches: int  # current-candle interactions (not confirmed)
    candle_closed: bool  # True once a bar newer than the defining candle exists
    later_modified: bool  # True when the event was invalidated
    metadata: dict[str, Any] = field(default_factory=dict)
    # --- correctness-closure fields ---
    event_id: str = ""  # deterministic: symbol|type|side|defining_candle_time
    defining_candle_time: Any = None  # bar time of the candle that formed it
    invalidated_at: str | None = None  # ISO timestamp when invalidated

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side,
            "status": self.status,
            "detected_at": self.detected_at,
            "confirmed_at": self.confirmed_at,
            "invalidated_at": self.invalidated_at,
            "price": self.price,
            "top": self.top,
            "bottom": self.bottom,
            "touches": self.touches,
            "provisional_touches": self.provisional_touches,
            "candle_closed": self.candle_closed,
            "later_modified": self.later_modified,
            "defining_candle_time": self.defining_candle_time,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, rec: dict[str, Any]) -> "StructureEvent | None":
        """Rehydrate an event from a ledger record (latest state per event_id)."""
        required = ("event_type", "symbol", "side", "status", "detected_at")
        if not all(k in rec for k in required):
            return None
        try:
            return cls(
                event_type=str(rec["event_type"]),
                symbol=str(rec["symbol"]),
                timeframe=str(rec.get("timeframe", "M1")),
                side=str(rec["side"]),
                status=str(rec["status"]),
                detected_at=str(rec["detected_at"]),
                confirmed_at=rec.get("confirmed_at"),
                invalidated_at=rec.get("invalidated_at"),
                price=float(rec.get("price", 0.0)),
                top=float(rec.get("top", 0.0)),
                bottom=float(rec.get("bottom", 0.0)),
                touches=int(rec.get("touches", 0)),
                provisional_touches=int(rec.get("provisional_touches", 0)),
                candle_closed=bool(rec.get("candle_closed", False)),
                later_modified=bool(rec.get("later_modified", False)),
                metadata=dict(rec.get("metadata") or {}),
                event_id=str(rec.get("event_id", "")),
                defining_candle_time=rec.get("defining_candle_time"),
            )
        except (TypeError, ValueError):
            return None


@dataclass
class DecisionState:
    """Compressed one-card decision for a symbol."""

    symbol: str
    state: str  # "WAIT", "BUY", "SELL"
    bias: str  # "Bullish", "Bearish", "Mixed", "Neutral"
    active_zone: str | None  # description of the active zone
    zone_type: str | None  # "BOS", "CHoCH", "FVG", "OB"
    zone_age_minutes: float  # how long the zone has been active
    touches_confirmed: int
    touches_provisional: int
    price_location: str  # "above_zone", "in_zone", "below_zone", "unknown"
    bull_trigger: str  # what would confirm bullish
    bear_trigger: str  # what would confirm bearish
    candle_close_seconds: int  # seconds until M1 candle closes (UTC clock)
    data_age_seconds: float  # age of the last market bar (not the update call)
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "state": self.state,
            "bias": self.bias,
            "active_zone": self.active_zone,
            "zone_type": self.zone_type,
            "zone_age_minutes": round(self.zone_age_minutes, 1),
            "touches_confirmed": self.touches_confirmed,
            "touches_provisional": self.touches_provisional,
            "price_location": self.price_location,
            "bull_trigger": self.bull_trigger,
            "bear_trigger": self.bear_trigger,
            "candle_close_seconds": self.candle_close_seconds,
            "data_age_seconds": round(self.data_age_seconds, 1),
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# M1 Structure Engine
# ---------------------------------------------------------------------------


class M1StructureEngine:
    """Detects ICT/SMC structure on M1 with provisional/confirmed states."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        restore_from_ledger: bool = False,
    ) -> None:
        cfg = config or {}
        m1_cfg = cfg.get("m1_structure", {}) or {}
        self.enabled = bool(m1_cfg.get("enabled", False))
        self.lookback_bars = int(m1_cfg.get("lookback_bars", 50))
        self.min_displacement_atr = float(m1_cfg.get("min_displacement_atr", 0.5))
        self.min_fvg_atr = float(m1_cfg.get("min_fvg_atr", 0.25))
        self.ob_lookback = int(m1_cfg.get("ob_lookback", 20))
        # Per-symbol state: {symbol: [StructureEvent, ...]} in detection order.
        self._events: dict[str, list[StructureEvent]] = {}
        # Per-symbol decision state
        self._decisions: dict[str, DecisionState] = {}
        # Restore the last known state per event from the immutable ledger so
        # the shadow loop is forward-only across pipeline cycles (detections
        # are idempotent; touches/confirmations are not replayed).
        if restore_from_ledger:
            self._restore_from_ledger()

    def update(
        self,
        symbol: str,
        m1_bars: list[dict[str, Any]],
        current_price: float,
        atr: float,
    ) -> DecisionState:
        """Process M1 bars and return the compressed decision state.

        Call this on every M1 tick/close. The engine will:
        1. Detect new structure events (provisional, FORMING)
        2. Confirm events whose defining candle has closed (newer bar exists)
        3. Update touches on confirmed zones (closed candles only)
        4. Invalidate decisively broken zones
        5. Compress into one WAIT/BUY/SELL decision
        """
        if not self.enabled:
            return self._empty_decision(symbol)

        if symbol not in self._events:
            self._events[symbol] = []

        # Process bars
        self._detect_bos_choch(symbol, m1_bars, current_price, atr)
        self._detect_fvg(symbol, m1_bars, atr)
        self._detect_order_blocks(symbol, m1_bars, atr)
        self._confirm_events(symbol, m1_bars)
        self._update_touches(symbol, m1_bars, current_price)
        self._invalidate_events(symbol, m1_bars, current_price, atr)

        # Compress into decision
        decision = self._compress_decision(symbol, current_price, m1_bars)
        self._decisions[symbol] = decision
        return decision

    def get_events(self, symbol: str) -> list[dict[str, Any]]:
        """Return all active (non-invalidated) events for a symbol."""
        events = self._events.get(symbol, [])
        return [e.to_dict() for e in events if not e.later_modified]

    def get_decision(self, symbol: str) -> dict[str, Any]:
        """Return the compressed decision state for a symbol."""
        decision = self._decisions.get(symbol)
        if decision:
            return decision.to_dict()
        return self._empty_decision(symbol).to_dict()

    def get_all_decisions(self) -> dict[str, dict[str, Any]]:
        """Return decisions for all tracked symbols."""
        return {sym: self.get_decision(sym) for sym in self._decisions}

    # ------------------------------------------------------------------
    # State restore
    # ------------------------------------------------------------------

    def _restore_from_ledger(self) -> None:
        """Seed per-symbol event state from the latest ledger record per event."""
        try:
            records = read_structure_events(limit=5000)
            for rec in reversed(records):  # oldest-first so lists stay ordered
                event = StructureEvent.from_dict(rec)
                if event is None:
                    continue
                self._events.setdefault(event.symbol, []).append(event)
        except Exception as exc:  # noqa: BLE001 — shadow state must not crash
            _LOG.warning("M1 structure ledger restore failed: %s", exc)

    # ------------------------------------------------------------------
    # Detection: BOS and CHoCH
    # ------------------------------------------------------------------

    def _detect_bos_choch(
        self,
        symbol: str,
        bars: list[dict],
        current_price: float,
        atr: float,
    ) -> None:
        """Detect Break of Structure and Change of Character on M1.

        BOS: Price breaks a recent swing high/low in the direction of the trend.
        CHoCH: Price breaks a recent swing high/low AGAINST the trend.
        """
        if len(bars) < 5:
            return

        events = self._events[symbol]
        now_iso = utc_now_iso()
        defining_time = bars[-1].get("time")

        # Find recent swing highs and lows (using last N bars). The window
        # keeps the FULL bar list (no -1 shift) so swing points near the start
        # of the window are not silently excluded from structure detection.
        lookback = min(self.lookback_bars, len(bars))
        window = bars[-lookback:]

        swing_highs = []
        swing_lows = []
        for i in range(2, len(window) - 2):
            bar = window[i]
            prev1 = window[i - 1]
            prev2 = window[i - 2]
            next1 = window[i + 1]
            next2 = window[i + 2]
            if (bar["high"] > prev1["high"] and bar["high"] > prev2["high"]
                    and bar["high"] > next1["high"] and bar["high"] > next2["high"]):
                swing_highs.append({"index": i, "price": bar["high"], "time": bar.get("time")})
            if (bar["low"] < prev1["low"] and bar["low"] < prev2["low"]
                    and bar["low"] < next1["low"] and bar["low"] < next2["low"]):
                swing_lows.append({"index": i, "price": bar["low"], "time": bar.get("time")})

        if not swing_highs and not swing_lows:
            return

        recent_highs = swing_highs[-3:] if len(swing_highs) >= 3 else swing_highs
        recent_lows = swing_lows[-3:] if len(swing_lows) >= 3 else swing_lows

        bullish_structure = False
        bearish_structure = False
        if len(recent_highs) >= 2 and recent_highs[-1]["price"] > recent_highs[-2]["price"]:
            bullish_structure = True
        if len(recent_lows) >= 2 and recent_lows[-1]["price"] < recent_lows[-2]["price"]:
            bearish_structure = True

        # BOS bullish — price broke above the last swing high in an uptrend
        if bullish_structure and swing_highs:
            last_high = swing_highs[-1]["price"]
            if current_price > last_high:
                self._add_event(events, StructureEvent(
                    event_type="bos", symbol=symbol, timeframe="M1", side="bullish",
                    status="FORMING", detected_at=now_iso, confirmed_at=None,
                    price=current_price, top=current_price, bottom=last_high,
                    touches=0, provisional_touches=0, candle_closed=False,
                    later_modified=False, defining_candle_time=defining_time,
                    metadata={"swing_high": last_high},
                ))

        # BOS bearish — price broke below the last swing low in a downtrend
        if bearish_structure and swing_lows:
            last_low = swing_lows[-1]["price"]
            if current_price < last_low:
                self._add_event(events, StructureEvent(
                    event_type="bos", symbol=symbol, timeframe="M1", side="bearish",
                    status="FORMING", detected_at=now_iso, confirmed_at=None,
                    price=current_price, top=last_low, bottom=current_price,
                    touches=0, provisional_touches=0, candle_closed=False,
                    later_modified=False, defining_candle_time=defining_time,
                    metadata={"swing_low": last_low},
                ))

        # CHoCH bearish — bullish structure broken
        if bullish_structure and swing_lows:
            last_low = swing_lows[-1]["price"]
            if current_price < last_low:
                self._add_event(events, StructureEvent(
                    event_type="choch", symbol=symbol, timeframe="M1", side="bearish",
                    status="FORMING", detected_at=now_iso, confirmed_at=None,
                    price=current_price, top=last_low, bottom=current_price,
                    touches=0, provisional_touches=0, candle_closed=False,
                    later_modified=False, defining_candle_time=defining_time,
                    metadata={"broken_structure": "bullish", "swing_low": last_low},
                ))

        # CHoCH bullish — bearish structure broken
        if bearish_structure and swing_highs:
            last_high = swing_highs[-1]["price"]
            if current_price > last_high:
                self._add_event(events, StructureEvent(
                    event_type="choch", symbol=symbol, timeframe="M1", side="bullish",
                    status="FORMING", detected_at=now_iso, confirmed_at=None,
                    price=current_price, top=current_price, bottom=last_high,
                    touches=0, provisional_touches=0, candle_closed=False,
                    later_modified=False, defining_candle_time=defining_time,
                    metadata={"broken_structure": "bearish", "swing_high": last_high},
                ))

    # ------------------------------------------------------------------
    # Detection: Fair Value Gap (FVG)
    # ------------------------------------------------------------------

    def _detect_fvg(self, symbol: str, bars: list[dict], atr: float) -> None:
        """Detect ICT/SMC Fair Value Gaps on M1 (3-bar structural imbalance)."""
        if len(bars) < 3 or atr <= 0:
            return

        events = self._events[symbol]
        now_iso = utc_now_iso()
        defining_time = bars[-1].get("time")

        bar0 = bars[-3]
        bar2 = bars[-1]

        # Bullish FVG: bar[i-2].high < bar[i].low (gap up)
        if bar0["high"] < bar2["low"]:
            gap = bar2["low"] - bar0["high"]
            gap_atr = gap / atr
            if gap_atr >= self.min_fvg_atr:
                self._add_event(events, StructureEvent(
                    event_type="fvg", symbol=symbol, timeframe="M1", side="bullish",
                    status="FORMING", detected_at=now_iso, confirmed_at=None,
                    price=bar2["close"], top=bar2["low"], bottom=bar0["high"],
                    touches=0, provisional_touches=0, candle_closed=False,
                    later_modified=False, defining_candle_time=defining_time,
                    metadata={"gap_size": gap, "gap_atr": round(gap_atr, 3)},
                ))

        # Bearish FVG: bar[i-2].low > bar[i].high (gap down)
        if bar0["low"] > bar2["high"]:
            gap = bar0["low"] - bar2["high"]
            gap_atr = gap / atr
            if gap_atr >= self.min_fvg_atr:
                self._add_event(events, StructureEvent(
                    event_type="fvg", symbol=symbol, timeframe="M1", side="bearish",
                    status="FORMING", detected_at=now_iso, confirmed_at=None,
                    price=bar2["close"], top=bar0["low"], bottom=bar2["high"],
                    touches=0, provisional_touches=0, candle_closed=False,
                    later_modified=False, defining_candle_time=defining_time,
                    metadata={"gap_size": gap, "gap_atr": round(gap_atr, 3)},
                ))

    # ------------------------------------------------------------------
    # Detection: Order Blocks (OB)
    # ------------------------------------------------------------------

    def _detect_order_blocks(self, symbol: str, bars: list[dict], atr: float) -> None:
        """Detect ICT/SMC Order Blocks on M1 (prior opposite-color candle + displacement)."""
        if len(bars) < 3 or atr <= 0:
            return

        events = self._events[symbol]
        now_iso = utc_now_iso()
        defining_time = bars[-1].get("time")

        bar0 = bars[-3]
        bar1 = bars[-2]
        bar2 = bars[-1]

        bar0_body = bar0["close"] - bar0["open"]
        bar1_body = bar1["close"] - bar1["open"]

        # Bullish OB: bearish candle followed by strong bullish displacement
        if bar0_body < 0 and bar1_body > 0:
            displacement = bar1_body / atr
            if displacement >= self.min_displacement_atr:
                self._add_event(events, StructureEvent(
                    event_type="order_block", symbol=symbol, timeframe="M1",
                    side="bullish", status="FORMING", detected_at=now_iso,
                    confirmed_at=None, price=bar2["close"], top=bar0["open"],
                    bottom=bar0["low"], touches=0, provisional_touches=0,
                    candle_closed=False, later_modified=False,
                    defining_candle_time=defining_time,
                    metadata={"displacement_atr": round(displacement, 3)},
                ))

        # Bearish OB: bullish candle followed by strong bearish displacement
        if bar0_body > 0 and bar1_body < 0:
            displacement = abs(bar1_body) / atr
            if displacement >= self.min_displacement_atr:
                self._add_event(events, StructureEvent(
                    event_type="order_block", symbol=symbol, timeframe="M1",
                    side="bearish", status="FORMING", detected_at=now_iso,
                    confirmed_at=None, price=bar2["close"], top=bar0["high"],
                    bottom=bar0["open"], touches=0, provisional_touches=0,
                    candle_closed=False, later_modified=False,
                    defining_candle_time=defining_time,
                    metadata={"displacement_atr": round(displacement, 3)},
                ))

    # ------------------------------------------------------------------
    # Confirmation: FORMING -> CONFIRMED only after the defining candle closes
    # ------------------------------------------------------------------

    def _confirm_events(self, symbol: str, bars: list[dict]) -> None:
        """Confirm FORMING events only when a bar newer than the defining
        candle exists (i.e. the candle that formed the event has closed)."""
        events = self._events[symbol]
        if not bars:
            return
        latest_epoch = _ts_epoch(bars[-1].get("time"))
        if latest_epoch <= 0:
            return
        now_iso = utc_now_iso()

        for event in events:
            if event.status != "FORMING":
                continue
            defining_epoch = _ts_epoch(event.defining_candle_time)
            if defining_epoch <= 0:
                # Cannot prove the defining candle closed — fail closed.
                continue
            if latest_epoch > defining_epoch:
                event.candle_closed = True
                event.status = "CONFIRMED"
                event.confirmed_at = now_iso
                write_structure_event(event)  # confirm transition -> ledger

    # ------------------------------------------------------------------
    # Touch counting
    # ------------------------------------------------------------------

    def _update_touches(
        self,
        symbol: str,
        bars: list[dict],
        current_price: float,
    ) -> None:
        """Update touch counts on active zones.

        Confirmed touches are only counted for CLOSED candles strictly after
        the defining candle, deduplicated per bar time. Current-candle
        interaction is tracked as a provisional touch instead.

        The M1 feed (MT5 copy_rates) contains only completed candles, so the
        last bar counts as closed; if a feed ever included the forming candle,
        its in-zone close would show as a provisional touch instead.
        """
        events = self._events[symbol]
        for event in events:
            if event.later_modified:
                continue

            event.provisional_touches = (
                1 if event.bottom <= current_price <= event.top else 0
            )

            if event.status != "CONFIRMED" or event.defining_candle_time is None:
                continue  # forming events never accumulate confirmed touches

            defining_epoch = _ts_epoch(event.defining_candle_time)
            if defining_epoch <= 0:
                continue
            for bar in bars:
                bar_epoch = _ts_epoch(bar.get("time"))
                if bar_epoch <= defining_epoch:
                    continue  # only candles strictly after the defining candle
                close = bar.get("close")
                if close is None:
                    continue
                if event.bottom <= close <= event.top:
                    bar_time = bar.get("time")
                    # Dedupe per bar time (read fresh each iteration).
                    if bar_time != event.metadata.get("last_touch_time"):
                        event.touches += 1
                        event.metadata["last_touch_time"] = bar_time

    # ------------------------------------------------------------------
    # Invalidation
    # ------------------------------------------------------------------

    def _invalidate_events(
        self,
        symbol: str,
        bars: list[dict],
        current_price: float,
        atr: float,
    ) -> None:
        """Invalidate zones decisively broken by later price action.

        Invalidated events get status INVALIDATED (not CONFIRMED) and are
        excluded from decisions, touches, and future detections of the same
        event_id.
        """
        events = self._events[symbol]
        now_iso = utc_now_iso()

        for event in events:
            if event.later_modified:
                continue

            invalidated = False
            if event.event_type in ("bos", "choch"):
                if event.side == "bullish" and current_price < event.bottom - atr:
                    invalidated = True
                elif event.side == "bearish" and current_price > event.top + atr:
                    invalidated = True
            elif event.event_type == "fvg":
                if event.side == "bullish" and current_price <= event.bottom:
                    invalidated = True
                elif event.side == "bearish" and current_price >= event.top:
                    invalidated = True
            elif event.event_type == "order_block":
                if bars and bars[-1].get("close") is not None:
                    last_close = bars[-1]["close"]
                    if event.side == "bullish" and last_close < event.bottom:
                        invalidated = True
                    elif event.side == "bearish" and last_close > event.top:
                        invalidated = True

            if invalidated:
                event.later_modified = True
                event.status = "INVALIDATED"
                event.invalidated_at = now_iso
                write_structure_event(event)  # invalidate transition -> ledger

    # ------------------------------------------------------------------
    # Decision compression (WAIT until the full confirmation chain)
    # ------------------------------------------------------------------

    def _compress_decision(
        self,
        symbol: str,
        current_price: float,
        bars: list[dict],
    ) -> DecisionState:
        """Compress all active structure events into one decision state.

        BUY/SELL only when the confirmation chain holds:
          * >= 2 confirmed structures on the same side,
          * the most recent confirmation is on that side,
          * the last close is at/through the newest structure level
            (zone hold + level reclaimed).
        Everything else — forming-only, single confirmation, conflicting
        evidence, price back inside the zone — stays WAIT.
        """
        events = [e for e in self._events.get(symbol, []) if not e.later_modified]

        now_iso = utc_now_iso()
        now = time.time()

        confirmed = [e for e in events if e.status == "CONFIRMED"]
        bullish_confirmed = [e for e in confirmed if e.side == "bullish"]
        bearish_confirmed = [e for e in confirmed if e.side == "bearish"]
        latest_confirmed = confirmed[-1] if confirmed else None

        last_close = bars[-1].get("close") if bars else None

        bullish_chain = (
            len(bullish_confirmed) >= 2
            and latest_confirmed is not None
            and latest_confirmed.side == "bullish"
            and last_close is not None
            and last_close >= latest_confirmed.top
        )
        bearish_chain = (
            len(bearish_confirmed) >= 2
            and latest_confirmed is not None
            and latest_confirmed.side == "bearish"
            and last_close is not None
            and last_close <= latest_confirmed.bottom
        )

        if bullish_chain and not bearish_chain:
            state, bias = "BUY", "Bullish"
        elif bearish_chain and not bullish_chain:
            state, bias = "SELL", "Bearish"
        else:
            state = "WAIT"
            if bullish_confirmed and bearish_confirmed:
                bias = "Mixed"
            elif bullish_confirmed:
                bias = "Bullish"
            elif bearish_confirmed:
                bias = "Bearish"
            else:
                bias = "Neutral"

        # Active zone: most recent confirmation, else the most recent event.
        active_event = latest_confirmed or (events[-1] if events else None)

        active_zone = None
        zone_type = None
        zone_age = 0.0
        touches_confirmed = 0
        touches_provisional = 0
        price_location = "unknown"
        bull_trigger = "No active zone"
        bear_trigger = "No active zone"

        if active_event:
            active_zone = f"{active_event.event_type.upper()} {active_event.side}"
            zone_type = active_event.event_type.upper()
            try:
                detected = datetime.fromisoformat(
                    active_event.detected_at.replace("Z", "+00:00")
                )
                zone_age = max(0.0, (now - detected.timestamp()) / 60.0)
            except Exception:
                zone_age = 0.0
            touches_confirmed = active_event.touches
            touches_provisional = active_event.provisional_touches

            if current_price > active_event.top:
                price_location = "above_zone"
            elif current_price < active_event.bottom:
                price_location = "below_zone"
            else:
                price_location = "in_zone"

            bull_trigger = f"Close above {active_event.top:.5f}"
            bear_trigger = f"Close below {active_event.bottom:.5f}"

        # Data freshness: age of the last market bar, not the update call.
        data_age = -1.0
        if bars:
            last_epoch = _ts_epoch(bars[-1].get("time"))
            if last_epoch > 0:
                data_age = max(0.0, now - last_epoch)

        # Candle-close countdown: current UTC clock vs the next minute boundary.
        now_dt = datetime.now(timezone.utc)
        next_boundary = now_dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        candle_close_seconds = max(0, int((next_boundary - now_dt).total_seconds()))
        if data_age > 90:
            # Stale feed — no live candle is actually in flight; don't pretend
            # a countdown exists when the last market bar is minutes old.
            candle_close_seconds = 0

        return DecisionState(
            symbol=symbol,
            state=state,
            bias=bias,
            active_zone=active_zone,
            zone_type=zone_type,
            zone_age_minutes=zone_age,
            touches_confirmed=touches_confirmed,
            touches_provisional=touches_provisional,
            price_location=price_location,
            bull_trigger=bull_trigger,
            bear_trigger=bear_trigger,
            candle_close_seconds=candle_close_seconds,
            data_age_seconds=data_age,
            updated_at=now_iso,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _add_event(self, events: list[StructureEvent], event: StructureEvent) -> None:
        """Register a newly detected event (idempotent by event_id)."""
        if event.defining_candle_time is not None:
            event.event_id = (
                f"{event.symbol}|{event.event_type}|{event.side}|"
                f"{event.defining_candle_time}"
            )
        else:
            event.event_id = (
                f"{event.symbol}|{event.event_type}|{event.side}|{event.detected_at}"
            )
        for existing in events:
            if existing.event_id == event.event_id:
                return  # same structure instance already tracked
        events.append(event)
        # Create transition -> immutable ledger (idempotent via transaction_id).
        write_structure_event(event)

    def _empty_decision(self, symbol: str) -> DecisionState:
        """Return an empty decision state when engine is disabled."""
        return DecisionState(
            symbol=symbol,
            state="WAIT",
            bias="Neutral",
            active_zone=None,
            zone_type=None,
            zone_age_minutes=0,
            touches_confirmed=0,
            touches_provisional=0,
            price_location="unknown",
            bull_trigger="Engine disabled",
            bear_trigger="Engine disabled",
            candle_close_seconds=0,
            data_age_seconds=-1,
            updated_at=utc_now_iso(),
        )


# ---------------------------------------------------------------------------
# Structure Event Ledger (immutable append-only, idempotent per transition)
# ---------------------------------------------------------------------------

LEDGER_NAME = "m1_structure_events"


def write_structure_event(event: StructureEvent) -> None:
    """Append one structure-event lifecycle record to the immutable JSONL ledger.

    Idempotent per (event_id, status) via the archive transaction_id: the same
    transition is never appended twice, while the full create/confirm/invalidate
    stream is preserved (each transition is a distinct status).
    """
    try:
        from core.utils import append_archive_record

        record = event.to_dict()
        eid = event.event_id or "unknown"
        record["transaction_id"] = f"{eid}:{event.status}"
        append_archive_record(LEDGER_NAME, record)
    except Exception as exc:  # noqa: BLE001 — audit failure must stay visible
        _LOG.warning("Failed to write structure event to ledger: %s", exc)


def read_structure_events(
    symbol: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read the latest ledger record per event_id (most recent first)."""
    try:
        ledger_path = STATE_DIR / f"{LEDGER_NAME}.jsonl"
        if not ledger_path.exists():
            return []
        lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
        latest: dict[str, dict[str, Any]] = {}
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            eid = rec.get("event_id") or rec.get("transaction_id")
            if not eid:
                continue
            latest[eid] = rec  # keep the newest record per event
        events: list[dict[str, Any]] = []
        for rec in reversed(list(latest.values())):
            if symbol and rec.get("symbol") != symbol:
                continue
            events.append(rec)
        return events[:limit]
    except Exception as exc:  # noqa: BLE001 — read must never raise
        _LOG.warning("Failed to read structure events: %s", exc)
        return []
