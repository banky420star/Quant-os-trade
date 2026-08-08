"""M1 Structure shadow loop — forward-only structure engine runner.

Reads per-symbol M1 candles from state/latest_candles.json, feeds them into
core/m1_structure_engine.M1StructureEngine, and writes the compressed
WAIT/BUY/SELL decisions to state/m1_structure_decisions.json for the
dashboard.

Forward-only across pipeline cycles: the engine instance persists for the
process lifetime and restores its per-event state from the immutable ledger on
first construction, so detections are idempotent and FORMING -> CONFIRMED /
touch transitions are never replayed.

Shadow-only: never places orders, never touches the broker or the kill switch.
Fault-isolated: any failure logs a warning and leaves the last good decision
file untouched.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from core.utils import load_config, read_json_state, utc_now_iso, write_json_state

_LOG = logging.getLogger("m1_structure_loop")

M1_TF = "M1"

# Non-overlap guard: the dedicated shadow service may never stack two passes.
# A run that finds another pass still in flight is skipped (never queued).
_RUN_LOCK = threading.Lock()

# Persistent across pipeline cycles; lazily built from the ledger on first run.
_ENGINE: Any = None


def _collect_m1_bars(candles_doc: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Extract M1 bars per symbol from latest_candles.json.

    Returns {symbol: [bar, ...]} for symbols that actually have M1 rows.
    MT5 M1 bar rows carry open/high/low/close/time (+volume when available).
    """
    out: dict[str, list[dict[str, Any]]] = {}
    symbols = (candles_doc or {}).get("symbols") or {}
    if not isinstance(symbols, dict):
        return out
    for sym, tf_map in symbols.items():
        if not isinstance(tf_map, dict):
            continue
        bars = tf_map.get(M1_TF)
        if isinstance(bars, list) and len(bars) >= 3:
            clean = []
            for b in bars:
                if isinstance(b, dict) and all(k in b for k in ("open", "high", "low", "close")):
                    clean.append(b)
            if clean:
                out[sym] = clean
    return out


def _compute_atr(bars: list[dict[str, Any]], period: int = 14) -> float:
    """Simple ATR(period) over the bar list. Returns 0 when insufficient data."""
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        hi = float(bars[i]["high"])
        lo = float(bars[i]["low"])
        pc = float(bars[i - 1]["close"])
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    if not trs:
        return 0.0
    tail = trs[-period:]
    return sum(tail) / len(tail)


def _get_engine(config: dict[str, Any]) -> Any:
    """Return the process-lifetime engine, restoring state from the ledger once."""
    global _ENGINE
    if _ENGINE is None:
        from core.m1_structure_engine import M1StructureEngine

        _ENGINE = M1StructureEngine(config, restore_from_ledger=True)
    return _ENGINE


def _run_structure_pass(config: dict[str, Any]) -> dict[str, Any]:
    """One structure-detection pass across all symbols with M1 data."""
    m1_cfg = config.get("m1_structure") or {}
    if not bool(m1_cfg.get("enabled", False)):
        return {"enabled": False, "decisions": {}, "events": {}, "updated_at": utc_now_iso()}

    candles = read_json_state("latest_candles.json", default={})
    m1_bars_by_symbol = _collect_m1_bars(candles)
    if not m1_bars_by_symbol:
        return {
            "enabled": True,
            "decisions": {},
            "events": {},
            "reason": "no_m1_data",
            "updated_at": utc_now_iso(),
        }

    engine = _get_engine(config)
    decisions: dict[str, dict[str, Any]] = {}
    events_by_symbol: dict[str, list[dict[str, Any]]] = {}

    for symbol, bars in m1_bars_by_symbol.items():
        if len(bars) < 3:
            continue
        atr = _compute_atr(bars)
        current_price = float(bars[-1]["close"])
        decision = engine.update(symbol, bars, current_price, atr)
        decisions[symbol] = decision.to_dict()
        events_by_symbol[symbol] = engine.get_events(symbol)

    return {
        "enabled": True,
        "decisions": decisions,
        "events": events_by_symbol,
        "updated_at": utc_now_iso(),
    }


def run() -> dict[str, Any] | None:
    """Shadow service entry point. Reads-only, shadow-only, fault-isolated.

    Non-reentrant: if a previous pass is still running, this call returns None
    immediately (never queues or overlaps).
    """
    if not _RUN_LOCK.acquire(blocking=False):
        _LOG.warning("M1 structure loop: previous run still in progress — skipping")
        return None
    try:
        try:
            config = load_config()
            result = _run_structure_pass(config)
            write_json_state("m1_structure_decisions.json", result)
            n = len(result.get("decisions") or {})
            _LOG.info(
                "M1 structure loop: decisions=%d enabled=%s",
                n, result.get("enabled"),
            )
            return result
        except Exception as exc:  # noqa: BLE001 — shadow service must never break the app
            _LOG.warning("M1 structure loop failed: %s", exc)
            return None
    finally:
        _RUN_LOCK.release()
