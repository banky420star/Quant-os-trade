"""Market-closed back-off (Phase 2.4 cleanup).

When a broker order fails because the symbol's market is closed (MT5 retcode
10018 / "Market closed" / "Trade disabled"), retrying every cycle spams failed
orders. This module records a per-symbol back-off window so execution_loop
skips that symbol until the market is likely open again. Back-off clears the
moment an order for the symbol succeeds (market reopened).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.utils import read_json_state, utc_now_iso, write_json_state

STATE_FILE = "market_closed_backoff.json"

# MT5 retcodes that mean "market is closed / trading disabled right now".
MARKET_CLOSED_RETCODES = {10018, 10020}
MARKET_CLOSED_TOKENS = ("market closed", "trade disabled", "market is closed", "trading disabled")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _backoff_minutes(config: dict[str, Any]) -> float:
    return float((config.get("execution") or {}).get("market_closed_backoff_minutes", 5.0))


def read_backoff() -> dict[str, str]:
    return (read_json_state(STATE_FILE, default={}) or {}).get("symbols") or {}


def in_backoff(symbol: str, config: dict[str, Any] | None = None) -> bool:
    sym_map = read_backoff()
    until = _parse_iso(sym_map.get(symbol))
    return bool(until and until > datetime.now(timezone.utc))


def _write(sym_map: dict[str, str]) -> None:
    write_json_state(STATE_FILE, {"timestamp": utc_now_iso(), "symbols": sym_map})


def record_market_closed(symbol: str, config: dict[str, Any]) -> None:
    """Set a back-off window for a symbol after a market-closed rejection."""
    mins = _backoff_minutes(config)
    until = (datetime.now(timezone.utc) + timedelta(minutes=mins)).isoformat()
    sym_map = dict(read_backoff())
    sym_map[symbol] = until
    # prune expired entries
    now = datetime.now(timezone.utc)
    sym_map = {s: u for s, u in sym_map.items() if (_parse_iso(u) or now) > now}
    _write(sym_map)


def clear_backoff(symbol: str) -> None:
    sym_map = dict(read_backoff())
    if symbol in sym_map:
        sym_map.pop(symbol, None)
        _write(sym_map)


def is_market_closed_error(err: dict[str, Any]) -> bool:
    msg = str(err.get("error") or err).lower()
    if any(tok in msg for tok in MARKET_CLOSED_TOKENS):
        return True
    try:
        return int(err.get("retcode") or 0) in MARKET_CLOSED_RETCODES
    except (TypeError, ValueError):
        return False
