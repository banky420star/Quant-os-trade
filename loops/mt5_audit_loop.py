"""Read-only MT5 audit loop.

Collects account/position/order/history snapshots and summarizes the terminal
journal. It deliberately never calls order_send, order_check, or any mutation
API. The output is bounded and written to state/mt5_audit.json plus
state/mt5_calendar.json for dashboard/API consumers.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.mt5_connection_manager import MT5ConnectionManager
from core.mt5_terminal_manager import MT5TerminalManager
from core.news_calendar import news_context_at
from core.utils import ensure_dirs, load_config, setup_logger, utc_now_iso, write_json_state

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover - exercised through the unavailable path
    mt5 = None  # type: ignore

_SENSITIVE = re.compile(r"(?i)(password|passwd|login|account|server|authorization|token)\s*[:=]\s*[^,; ]+")
_ERROR_WORDS = re.compile(r"(?i)error|failed|failure|reject|invalid|cannot|denied|timeout|critical")
_TRADE_WORDS = re.compile(r"(?i)order|deal|position|trade|margin|stop.?loss|take.?profit")


def _safe_text(value: Any, limit: int = 240) -> str:
    text = _SENSITIVE.sub(lambda m: f"{m.group(1)}=<redacted>", str(value))
    return text[:limit]


def _field(obj: Any, name: str, default: Any = None) -> Any:
    value = getattr(obj, name, default)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _utc_from_epoch(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _deal_row(deal: Any) -> dict[str, Any]:
    return {
        "ticket": _field(deal, "ticket"),
        "order": _field(deal, "order"),
        "position_id": _field(deal, "position_id"),
        "symbol": _field(deal, "symbol"),
        "type": _field(deal, "type"),
        "entry": _field(deal, "entry"),
        "reason": _field(deal, "reason"),
        "volume": _field(deal, "volume"),
        "price": _field(deal, "price"),
        "profit": _field(deal, "profit", 0.0),
        "commission": _field(deal, "commission", 0.0),
        "swap": _field(deal, "swap", 0.0),
        "time": _utc_from_epoch(_field(deal, "time")),
    }


def _order_row(order: Any) -> dict[str, Any]:
    return {
        "ticket": _field(order, "ticket"),
        "order": _field(order, "order"),
        "position_id": _field(order, "position_id"),
        "symbol": _field(order, "symbol"),
        "type": _field(order, "type"),
        "state": _field(order, "state"),
        "reason": _field(order, "reason"),
        "volume_initial": _field(order, "volume_initial"),
        "volume_current": _field(order, "volume_current"),
        "price_open": _field(order, "price_open"),
        "time_setup": _utc_from_epoch(_field(order, "time_setup")),
        "time_done": _utc_from_epoch(_field(order, "time_done")),
    }


def _journal_files(data_path: str | Path | None) -> list[Path]:
    if not data_path:
        return []
    root = Path(data_path)
    candidates = [root / "logs", root / "MQL5" / "Logs"]
    files: list[Path] = []
    for directory in candidates:
        if directory.exists():
            files.extend(p for p in directory.glob("*.log") if p.is_file())
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def summarize_journal(data_path: str | Path | None, *, max_files: int = 4, tail_lines: int = 5000) -> dict[str, Any]:
    """Return bounded journal counts and redacted samples, never raw credentials."""
    counts: Counter[str] = Counter()
    files: list[dict[str, Any]] = []
    samples: list[str] = []
    for path in _journal_files(data_path)[:max_files]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-tail_lines:]
            size = path.stat().st_size
        except OSError:
            continue
        files.append({"name": path.name, "bytes": size, "lines_scanned": len(lines)})
        for line in lines:
            if _ERROR_WORDS.search(line):
                counts["error_or_rejection"] += 1
                if len(samples) < 12:
                    samples.append(_safe_text(line))
            if _TRADE_WORDS.search(line):
                counts["trade_related"] += 1
    return {"files": files, "counts": dict(counts), "samples": samples}


def _calendar_snapshot(config: dict[str, Any], now: datetime) -> dict[str, Any]:
    native = [name for name in dir(mt5) if name.startswith("calendar_")] if mt5 else []
    context = news_context_at(now, config)
    # Function-name presence is only capability detection. We do not call a
    # native calendar function here, so labeling the events as native would be
    # misleading. Keep the actual event source explicit.
    return {
        "timestamp": utc_now_iso(),
        "native_mt5_api_available": bool(native),
        "native_functions": native,
        "source": "constructed_local_calendar",
        "events": context.get("macro_events", []),
        "current_context": context,
        "note": (
            "Native MT5 calendar functions detected but not queried by this read-only loop."
            if native else "Native MT5 calendar API is unavailable in this terminal Python package; events are the repository's constructed fallback."
        ),
    }


def _read_rows(api_name: str, loader: Any, *args: Any) -> list[Any]:
    """Read an MT5 collection without turning an API failure into empty data."""
    rows = loader(*args)
    if rows is not None:
        return list(rows)
    last_error = mt5.last_error() if mt5 and hasattr(mt5, "last_error") else "unknown MT5 error"
    raise RuntimeError(f"{api_name} failed: {_safe_text(last_error)}")


def _empty_report(reason: str, error: str | None = None) -> dict[str, Any]:
    return {
        "timestamp": utc_now_iso(),
        "status": "unavailable",
        "read_only": True,
        "orders_submitted": 0,
        "reason": reason,
        "error": _safe_text(error) if error else None,
        "account": None,
        "open_positions": [],
        "pending_orders": [],
        "recent_deals": [],
        "recent_orders": [],
        "journal": {"files": [], "counts": {}, "samples": []},
    }


def run(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Collect a read-only snapshot and return supervisor-compatible status."""
    ensure_dirs()
    cfg = config or load_config()
    logger = setup_logger("mt5_audit_loop", "mt5_audit_loop.log")
    loop_cfg = cfg.get("mt5_audit_loop") or {}
    if not loop_cfg.get("enabled", True):
        report = _empty_report("disabled")
        write_json_state("mt5_audit.json", report)
        write_json_state("mt5_calendar.json", {
            "timestamp": utc_now_iso(),
            "source": "disabled",
            "native_mt5_api_available": bool(mt5 and any(name.startswith("calendar_") for name in dir(mt5))),
            "events": [],
            "note": "MT5 audit loop is disabled by configuration.",
        })
        return {"mt5_audit_loop": "OK"}

    if mt5 is None:
        report = _empty_report("MetaTrader5 package is not installed")
        write_json_state("mt5_audit.json", report)
        write_json_state("mt5_calendar.json", {
            "timestamp": utc_now_iso(),
            "source": "unavailable",
            "native_mt5_api_available": False,
            "events": [],
            "note": "MetaTrader5 package is not installed.",
        })
        return {"mt5_audit_loop": "OK"}

    now = datetime.now(timezone.utc)
    days = max(1, min(int(loop_cfg.get("history_days", 30)), 365))
    max_recent = max(1, min(int(loop_cfg.get("max_recent_rows", 100)), 500))
    connection: MT5ConnectionManager | None = None
    owns_connection = False
    try:
        # MT5's Python binding is process-global. Reuse an already-connected
        # session and never shut it down; only initialize a session ourselves
        # when no account snapshot is available. The shared trade lock keeps
        # this read batch away from broker mutations in this process.
        with MT5TerminalManager._shared_trade_lock:
            account = mt5.account_info()
            if account is None:
                connection = MT5ConnectionManager(cfg, logger)
                connection.connect()
                owns_connection = True
                account = mt5.account_info()
            if account is None:
                raise RuntimeError("MT5 account_info unavailable after connect")
            terminal = mt5.terminal_info()
            positions = _read_rows("positions_get", mt5.positions_get)
            pending = _read_rows("orders_get", mt5.orders_get)
            start = now - timedelta(days=days)
            deals = _read_rows("history_deals_get", mt5.history_deals_get, start, now)
            orders = _read_rows("history_orders_get", mt5.history_orders_get, start, now)
        data_path = getattr(terminal, "data_path", None) if terminal else None
        report = {
            "timestamp": utc_now_iso(),
            "status": "ok",
            "read_only": True,
            "orders_submitted": 0,
            "history_window_days": days,
            "account": {
                "balance": _field(account, "balance"),
                "equity": _field(account, "equity"),
                "profit": _field(account, "profit"),
                "currency": _field(account, "currency"),
                "trade_allowed": _field(account, "trade_allowed"),
                "trade_expert": _field(account, "trade_expert"),
                "server": _field(account, "server"),
                "account_mode": {0: "demo", 1: "contest", 2: "real"}.get(_field(account, "trade_mode"), "unknown"),
            },
            "terminal": {
                "connected": _field(terminal, "connected"),
                "trade_allowed": _field(terminal, "trade_allowed"),
                "build": _field(terminal, "build"),
            },
            "open_positions": [{
                "ticket": _field(p, "ticket"), "symbol": _field(p, "symbol"),
                "type": _field(p, "type"), "volume": _field(p, "volume"),
                "price_open": _field(p, "price_open"), "profit": _field(p, "profit"),
                "time": _utc_from_epoch(_field(p, "time")),
            } for p in positions[:max_recent]],
            "pending_orders": [_order_row(o) for o in pending[:max_recent]],
            "recent_deals": [_deal_row(d) for d in deals[-max_recent:]],
            "recent_orders": [_order_row(o) for o in orders[-max_recent:]],
            "counts": {
                "open_positions": len(positions), "pending_orders": len(pending),
                "deals": len(deals), "historical_orders": len(orders),
                "deal_symbols": sorted({_field(d, "symbol") for d in deals if _field(d, "symbol")}),
            },
            "journal": summarize_journal(
                data_path,
                max_files=max(1, min(int(loop_cfg.get("journal_files", 4)), 12)),
                tail_lines=max(100, min(int(loop_cfg.get("journal_tail_lines", 5000)), 20000)),
            ),
        }
        calendar = _calendar_snapshot(cfg, now)
        write_json_state("mt5_audit.json", report)
        write_json_state("mt5_calendar.json", calendar)
        logger.info("MT5 audit: deals=%d orders=%d open=%d pending=%d journal_errors=%d", len(deals), len(orders), len(positions), len(pending), report["journal"]["counts"].get("error_or_rejection", 0))
        return {"mt5_audit_loop": "OK"}
    except Exception as exc:
        logger.warning("MT5 audit unavailable: %s", exc)
        report = _empty_report("connection_or_read_failure", str(exc))
        write_json_state("mt5_audit.json", report)
        write_json_state("mt5_calendar.json", {
            "timestamp": utc_now_iso(),
            "source": "unavailable",
            "native_mt5_api_available": bool(mt5 and any(name.startswith("calendar_") for name in dir(mt5))),
            "events": [],
            "note": "Calendar snapshot was not refreshed because the MT5 audit read failed.",
        })
        return {"mt5_audit_loop": "OK"}
    finally:
        # Only close a session this loop had to create, and do so while the
        # shared lock is still held. An existing trading session is never
        # interrupted by an audit cycle.
        if owns_connection and connection is not None:
            with MT5TerminalManager._shared_trade_lock:
                connection.disconnect()


if __name__ == "__main__":
    run()
