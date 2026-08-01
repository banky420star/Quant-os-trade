"""Agent 6: Risk Loop — exposure, drawdown, kill switch.

In mt5 mode this loop re-reads the live account snapshot from MT5 and refreshes
``state/account.json`` *before* running the risk checks. Missing, stale, or
inconsistent account state keeps the kill switch ON rather than silently
showing fake drawdown numbers.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.equity_tracker import record_snapshot
from core.mt5_connection_manager import MT5ConnectionManager
from core.risk_manager import RiskManager
from core.utils import load_config, read_json_state, setup_logger, utc_now_iso, write_json_state


def _refresh_mt5_account(
    config: dict[str, Any],
    logger: logging.Logger,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Refresh state/account.json from a live MT5 connection.

    Returns ``(account_context, snapshot)`` where ``account_context`` is fed into
    ``RiskManager.evaluate`` and ``snapshot`` is the raw MT5 payload (or ``{}``
    when the connection could not be established).
    """
    expected_mode = config.get("mt5", {}).get("account_mode", "real")
    context: dict[str, Any] = {
        "expected_account_mode": expected_mode,
        "actual_account_mode": None,
        "status": "ok",
        "account_error": None,
    }

    snapshot: dict[str, Any] = {}
    connection = MT5ConnectionManager(config, logger)
    try:
        connection.connect()
        try:
            snapshot = connection.account_snapshot() or {}
            login = snapshot.get("login")
            server = snapshot.get("server")
            actual_mode = snapshot.get("account_mode")

            if not login or not server:
                context["status"] = "missing"
                context["account_error"] = "account snapshot missing login/server"
            elif actual_mode == "unknown" or actual_mode is None:
                context["status"] = "unknown"
                context["account_error"] = "unknown account mode from MT5"
            elif snapshot.get("equity") is None:
                # Login/server/mode all present but equity missing -> still fail
                # safe so the risk loop does not silently fall back to starting
                # cash as equity.
                context["status"] = "missing"
                context["account_error"] = "account snapshot missing equity"
            else:
                context["actual_account_mode"] = actual_mode
                payload = {"timestamp": utc_now_iso(), **snapshot}
                # Surface the previous-snapshot staleness so operators see it
                # without having to dig into disk state.
                prior = read_json_state("account.json", default={})
                if prior and not MT5ConnectionManager.is_account_fresh(
                    prior, max_age_seconds=90
                ):
                    logger.warning(
                        "Previous account.json was stale (login=%s); refreshing from MT5",
                        prior.get("login"),
                    )
                write_json_state("account.json", payload)
                logger.info(
                    "Refreshed account.json from MT5: login=%s server=%s mode=%s equity=%.2f",
                    login,
                    server,
                    actual_mode,
                    float(snapshot.get("equity", 0) or 0),
                )
        finally:
            connection.disconnect()
    except (ConnectionError, RuntimeError, OSError) as exc:
        logger.error("MT5 account refresh failed: %s", exc)
        context["status"] = "failed"
        context["account_error"] = f"connection_failed: {exc}"[:200]

    return context, snapshot


def run() -> dict:
    """Evaluate portfolio risk and update kill switch."""
    config = load_config()
    logger = setup_logger("risk_loop", "risk_loop.log")
    logger.info("Starting risk loop")

    positions_data = read_json_state("paper_positions.json", default={"positions": []})
    orders_data = read_json_state("paper_orders.json", default={"orders": [], "balance": {}})
    trades_data = read_json_state("paper_trades.json", default={"trades": []})
    features = read_json_state("features.json", default={"symbols": {}})
    kill_existing = read_json_state("kill_switch.json", default={"kill_switch": config["risk"].get("kill_switch", False)})

    positions = positions_data.get("positions", [])
    orders = orders_data.get("orders", [])
    balance = dict(orders_data.get("balance", {}))
    trades = trades_data.get("trades", [])

    account_context: dict[str, Any] = {}

    if config.get("execution", {}).get("mode") == "mt5":
        # MT5-owned state must never influence paper-mode risk calculations.
        baseline = read_json_state("mt5_baseline.json", default={})
        if baseline.get("starting_cash") is not None:
            balance["starting_cash"] = float(baseline["starting_cash"])

        mt5_ctx, snapshot = _refresh_mt5_account(config, logger)
        account_context = mt5_ctx

        # Use fresh MT5 values when the snapshot is valid. Persisted account.json
        # is refreshed for observability only and is never consumed in paper mode.
        if snapshot.get("equity") is not None and not mt5_ctx.get("account_error"):
            balance["equity"] = float(snapshot["equity"])
        if snapshot.get("balance") is not None and not mt5_ctx.get("account_error"):
            balance["cash"] = float(snapshot["balance"])

    manager = RiskManager(config, logger)
    result = manager.evaluate(positions, orders, balance, trades, features, kill_existing, account_context=account_context)

    write_json_state("risk_state.json", result["risk_state"])
    write_json_state("kill_switch.json", result["kill_switch"])
    record_snapshot(
        result["risk_state"].get("equity", balance.get("equity", 0)),
        result["risk_state"].get("cash", balance.get("cash")),
        source="risk_loop",
        extra={
            "drawdown": result["risk_state"].get("drawdown"),
            "open_positions": result["risk_state"].get("open_positions"),
            "exposure_used_pct": result["risk_state"].get("exposure_used_pct"),
            "account_state": result["risk_state"].get("account_state"),
        },
    )
    logger.info("Risk state saved — kill_switch=%s", result["kill_switch"]["kill_switch"])
    return result


if __name__ == "__main__":
    run()