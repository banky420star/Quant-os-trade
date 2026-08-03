"""Agent 6: Risk Loop — exposure, drawdown, kill switch."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.blue_guardian import blue_guardian_enabled, evaluate_daily_state
from core.equity_tracker import record_snapshot
from core.risk_manager import RiskManager
from core.utils import (
    load_config,
    read_json_state,
    setup_logger,
    utc_now_iso,
    write_json_state,
)


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

    if config.get("execution", {}).get("mode") == "mt5":
        baseline = read_json_state("mt5_baseline.json", default={})
        account = read_json_state("account.json", default={})
        # Refresh account.json from the live terminal every cycle. data_loop
        # only writes it once in startup(), so without this the equity graph
        # and risk math read a frozen balance for the whole session.
        try:
            from core.mt5_connection_manager import MT5ConnectionManager

            conn = MT5ConnectionManager(config, logger)
            try:
                conn.connect()
                live = conn.account_snapshot()
                if live.get("login") is not None and live.get("balance") is not None:
                    account = {**account, **live}
                    account["timestamp"] = utc_now_iso()
                    write_json_state("account.json", account)
            finally:
                conn.disconnect()
        except Exception as exc:
            logger.warning(
                "Live account refresh failed — using cached account.json: %s", exc,
            )
        acct_login = account.get("login")
        if (
            acct_login is not None
            and baseline.get("login") is not None
            and int(acct_login) != int(baseline["login"])
            and account.get("balance") is not None
        ):
            from core.daily_growth import reset_daily_growth_baseline
            from core.utils import utc_now_iso as _now

            eq = float(account.get("equity") or account["balance"])
            cash = float(account["balance"])
            baseline = {
                "login": int(acct_login),
                "server": account.get("server", ""),
                "starting_cash": cash,
                "set_at": _now(),
                "source": "risk_loop_login_change",
            }
            write_json_state("mt5_baseline.json", baseline)
            reset_daily_growth_baseline(eq, config)
            kill_existing = {"kill_switch": False, "reason": None, "activated_at": None}
            logger.info(
                "MT5 login changed -> rebaselined to $%.2f and cleared kill switch",
                cash,
            )
        if baseline.get("starting_cash"):
            balance["starting_cash"] = float(baseline["starting_cash"])
        if account.get("equity") is not None:
            balance["equity"] = float(account["equity"])
        if account.get("balance") is not None:
            balance["cash"] = float(account["balance"])

    symbol_specs_data = read_json_state("symbol_specs.json", default={})
    symbol_specs = symbol_specs_data.get("specs") or None

    manager = RiskManager(config, logger)
    result = manager.evaluate(
        positions,
        orders,
        balance,
        trades,
        features,
        kill_existing,
        symbol_specs=symbol_specs,
    )

    if blue_guardian_enabled(config):
        eq = float(balance.get("equity", 0) or 0)
        cash = float(balance.get("cash", eq) or eq)
        bg = evaluate_daily_state(config, balance=cash, equity=eq)
        result["risk_state"]["blue_guardian"] = bg
        if bg.get("trading_paused"):
            result["kill_switch"] = {
                "kill_switch": True,
                "reason": bg.get("pause_reason") or "Blue Guardian daily pause",
                "activated_at": kill_existing.get("activated_at") or result["kill_switch"].get("activated_at"),
            }

    write_json_state("risk_state.json", result["risk_state"])
    write_json_state("kill_switch.json", result["kill_switch"])
    if result["kill_switch"].get("kill_switch") and result["kill_switch"].get("reason"):
        from core.audit_log import append_event
        from core.ops_alerts import alert_kill_switch

        append_event("risk.kill_switch", details={"reason": result["kill_switch"]["reason"]})
        alert_kill_switch(config, result["kill_switch"]["reason"])
    record_snapshot(
        result["risk_state"].get("equity", balance.get("equity", 0)),
        result["risk_state"].get("cash", balance.get("cash")),
        source="risk_loop",
        extra={
            "drawdown": result["risk_state"].get("drawdown"),
            "open_positions": result["risk_state"].get("open_positions"),
            "exposure_used_pct": result["risk_state"].get("exposure_used_pct"),
        },
    )
    logger.info("Risk state saved — kill_switch=%s", result["kill_switch"]["kill_switch"])
    return result


if __name__ == "__main__":
    run()