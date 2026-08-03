"""Fast live executor — MT5 orders triggered by the tick-reactive fast layer."""

from __future__ import annotations

import logging
from typing import Any

from core.audit_log import append_event
from core.fast_mode import fast_mode_live, fast_mode_settings
from core.fast_signal_cache import read_fast_state, record_trade_entry, write_fast_state
from core.mt5_broker import MT5Broker
from core.mt5_connection_manager import MT5ConnectionManager
from core.state_store import (
    read_approved_signals,
    read_evaluated_signals,
    sync_store_from_doc,
)
from core.utils import read_json_state, utc_now_iso, write_json_state


def _require_verifier_approval(config: dict[str, Any]) -> bool:
    cfg = fast_mode_settings(config)
    return bool(cfg.get("require_verifier_approval", True))


def _find_evaluated_signal(signal_id: str, config: dict[str, Any]) -> dict[str, Any] | None:
    doc = read_evaluated_signals(config) or read_json_state("evaluated_signals.json", default={})
    for sig in doc.get("evaluated") or []:
        if sig.get("signal_id") == signal_id:
            return dict(sig)
    return None


def _is_verifier_approved(signal_id: str, config: dict[str, Any]) -> bool:
    doc = read_approved_signals(config) or read_json_state("approved_signals.json", default={})
    for row in doc.get("approved") or []:
        sig = row.get("signal", row)
        if sig.get("signal_id") == signal_id:
            return True
    return False


def _executed_ids(config: dict[str, Any], state: dict[str, Any]) -> set[str]:
    orders = read_json_state("mt5_orders.json", default={"orders": []}).get("orders") or []
    from_orders = {o["signal_id"] for o in orders if o.get("signal_id")}
    from_state = set(state.get("executed_signals") or [])
    return from_orders | from_state


def _mark_executed(signal_id: str, state: dict[str, Any]) -> None:
    executed = list(state.get("executed_signals") or [])
    if signal_id not in executed:
        executed.append(signal_id)
    state["executed_signals"] = executed[-100:]
    write_fast_state(state)


def _prepare_signal(
    signal: dict[str, Any],
    cache_entry: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    """Merge cache anchor + management profile into the evaluated signal."""
    out = dict(signal)
    entry_type = str(decision.get("entry_type") or cache_entry.get("entry_type") or "limit")
    action = str(decision.get("action") or "")
    if action == "enter_market" or (entry_type == "market" and action != "enter_limit"):
        out["entry_mode"] = "market"
    else:
        out["entry_mode"] = "limit"
        anchor = float(cache_entry.get("anchor") or out.get("entry") or 0)
        if anchor > 0:
            out["entry"] = anchor
        out["within_reach"] = True
    mgmt = cache_entry.get("management_profile")
    if isinstance(mgmt, dict) and mgmt:
        out["management_profile"] = mgmt
    out["fast_mode"] = True
    return out


def _persist_mt5_result(config: dict[str, Any], result: dict[str, Any]) -> None:
    ts = result.get("timestamp") or utc_now_iso()
    orders_doc = {
        "timestamp": ts,
        "mode": "mt5",
        "balance": result.get("balance"),
        "account": result.get("account"),
        "orders": result.get("orders", []),
    }
    positions_doc = {
        "timestamp": ts,
        "mode": "mt5",
        "positions": result.get("positions", []),
    }
    trades_doc = {
        "timestamp": ts,
        "mode": "mt5",
        "trades": result.get("trades", []),
    }
    # MT5 namespace: persists to mt5_* so paper_* stays clean.
    # Dashboard reads from mt5_* when mode is mt5 (see dashboard/server.py).
    write_json_state("mt5_orders.json", orders_doc)
    write_json_state("mt5_positions.json", positions_doc)
    write_json_state("mt5_trades.json", trades_doc)
    sync_store_from_doc(config, "orders", orders_doc)
    sync_store_from_doc(config, "positions", positions_doc)
    sync_store_from_doc(config, "trades", trades_doc)


def execute_fast_entry(
    decision: dict[str, Any],
    cache_entry: dict[str, Any],
    *,
    config: dict[str, Any],
    state: dict[str, Any] | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Place a single MT5 order when fast tick fires enter_*."""
    log = logger or logging.getLogger("fast_live_executor")
    state = state if state is not None else read_fast_state()

    if not fast_mode_live(config):
        return {"skipped": True, "reason": "observe_only"}

    action = str(decision.get("action") or "")
    if not action.startswith("enter"):
        return {"skipped": True, "reason": "not_enter_action"}

    signal_id = str(cache_entry.get("signal_id") or "")
    symbol = str(cache_entry.get("symbol") or decision.get("symbol") or "")
    if not signal_id:
        return {"blocked": True, "reason": "missing_signal_id"}

    exec_cfg = config.get("execution") or {}
    if exec_cfg.get("mode") != "mt5":
        return {"blocked": True, "reason": "execution_mode_not_mt5"}
    if not exec_cfg.get("live_trading_enabled"):
        return {"blocked": True, "reason": "live_trading_disabled"}

    kill = read_json_state("kill_switch.json", default={})
    if kill.get("kill_switch"):
        return {"blocked": True, "reason": f"kill_switch:{kill.get('reason')}"}

    bg = read_json_state("blue_guardian.json", default={})
    if bg.get("enabled") and bg.get("trading_paused"):
        return {"blocked": True, "reason": "blue_guardian_pause"}

    if _require_verifier_approval(config) and not _is_verifier_approved(signal_id, config):
        return {"blocked": True, "reason": "not_verifier_approved"}

    if signal_id in _executed_ids(config, state):
        return {"blocked": True, "reason": "already_executed"}

    signal = _find_evaluated_signal(signal_id, config)
    if not signal:
        return {"blocked": True, "reason": "evaluated_signal_missing"}

    signal = _prepare_signal(signal, cache_entry, decision)
    orders = list(read_json_state("mt5_orders.json", default={"orders": []}).get("orders") or [])
    positions = list(read_json_state("mt5_positions.json", default={"positions": []}).get("positions") or [])
    trades = list(read_json_state("mt5_trades.json", default={"trades": []}).get("trades") or [])
    executed = _executed_ids(config, state)

    connection = MT5ConnectionManager(config, log)
    try:
        connection.connect()
        broker = MT5Broker(config, log)
        result = broker.process_approved_signals(
            [{"signal": signal}],
            existing_orders=orders,
            existing_trades=trades,
            existing_positions=positions,
            executed_signal_ids=executed,
        )
        _persist_mt5_result(config, result)

        placed = list(result.get("placed") or [])
        errors = list(result.get("errors") or [])
        if placed:
            _mark_executed(signal_id, state)
            record_trade_entry(symbol, state)
            for order in placed:
                append_event(
                    "fast_mode.order_placed",
                    symbol=symbol,
                    details={
                        "signal_id": signal_id,
                        "ticket": order.get("ticket"),
                        "side": order.get("side"),
                        "volume": order.get("volume"),
                        "entry_type": decision.get("entry_type"),
                    },
                )
            log.info(
                "FAST LIVE placed %s %s ticket=%s",
                symbol,
                signal.get("side"),
                placed[0].get("ticket"),
            )
            return {"success": True, "placed": placed, "errors": errors}

        err_msg = errors[0].get("error") if errors else "order_failed"
        append_event(
            "fast_mode.order_error",
            symbol=symbol,
            details={"signal_id": signal_id, "error": err_msg},
        )
        log.warning("FAST LIVE failed %s: %s", symbol, err_msg)
        return {"success": False, "placed": [], "errors": errors, "error": err_msg}
    except Exception as exc:
        log.error("FAST LIVE exception %s: %s", symbol, exc)
        append_event("fast_mode.order_error", symbol=symbol, details={"error": str(exc)})
        return {"success": False, "error": str(exc)}
    finally:
        try:
            connection.disconnect()
        except Exception:
            pass