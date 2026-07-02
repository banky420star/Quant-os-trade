"""Blue Guardian enforcement loop — floating loss closes and daily state."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.blue_guardian import (
    blue_guardian_enabled,
    blue_guardian_settings,
    can_close_position,
    evaluate_daily_state,
    excess_position_close_actions,
    per_trade_close_actions,
    portfolio_close_all,
    prune_open_times,
    total_floating_pnl,
)
from core.mt5_broker import MT5Broker
from core.mt5_connection_manager import MT5ConnectionManager
from core.position_sync import fetch_mt5_agent_positions
from core.utils import load_config, read_json_state, setup_logger, utc_now_iso, write_json_state


def _close_actions_mt5(broker: MT5Broker, actions: list[dict], logger) -> list[dict]:
    results = []
    for act in actions:
        ok, hold_reason = can_close_position(
            broker.config,
            {"ticket": act["ticket"], "opened_at": act.get("opened_at")},
            reason=act["reason"],
        )
        if not ok:
            logger.info("BG skip close #%s — %s", act["ticket"], hold_reason)
            continue
        cr = broker.close_position(
            int(act["ticket"]),
            act["symbol"],
            act["side"],
            float(act["volume"]),
            reason=act["reason"],
        )
        results.append({**act, "success": cr.get("success"), "error": cr.get("error")})
        if cr.get("success"):
            logger.warning("BG closed #%s %s profit=%.2f reason=%s", act["ticket"], act["symbol"], act["profit"], act["reason"])
    return results


def run() -> dict:
    config = load_config()
    logger = setup_logger("blue_guardian_loop", "blue_guardian_loop.log")
    if not blue_guardian_enabled(config):
        return {"enabled": False}

    mode = config.get("execution", {}).get("mode", "paper")
    account = read_json_state("account.json", default={})
    balance = float(account.get("balance", 0) or 0)
    equity = float(account.get("equity", balance) or balance)
    daily = evaluate_daily_state(config, balance=balance, equity=equity)

    positions: list[dict] = []
    close_results: list[dict] = []
    portfolio_action = None

    if mode == "mt5":
        connection = MT5ConnectionManager(config, logger)
        try:
            connection.connect()
            positions = fetch_mt5_agent_positions(config, logger)
            broker = MT5Broker(config, logger)
            portfolio_action = portfolio_close_all(config, positions)
            if portfolio_action:
                logger.warning(
                    "BG PORTFOLIO FLATTEN floating=%.2f threshold=%.2f",
                    portfolio_action["floating_pnl"],
                    portfolio_action["threshold"],
                )
                for pos in positions:
                    _close_actions_mt5(
                        broker,
                        [{
                            "ticket": pos["ticket"],
                            "symbol": pos["symbol"],
                            "side": pos["side"],
                            "volume": pos["size"],
                            "profit": pos.get("profit", 0),
                            "reason": portfolio_action["reason"],
                            "emergency": True,
                            "opened_at": pos.get("opened_at"),
                        }],
                        logger,
                    )
                positions = fetch_mt5_agent_positions(config, logger)
            else:
                excess_actions = excess_position_close_actions(config, positions)
                if excess_actions:
                    logger.warning(
                        "BG excess positions: %d open > max %d — closing %d",
                        len(positions),
                        blue_guardian_settings(config)["max_total_open_positions"],
                        len(excess_actions),
                    )
                    close_results = _close_actions_mt5(broker, excess_actions, logger)
                    positions = fetch_mt5_agent_positions(config, logger)
                actions = per_trade_close_actions(config, positions)
                close_results.extend(_close_actions_mt5(broker, actions, logger))
        finally:
            connection.disconnect()
    else:
        pos_data = read_json_state("paper_positions.json", default={"positions": []})
        positions = list(pos_data.get("positions", []))

    floating = total_floating_pnl(positions)
    shield_usd = float((daily.get("guardian_shield_usd") if daily else -50) or -50)
    prev_bg = read_json_state("blue_guardian.json", default={}) or {}
    shield_breaches = int(prev_bg.get("guardian_shield_breaches", 0) or 0)
    if floating <= shield_usd:
        shield_breaches += 1
        daily["guardian_shield_breaches"] = shield_breaches
        write_json_state("blue_guardian.json", {**prev_bg, **daily, "guardian_shield_breaches": shield_breaches})

    tickets = {str(p.get("ticket") or p.get("position_id")) for p in positions}
    prune_open_times(tickets)

    summary = {
        "enabled": True,
        "timestamp": utc_now_iso(),
        "floating_pnl": floating,
        "open_positions": len(positions),
        "daily": daily,
        "portfolio_action": portfolio_action,
        "per_trade_closes": close_results,
    }
    write_json_state("blue_guardian_actions.json", summary)

    if daily.get("trading_paused"):
        kill = read_json_state("kill_switch.json", default={"kill_switch": False})
        kill["kill_switch"] = True
        kill["reason"] = daily.get("pause_reason") or "Blue Guardian daily pause"
        kill["activated_at"] = kill.get("activated_at") or utc_now_iso()
        write_json_state("kill_switch.json", kill)

    logger.info(
        "Blue Guardian: floating=%.2f positions=%d paused=%s closes=%d",
        floating,
        len(positions),
        daily.get("trading_paused"),
        len(close_results),
    )
    return summary


if __name__ == "__main__":
    run()