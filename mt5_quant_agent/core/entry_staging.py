"""Entry confirmation staging — wait for conditions to hold before filling."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.utils import read_json_state, utc_now_iso, write_json_state

STATE_FILE = "entry_staging.json"


def entry_confirm_seconds(config: dict[str, Any]) -> float:
    trading = config.get("trading") or {}
    sec = trading.get("entry_confirm_seconds")
    if sec is not None:
        return max(0.0, float(sec))
    micro = (config.get("practice") or {}).get("micro") or {}
    if micro.get("enabled") and micro.get("entry_confirm_seconds") is not None:
        return max(0.0, float(micro["entry_confirm_seconds"]))
    return 0.0


def staging_key(signal: dict[str, Any]) -> str:
    return "|".join(
        str(signal.get(k, ""))
        for k in ("symbol", "side", "setup_type")
    )


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _max_drift_atr(config: dict[str, Any]) -> float:
    se = (config.get("trading") or {}).get("strategy_entries") or {}
    return float(se.get("max_entry_wait_atr", 2.0))


def _price_drifted(signal: dict[str, Any], row: dict[str, Any], config: dict[str, Any]) -> bool:
    """Reset confirm timer when price drifts too far from the staged anchor."""
    anchor = row.get("anchor_price")
    if anchor is None:
        return False
    price = float(signal.get("market_price") or signal.get("entry") or 0)
    if price <= 0:
        return False
    atr_hint = float(signal.get("distance_atr") or 0)
    if atr_hint > 0 and signal.get("entry"):
        est_atr = abs(float(signal["entry"]) - price) / atr_hint if atr_hint else 0
    else:
        est_atr = price * 0.001
    max_drift = _max_drift_atr(config) * max(est_atr, price * 0.0005)
    return abs(price - float(anchor)) > max_drift


def touch_and_check(
    signal: dict[str, Any],
    config: dict[str, Any],
    feat: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Track how long this signal fingerprint has been continuously valid."""
    need = entry_confirm_seconds(config)
    if need <= 0:
        return {"ready": True, "age_sec": 0.0, "need_sec": 0.0}

    now = datetime.now(timezone.utc)
    state = read_json_state(STATE_FILE, default={"entries": {}})
    entries: dict[str, Any] = dict(state.get("entries") or {})
    key = staging_key(signal)
    row = entries.get(key)
    anchor = signal.get("entry_anchor_price") or signal.get("entry")

    if row and _price_drifted(signal, row, config):
        row = None

    if not row or not row.get("first_seen_at"):
        entries[key] = {
            "first_seen_at": utc_now_iso(),
            "signal_id": signal.get("signal_id"),
            "symbol": signal.get("symbol"),
            "anchor_price": float(anchor) if anchor is not None else None,
            "market_price": signal.get("market_price"),
        }
        write_json_state(STATE_FILE, {"updated_at": utc_now_iso(), "entries": entries})
        return {"ready": False, "age_sec": 0.0, "need_sec": need, "reset": "new_or_drift"}

    first = _parse_ts(str(row["first_seen_at"]))
    age = (now - first).total_seconds() if first else 0.0
    row["signal_id"] = signal.get("signal_id")
    row["last_seen_at"] = utc_now_iso()
    row["anchor_price"] = float(anchor) if anchor is not None else row.get("anchor_price")
    row["market_price"] = signal.get("market_price")
    if feat:
        row["m5_trend"] = feat.get("m5_trend")
    entries[key] = row
    write_json_state(STATE_FILE, {"updated_at": utc_now_iso(), "entries": entries})
    return {
        "ready": age >= need,
        "age_sec": round(age, 1),
        "need_sec": need,
        "first_seen_at": row.get("first_seen_at"),
    }


def clear_symbol(symbol: str) -> None:
    """Drop staged fingerprints for a symbol (e.g. after a close)."""
    state = read_json_state(STATE_FILE, default={"entries": {}})
    entries = {
        k: v for k, v in (state.get("entries") or {}).items()
        if not str(k).startswith(f"{symbol}|")
    }
    write_json_state(STATE_FILE, {"updated_at": utc_now_iso(), "entries": entries})


def prune_stale(max_age_sec: float = 600.0) -> None:
    """Remove staging rows that have not been seen recently."""
    now = datetime.now(timezone.utc)
    state = read_json_state(STATE_FILE, default={"entries": {}})
    kept: dict[str, Any] = {}
    for key, row in (state.get("entries") or {}).items():
        raw = row.get("last_seen_at") or row.get("first_seen_at")
        ts = _parse_ts(str(raw)) if raw else None
        if ts and (now - ts).total_seconds() <= max_age_sec:
            kept[key] = row
    write_json_state(STATE_FILE, {"updated_at": utc_now_iso(), "entries": kept})