"""Forward-label filtered paper signals for unbiased opportunity analysis.

This module records signals rejected by evaluation/verifier before their future
bars are known, then labels them later from stored M5 history. It never places
orders, changes gates, or writes live/MT5 state. A signal is labeled only when
its explicit SL/TP is known and the future window is complete (or an SL/TP was
already hit), with pessimistic SL-first handling on same-bar ambiguity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from core.history_manager import HistoryManager
from core.utils import read_json_state, utc_now_iso, write_json_state

STATE_FILE = "filtered_signal_labels.json"
SCHEMA_VERSION = 1
_DEFAULT_HORIZON_BARS = 48
_DEFAULT_MAX_PENDING = 10_000
_DEFAULT_MAX_LABELED = 50_000

HistoryLoader = Callable[[str, str], list[dict[str, Any]]]


def _settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("forward_labeling") or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "timeframe": str(raw.get("timeframe", "M5")),
        "horizon_bars": max(1, int(raw.get("horizon_bars", _DEFAULT_HORIZON_BARS))),
        "max_pending": max(100, int(raw.get("max_pending", _DEFAULT_MAX_PENDING))),
        "max_labeled": max(100, int(raw.get("max_labeled", _DEFAULT_MAX_LABELED))),
    }


def _paper_only(config: dict[str, Any]) -> bool:
    return str((config.get("execution") or {}).get("mode", "paper")).lower() == "paper"


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _signal_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    """Keep the fields needed for labeling and later analysis."""
    signal = row.get("signal") if isinstance(row.get("signal"), dict) else row
    snapshot: dict[str, Any] = {}
    for key in (
        "signal_id", "symbol", "side", "setup_type", "entry", "sl", "tp1", "tp2",
        "created_at", "market_context", "confidence", "confidence_tree", "evidence",
        "reason", "reasons", "trigger_summary", "trigger_context", "entry_narrative",
    ):
        if key in signal and signal.get(key) is not None:
            snapshot[key] = signal.get(key)
    # Verifier rejection metadata is deliberately kept separate from the
    # signal snapshot so the label describes an opportunity, not a trade.
    for key in (
        "rejection_reason", "failure_codes", "failures", "checks", "verified_at",
        "rejected_at", "source",
    ):
        if key in row and row.get(key) is not None:
            snapshot[key] = row.get(key)
    return snapshot


def _signal_time(snapshot: dict[str, Any], fallback: str) -> str:
    for key in ("created_at", "signal_time", "verified_at", "rejected_at"):
        value = snapshot.get(key)
        if _parse_time(value) is not None:
            return str(value)
    return fallback


def _levels(snapshot: dict[str, Any]) -> tuple[float, float, float, str] | None:
    side = str(snapshot.get("side") or "").upper()
    entry = _as_float(snapshot.get("entry"))
    sl = _as_float(snapshot.get("sl"))
    tp = _as_float(snapshot.get("tp1") or snapshot.get("tp"))
    if entry is None or sl is None or tp is None or side not in ("BUY", "SELL"):
        return None
    risk = entry - sl if side == "BUY" else sl - entry
    reward = tp - entry if side == "BUY" else entry - tp
    if risk <= 0 or reward <= 0:
        return None
    return entry, sl, tp, side


def label_signal_against_bars(
    snapshot: dict[str, Any],
    bars: list[dict[str, Any]],
    *,
    horizon_bars: int = _DEFAULT_HORIZON_BARS,
) -> dict[str, Any] | None:
    """Return a forward outcome once the window has enough data.

    Bars must be strictly after the signal's creation time and sorted oldest
    first. A hit can be labeled immediately; an unresolved signal remains
    pending until ``horizon_bars`` future bars exist.
    """
    levels = _levels(snapshot)
    if levels is None:
        return None
    entry, sl, tp, side = levels
    signal_dt = _parse_time(snapshot.get("created_at") or snapshot.get("signal_time"))
    future: list[dict[str, Any]] = []
    for bar in bars:
        bar_dt = _parse_time(bar.get("time"))
        if signal_dt is not None and (bar_dt is None or bar_dt <= signal_dt):
            continue
        future.append(bar)
    if not future:
        return None

    risk = entry - sl if side == "BUY" else sl - entry
    for index, bar in enumerate(future[:horizon_bars], start=1):
        high = _as_float(bar.get("high"))
        low = _as_float(bar.get("low"))
        close = _as_float(bar.get("close"))
        if high is None or low is None or close is None:
            continue
        # Conservative ordering: when both levels occur in one candle, count SL.
        if side == "BUY":
            if low <= sl:
                return {"r_multiple": -1.0, "exit_reason": "sl", "bars_held": index,
                        "exit_price": sl, "entry": entry, "sl": sl, "tp": tp, "side": side}
            if high >= tp:
                return {"r_multiple": round((tp - entry) / risk, 4), "exit_reason": "tp",
                        "bars_held": index, "exit_price": tp, "entry": entry, "sl": sl,
                        "tp": tp, "side": side}
        else:
            if high >= sl:
                return {"r_multiple": -1.0, "exit_reason": "sl", "bars_held": index,
                        "exit_price": sl, "entry": entry, "sl": sl, "tp": tp, "side": side}
            if low <= tp:
                return {"r_multiple": round((entry - tp) / risk, 4), "exit_reason": "tp",
                        "bars_held": index, "exit_price": tp, "entry": entry, "sl": sl,
                        "tp": tp, "side": side}

    if len(future) < horizon_bars:
        return None
    close = _as_float(future[horizon_bars - 1].get("close"))
    if close is None:
        return None
    r = ((close - entry) / risk) if side == "BUY" else ((entry - close) / risk)
    return {"r_multiple": round(r, 4), "exit_reason": "time_stop", "bars_held": horizon_bars,
            "exit_price": close, "entry": entry, "sl": sl, "tp": tp, "side": side}


def _default_doc() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": None,
        "pending": [],
        "labeled": [],
        "stats": {"recorded": 0, "labeled": 0, "pending": 0, "skipped_invalid": 0},
    }


def _load_doc() -> dict[str, Any]:
    raw = read_json_state(STATE_FILE, default={})
    if not isinstance(raw, dict):
        return _default_doc()
    doc = _default_doc()
    doc.update(raw)
    doc["pending"] = list(doc.get("pending") or [])
    doc["labeled"] = list(doc.get("labeled") or [])
    doc["stats"] = {**_default_doc()["stats"], **(doc.get("stats") or {})}
    return doc


def record_filtered_signals(
    signals: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    source: str,
    now: str | None = None,
    history_loader: HistoryLoader | None = None,
) -> dict[str, Any]:
    """Persist filtered opportunities and refresh any matured labels."""
    cfg = _settings(config)
    if not cfg["enabled"] or not _paper_only(config):
        return {"enabled": False, "recorded": 0, "labeled": 0}
    doc = _load_doc()
    timestamp = now or utc_now_iso()
    pending_by_id = {
        str(row.get("signal_id")): row for row in doc["pending"] if row.get("signal_id")
    }
    labeled_ids = {str(row.get("signal_id")) for row in doc["labeled"] if row.get("signal_id")}
    recorded = 0
    invalid = 0
    for row in signals:
        if not isinstance(row, dict):
            continue
        snapshot = _signal_snapshot(row)
        sid = str(snapshot.get("signal_id") or "").strip()
        if not sid:
            continue
        if sid in labeled_ids:
            continue
        if _levels(snapshot) is None:
            invalid += 1
            continue
        existing = pending_by_id.get(sid)
        if existing is None:
            pending_by_id[sid] = {
                "signal_id": sid,
                "symbol": snapshot.get("symbol"),
                "side": snapshot.get("side"),
                "setup_type": snapshot.get("setup_type"),
                "signal_time": _signal_time(snapshot, timestamp),
                "first_seen_at": timestamp,
                "last_seen_at": timestamp,
                "filter_sources": [source],
                "filter_reasons": list(row.get("failure_codes") or row.get("failures") or []),
                "signal": snapshot,
            }
            recorded += 1
        else:
            existing["last_seen_at"] = timestamp
            sources = set(existing.get("filter_sources") or [])
            sources.add(source)
            existing["filter_sources"] = sorted(sources)
            reasons = list(existing.get("filter_reasons") or [])
            for reason in (row.get("failure_codes") or row.get("failures") or []):
                if reason not in reasons:
                    reasons.append(reason)
            existing["filter_reasons"] = reasons
    doc["pending"] = list(pending_by_id.values())[-cfg["max_pending"]:]
    doc["stats"]["recorded"] = int(doc["stats"].get("recorded", 0)) + recorded
    doc["stats"]["skipped_invalid"] = int(doc["stats"].get("skipped_invalid", 0)) + invalid
    return _refresh_doc(doc, config, cfg, history_loader=history_loader, now=timestamp, recorded=recorded)


def _history_loader(config: dict[str, Any]) -> HistoryLoader:
    manager = HistoryManager(config)

    def load(symbol: str, timeframe: str) -> list[dict[str, Any]]:
        frame = manager.load(symbol, timeframe)
        if frame is None or frame.empty:
            return []
        return frame.sort_values("time").to_dict("records")

    return load


def _refresh_doc(
    doc: dict[str, Any],
    config: dict[str, Any],
    cfg: dict[str, Any] | None = None,
    *,
    history_loader: HistoryLoader | None = None,
    now: str | None = None,
    recorded: int = 0,
) -> dict[str, Any]:
    cfg = cfg or _settings(config)
    loader = history_loader or _history_loader(config)
    still_pending: list[dict[str, Any]] = []
    labeled = list(doc.get("labeled") or [])
    labeled_ids = {str(row.get("signal_id")) for row in labeled if row.get("signal_id")}
    newly_labeled = 0
    for item in doc.get("pending") or []:
        snapshot = dict(item.get("signal") or {})
        symbol = str(item.get("symbol") or snapshot.get("symbol") or "")
        bars = loader(symbol, cfg["timeframe"]) if symbol else []
        outcome = label_signal_against_bars(snapshot, bars, horizon_bars=cfg["horizon_bars"])
        if outcome is None:
            still_pending.append(item)
            continue
        sid = str(item.get("signal_id"))
        if sid in labeled_ids:
            continue
        labeled.append({
            **item,
            "labeled_at": now or utc_now_iso(),
            "outcome": outcome,
        })
        labeled_ids.add(sid)
        newly_labeled += 1
    doc["pending"] = still_pending[-cfg["max_pending"]:]
    doc["labeled"] = labeled[-cfg["max_labeled"]:]
    doc["updated_at"] = now or utc_now_iso()
    doc["stats"]["labeled"] = int(doc["stats"].get("labeled", 0)) + newly_labeled
    doc["stats"]["pending"] = len(doc["pending"])
    write_json_state(STATE_FILE, doc)
    return {"enabled": True, "recorded": recorded, "labeled": newly_labeled, "pending": len(doc["pending"])}


def refresh_labels(
    config: dict[str, Any],
    *,
    history_loader: HistoryLoader | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Label matured pending opportunities; safe no-op outside paper mode."""
    cfg = _settings(config)
    if not cfg["enabled"] or not _paper_only(config):
        return {"enabled": False, "recorded": 0, "labeled": 0}
    return _refresh_doc(_load_doc(), config, cfg, history_loader=history_loader, now=now)
