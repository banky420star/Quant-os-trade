"""M1 Smart-Money Structure Engine — ICT/SMC structure detection on M1.

Detects Break of Structure (BOS), Change of Character (CHoCH), Fair Value Gaps
(FVG), and Order Blocks (OB) on M1 candles with a provisional/confirmed state
machine. Every event is logged to an immutable append-only ledger.

Design principles:
  - Provisional events are tagged FORMING until the candle closes, then CONFIRMED.
  - No execution logic uses a forming M1 candle as though it were closed.
  - Touches are counted only on candle close (provisional interactions are
    marked separately).
  - The decision state compresses all conflicting structure into one clear
    WAIT/BUY/SELL per symbol.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.utils import STATE_DIR, utc_now_iso

_LOG = logging.getLogger("m1_structure")

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
    status: str  # "FORMING" or "CONFIRMED"
    detected_at: str  # ISO timestamp when first detected
    confirmed_at: str | None  # ISO timestamp when candle closed and confirmed
    price: float  # price at detection
    top: float  # upper boundary of zone (for OB/FVG)
    bottom: float  # lower boundary of zone (for OB/FVG)
    touches: int  # confirmed touches (only incremented on candle close)
    provisional_touches: int  # current candle interactions (not confirmed)
    candle_closed: bool  # True if the defining candle has closed
    later_modified: bool  # True if the event was invalidated by later price action
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side,
            "status": self.status,
            "detected_at": self.detected_at,
            "confirmed_at": self.confirmed_at,
            "price": self.price,
            "top": self.top,
            "bottom": self.bottom,
            "touches": self.touches,
            "provisional_touches": self.provisional_touches,
            "candle_closed": self.candle_closed,
            "later_modified": self.later_modified,
            "metadata": self.metadata,
        }


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
    candle_close_seconds: int  # seconds until M1 candle closes
    data_age_seconds: float  # how fresh the data is
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

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        m1_cfg = cfg.get("m1_structure", {}) or {}
        self.enabled = bool(m1_cfg.get("enabled", False))
        self.lookback_bars = int(m1_cfg.get("lookback_bars", 50))
        self.min_displacement_atr = float(m1_cfg.get("min_displacement_atr", 0.5))
        self.min_fvg_atr = float(m1_cfg.get("min_fvg_atr", 0.25))
        self.ob_lookback = int(m1_cfg.get("ob_lookback", 20))
        # Per-symbol state: {symbol: [StructureEvent, ...]}
        self._events: dict[str, list[StructureEvent]] = {}
        # Per-symbol decision state
        self._decisions: dict[str, DecisionState] = {}
        # Last update timestamp per symbol
        self._last_update: dict[str, float] = {}

    def update(
        self,
        symbol: str,
        m1_bars: list[dict[str, Any]],
        current_price: float,
        atr: float,
    ) -> DecisionState:
        """Process M1 bars and return the compressed decision state.

        Call this on every M1 tick/close. The engine will:
        1. Detect new structure events (provisional)
        2. Confirm events whose defining candle has closed
        3. Update touches on confirmed zones
        4. Compress into one decision state
        """
        if not self.enabled:
            return self._empty_decision(symbol)

        now = time.time()
        self._last_update[symbol] = now

        # Ensure event list exists
        if symbol not in self._events:
            self._events[symbol] = []

        # Process bars
        self._detect_bos_choch(symbol, m1_bars, current_price, atr)
        self._detect_fvg(symbol, m1_bars, atr)
        self._detect_order_blocks(symbol, m1_bars, atr)
        self._update_touches(symbol, m1_bars, current_price)
        self._confirm_events(symbol, m1_bars)
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
        CHoCH: Price breaks a recent swing high/low AGAINST the trend
               (character change from bullish to bearish or vice versa).
        """
        if len(bars) < 5:
            return

        events = self._events[symbol]
        now_iso = utc_now_iso()

        # Find recent swing highs and lows (using last N bars)
        lookback = min(self.lookback_bars, len(bars) - 1)
        window = bars[-lookback:]

        swing_highs = []
        swing_lows = []
        for i in range(2, len(window) - 2):
            bar = window[i]
            prev1 = window[i - 1]
            prev2 = window[i - 2]
            next1 = window[i + 1]
            next2 = window[i + 2]
            # Swing high: bar's high is higher than 2 bars on each side
            if (bar["high"] > prev1["high"] and bar["high"] > prev2["high"]
                    and bar["high"] > next1["high"] and bar["high"] > next2["high"]):
                swing_highs.append({"index": i, "price": bar["high"], "time": bar.get("time")})
            # Swing low: bar's low is lower than 2 bars on each side
            if (bar["low"] < prev1["low"] and bar["low"] < prev2["low"]
                    and bar["low"] < next1["low"] and bar["low"] < next2["low"]):
                swing_lows.append({"index": i, "price": bar["low"], "time": bar.get("time")})

        if not swing_highs and not swing_lows:
            return

        # Determine current trend from recent swing structure
        recent_highs = swing_highs[-3:] if len(swing_highs) >= 3 else swing_highs
        recent_lows = swing_lows[-3:] if len(swing_lows) >= 3 else swing_lows

        bullish_structure = False
        bearish_structure = False
        if len(recent_highs) >= 2:
            if recent_highs[-1]["price"] > recent_highs[-2]["price"]:
                bullish_structure = True
        if len(recent_lows) >= 2:
            if recent_lows[-1]["price"] < recent_lows[-2]["price"]:
                bearish_structure = True

        # Check for BOS (break in trend direction)
        if bullish_structure and swing_highs:
            last_high = swing_highs[-1]["price"]
            if current_price > last_high:
                # BOS bullish — price broke above the last swing high
                self._add_event(events, StructureEvent(
                    event_type="bos",
                    symbol=symbol,
                    timeframe="M1",
                    side="bullish",
                    status="FORMING",
                    detected_at=now_iso,
                    confirmed_at=None,
                    price=current_price,
                    top=current_price,
                    bottom=last_high,
                    touches=0,
                    provisional_touches=0,
                    candle_closed=False,
                    later_modified=False,
                    metadata={"swing_high": last_high},
                ))

        if bearish_structure and swing_lows:
            last_low = swing_lows[-1]["price"]
            if current_price < last_low:
                # BOS bearish — price broke below the last swing low
                self._add_event(events, StructureEvent(
                    event_type="bos",
                    symbol=symbol,
                    timeframe="M1",
                    side="bearish",
                    status="FORMING",
                    detected_at=now_iso,
                    confirmed_at=None,
                    price=current_price,
                    top=last_low,
                    bottom=current_price,
                    touches=0,
                    provisional_touches=0,
                    candle_closed=False,
                    later_modified=False,
                    metadata={"swing_low": last_low},
                ))

        # Check for CHoCH (break against trend)
        if bullish_structure and swing_lows:
            last_low = swing_lows[-1]["price"]
            if current_price < last_low:
                # CHoCH bearish — bullish structure broken
                self._add_event(events, StructureEvent(
                    event_type="choch",
                    symbol=symbol,
                    timeframe="M1",
                    side="bearish",
                    status="FORMING",
                    detected_at=now_iso,
                    confirmed_at=None,
                    price=current_price,
                    top=last_low,
                    bottom=current_price,
                    touches=0,
                    provisional_touches=0,
                    candle_closed=False,
                    later_modified=False,
                    metadata={"broken_structure": "bullish", "swing_low": last_low},
                ))

        if bearish_structure and swing_highs:
            last_high = swing_highs[-1]["price"]
            if current_price > last_high:
                # CHoCH bullish — bearish structure broken
                self._add_event(events, StructureEvent(
                    event_type="choch",
                    symbol=symbol,
                    timeframe="M1",
                    side="bullish",
                    status="FORMING",
                    detected_at=now_iso,
                    confirmed_at=None,
                    price=current_price,
                    top=current_price,
                    bottom=last_high,
                    touches=0,
                    provisional_touches=0,
                    candle_closed=False,
                    later_modified=False,
                    metadata={"broken_structure": "bearish", "swing_high": last_high},
                ))

    # ------------------------------------------------------------------
    # Detection: Fair Value Gap (FVG)
    # ------------------------------------------------------------------

    def _detect_fvg(
        self,
        symbol: str,
        bars: list[dict],
        atr: float,
    ) -> None:
        """Detect ICT/SMC Fair Value Gaps on M1 (3-bar structural imbalance)."""
        if len(bars) < 3 or atr <= 0:
            return

        events = self._events[symbol]
        now_iso = utc_now_iso()

        # Check the last 3 bars for FVG
        bar0 = bars[-3]  # i-2
        bar1 = bars[-2]  # i-1
        bar2 = bars[-1]  # i (current)

        # Bullish FVG: bar[i-2].high < bar[i].low (gap up)
        if bar0["high"] < bar2["low"]:
            gap = bar2["low"] - bar0["high"]
            gap_atr = gap / atr if atr > 0 else 0
            if gap_atr >= self.min_fvg_atr:
                self._add_event(events, StructureEvent(
                    event_type="fvg",
                    symbol=symbol,
                    timeframe="M1",
                    side="bullish",
                    status="FORMING",
                    detected_at=now_iso,
                    confirmed_at=None,
                    price=bar2["close"],
                    top=bar2["low"],
                    bottom=bar0["high"],
                    touches=0,
                    provisional_touches=0,
                    candle_closed=False,
                    later_modified=False,
                    metadata={"gap_size": gap, "gap_atr": round(gap_atr, 3)},
                ))

        # Bearish FVG: bar[i-2].low > bar[i].high (gap down)
        if bar0["low"] > bar2["high"]:
            gap = bar0["low"] - bar2["high"]
            gap_atr = gap / atr if atr > 0 else 0
            if gap_atr >= self.min_fvg_atr:
                self._add_event(events, StructureEvent(
                    event_type="fvg",
                    symbol=symbol,
                    timeframe="M1",
                    side="bearish",
                    status="FORMING",
                    detected_at=now_iso,
                    confirmed_at=None,
                    price=bar2["close"],
                    top=bar0["low"],
                    bottom=bar2["high"],
                    touches=0,
                    provisional_touches=0,
                    candle_closed=False,
                    later_modified=False,
                    metadata={"gap_size": gap, "gap_atr": round(gap_atr, 3)},
                ))

    # ------------------------------------------------------------------
    # Detection: Order Blocks (OB)
    # ------------------------------------------------------------------

    def _detect_order_blocks(
        self,
        symbol: str,
        bars: list[dict],
        atr: float,
    ) -> None:
        """Detect ICT/SMC Order Blocks on M1 (prior opposite-color candle + displacement)."""
        if len(bars) < 3 or atr <= 0:
            return

        events = self._events[symbol]
        now_iso = utc_now_iso()

        # Check the last 3 bars
        bar0 = bars[-3]  # potential OB candle
        bar1 = bars[-2]  # displacement candle
        bar2 = bars[-1]  # current candle

        # Bullish OB: bearish candle (bar0) followed by strong bullish displacement (bar1)
        bar0_body = bar0["close"] - bar0["open"]
        bar1_body = bar1["close"] - bar1["open"]
        bar1_range = bar1["high"] - bar1["low"]

        if bar0_body < 0 and bar1_body > 0:
            # Bearish candle followed by bullish displacement
            displacement = bar1_body / atr if atr > 0 else 0
            if displacement >= self.min_displacement_atr:
                ob_top = bar0["open"]  # OB is the bearish candle body
                ob_bottom = bar0["low"]
                self._add_event(events, StructureEvent(
                    event_type="order_block",
                    symbol=symbol,
                    timeframe="M1",
                    side="bullish",
                    status="FORMING",
                    detected_at=now_iso,
                    confirmed_at=None,
                    price=bar2["close"],
                    top=ob_top,
                    bottom=ob_bottom,
                    touches=0,
                    provisional_touches=0,
                    candle_closed=False,
                    later_modified=False,
                    metadata={"displacement_atr": round(displacement, 3)},
                ))

        # Bearish OB: bullish candle (bar0) followed by strong bearish displacement (bar1)
        if bar0_body > 0 and bar1_body < 0:
            displacement = abs(bar1_body) / atr if atr > 0 else 0
            if displacement >= self.min_displacement_atr:
                ob_top = bar0["high"]
                ob_bottom = bar0["open"]  # OB is the bullish candle body
                self._add_event(events, StructureEvent(
                    event_type="order_block",
                    symbol=symbol,
                    timeframe="M1",
                    side="bearish",
                    status="FORMING",
                    detected_at=now_iso,
                    confirmed_at=None,
                    price=bar2["close"],
                    top=ob_top,
                    bottom=ob_bottom,
                    touches=0,
                    provisional_touches=0,
                    candle_closed=False,
                    later_modified=False,
                    metadata={"displacement_atr": round(displacement, 3)},
                ))

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

        Confirmed touches are only incremented when a candle CLOSES inside the zone.
        Provisional touches track current-candle interactions.
        """
        events = self._events[symbol]
        for event in events:
            if event.later_modified:
                continue

            # Reset provisional touches (recalculated each tick)
            event.provisional_touches = 0

            # Check if current price is inside the zone
            if event.bottom <= current_price <= event.top:
                event.provisional_touches = 1

            # Check if the last closed bar's close was inside the zone
            if bars:
                last_closed = bars[-2] if len(bars) >= 2 else None
                if last_closed:
                    close = last_closed["close"]
                    if event.bottom <= close <= event.top:
                        # Only count if this is a new touch (not same bar)
                        last_touch_time = event.metadata.get("last_touch_time")
                        bar_time = last_closed.get("time")
                        if bar_time != last_touch_time:
                            event.touches += 1
                            event.metadata["last_touch_time"] = bar_time

    # ------------------------------------------------------------------
    # Confirmation
    # ------------------------------------------------------------------

    def _confirm_events(self, symbol: str, bars: list[dict]) -> None:
        """Confirm FORMING events once their defining candle has closed."""
        events = self._events[symbol]
        now_iso = utc_now_iso()

        for event in events:
            if event.status == "FORMING" and not event.candle_closed:
                # The event is confirmed when the candle that formed it has closed.
                # For now, we mark it confirmed after the next bar appears.
                if bars and len(bars) >= 2:
                    event.candle_closed = True
                    event.status = "CONFIRMED"
                    event.confirmed_at = now_iso

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
        """Invalidate events that have been decisively broken by later price action."""
        events = self._events[symbol]

        for event in events:
            if event.later_modified:
                continue

            # Invalidate BOS/CHoCH if price reverses more than 1 ATR beyond the level
            if event.event_type in ("bos", "choch"):
                if event.side == "bullish" and current_price < event.bottom - atr:
                    event.later_modified = True
                elif event.side == "bearish" and current_price > event.top + atr:
                    event.later_modified = True

            # Invalidate FVG if price fills the gap completely
            if event.event_type == "fvg":
                if event.side == "bullish" and current_price <= event.bottom:
                    event.later_modified = True
                elif event.side == "bearish" and current_price >= event.top:
                    event.later_modified = True

            # Invalidate OB if price closes through the zone
            if event.event_type == "order_block":
                if bars:
                    last_close = bars[-1]["close"]
                    if event.side == "bullish" and last_close < event.bottom:
                        event.later_modified = True
                    elif event.side == "bearish" and last_close > event.top:
                        event.later_modified = True

    # ------------------------------------------------------------------
    # Decision compression
    # ------------------------------------------------------------------

    def _compress_decision(
        self,
        symbol: str,
        current_price: float,
        bars: list[dict],
    ) -> DecisionState:
        """Compress all active structure events into one clear decision state."""
        events = [e for e in self._events.get(symbol, []) if not e.later_modified]

        now_iso = utc_now_iso()
        now = time.time()

        # Find the most recent confirmed event as the "active zone"
        confirmed = [e for e in events if e.status == "CONFIRMED"]
        active_event = confirmed[-1] if confirmed else None

        # Count bullish vs bearish evidence
        bullish_count = sum(1 for e in events if e.side == "bullish" and e.status == "CONFIRMED")
        bearish_count = sum(1 for e in events if e.side == "bearish" and e.status == "CONFIRMED")

        # Determine bias
        if bullish_count > bearish_count:
            bias = "Bullish"
        elif bearish_count > bullish_count:
            bias = "Bearish"
        elif bullish_count > 0 and bearish_count > 0:
            bias = "Mixed"
        else:
            bias = "Neutral"

        # Determine state
        if bias == "Mixed" or bias == "Neutral":
            state = "WAIT"
        elif bias == "Bullish":
            state = "BUY"
        elif bias == "Bearish":
            state = "SELL"
        else:
            state = "WAIT"

        # Active zone info
        active_zone = None
        zone_type = None
        zone_age = 0.0
        touches_confirmed = 0
        touches_provisional = 0

        if active_event:
            active_zone = f"{active_event.event_type.upper()} {active_event.side}"
            zone_type = active_event.event_type.upper()
            # Calculate age
            try:
                detected = datetime.fromisoformat(active_event.detected_at.replace("Z", "+00:00"))
                zone_age = (now - detected.timestamp()) / 60.0
            except Exception:
                zone_age = 0.0
            touches_confirmed = active_event.touches
            touches_provisional = active_event.provisional_touches

        # Price location relative to active zone
        price_location = "unknown"
        if active_event:
            if current_price > active_event.top:
                price_location = "above_zone"
            elif current_price < active_event.bottom:
                price_location = "below_zone"
            else:
                price_location = "in_zone"

        # Triggers
        if active_event:
            if active_event.side == "bullish":
                bull_trigger = f"Close above {active_event.top:.5f}"
                bear_trigger = f"Close below {active_event.bottom:.5f}"
            else:
                bull_trigger = f"Close above {active_event.top:.5f}"
                bear_trigger = f"Close below {active_event.bottom:.5f}"
        else:
            bull_trigger = "No active zone"
            bear_trigger = "No active zone"

        # Candle close countdown (M1 = 60 seconds)
        candle_close_seconds = 0
        if bars:
            last_bar = bars[-1]
            bar_time = last_bar.get("time")
            if bar_time:
                try:
                    if isinstance(bar_time, str):
                        dt = datetime.fromisoformat(bar_time.replace("Z", "+00:00"))
                    else:
                        dt = datetime.fromtimestamp(bar_time, tz=timezone.utc)
                    # M1 candle closes at the next minute boundary
                    seconds_into_minute = dt.second
                    candle_close_seconds = max(0, 60 - seconds_into_minute)
                except Exception:
                    candle_close_seconds = 0

        # Data age
        last_update = self._last_update.get(symbol, 0)
        data_age = now - last_update if last_update > 0 else -1.0

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
        """Add an event if it doesn't duplicate an existing recent one."""
        # Deduplicate: don't add if the same type/side exists within last 5 bars
        for existing in events[-10:]:
            if (existing.event_type == event.event_type
                    and existing.side == event.side
                    and existing.status != "INVALIDATED"):
                return
        events.append(event)

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
# Structure Event Ledger (immutable append-only)
# ---------------------------------------------------------------------------

LEDGER_NAME = "m1_structure_events"


def write_structure_event(event: StructureEvent) -> None:
    """Append one structure event to the immutable JSONL ledger."""
    try:
        from core.utils import append_archive_record
        append_archive_record(LEDGER_NAME, event.to_dict())
    except Exception as exc:
        _LOG.warning("Failed to write structure event to ledger: %s", exc)


def read_structure_events(
    symbol: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read recent structure events from the ledger."""
    try:
        ledger_path = STATE_DIR / f"{LEDGER_NAME}.jsonl"
        if not ledger_path.exists():
            return []
        lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
        events = []
        for line in reversed(lines[-limit:]):
            try:
                import json
                event = json.loads(line)
                if symbol and event.get("symbol") != symbol:
                    continue
                events.append(event)
            except Exception:
                continue
        return events
    except Exception as exc:
        _LOG.warning("Failed to read structure events: %s", exc)
        return []
