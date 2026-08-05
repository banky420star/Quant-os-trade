"""Reset the data-lab strategy experiment from a clean demo-account slate.

Default mode clears derived learning/evaluation state only. ``--flatten-demo``
additionally cancels this bot's pending orders and closes this bot's open
positions, but only after verifying the connected MT5 account is DEMO and only
for the configured magic number. It never targets other manual/EA trades.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
STATE = ROOT / "state"

RESET_FILES = (
    "trade_log.json", "trade_log_meta.json", "mt5_trades.json", "mt5_orders.json",
    "mt5_positions.json", "edge_scores.json", "edge_database.json", "memory.json",
    "learning_state.json", "learning_monitor.json", "strategy_arena.json",
    "specialized_setup_report.json", "forward_test_ledger.json", "policy_scores.json",
    "best_policies.json", "position_management.json", "position_confidence.json",
    "kill_switch.json", "risk_state.json", "daily_growth.json", "equity_history.json", "candidate_signals.json", "approved_signals.json",
    "rejected_signals.json", "evaluated_signals.json", "specialized_shadow_report.json",
    "specialized_shadow_ledger.jsonl", "forward_opportunities.json", "fast_mode_state.json",
    "fast_mode_runtime.json", "fast_mode_guard.json", "fast_mode_decisions.json",
)



def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _assert_bot_stopped() -> None:
    """Prevent a reset race with the order-producing bot process."""
    try:
        import psutil
    except ImportError:
        return
    current = __import__("os").getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        if proc.info.get("pid") == current:
            continue
        cmd = " ".join(proc.info.get("cmdline") or []).lower()
        if "start.py" in cmd or "run_all.py" in cmd:
            raise RuntimeError(
                "Bot process is running; stop it before a fresh reset to prevent "
                "new orders or ledger writes racing the reset."
            )


def _flatten_demo(config: dict) -> dict[str, int]:
    """Cancel/close only this bot's demo MT5 objects; refuse all other accounts."""
    _assert_bot_stopped()
    try:
        import MetaTrader5 as mt5
        from core.mt5_broker import MT5Broker
        from core.mt5_connection_manager import MT5ConnectionManager
        from core.symbol_manager import logical_symbol
        from core.utils import setup_logger
    except ImportError as exc:
        raise RuntimeError(f"MT5 package unavailable: {exc}") from exc

    logger = setup_logger("reset_data_lab_experiment", "reset_data_lab_experiment.log")
    conn = MT5ConnectionManager(config, logger)
    conn.connect()
    try:
        account = mt5.account_info()
        if account is None or int(getattr(account, "trade_mode", -1)) != 0:
            raise RuntimeError("Refusing flatten: connected MT5 account is not DEMO")
        magic = int((config.get("execution") or {}).get("magic_number", 20250625))
        orders = [o for o in (mt5.orders_get() or []) if int(getattr(o, "magic", -1)) == magic]
        positions = [p for p in (mt5.positions_get() or []) if int(getattr(p, "magic", -1)) == magic]
        cancelled = 0
        closed = 0
        failures = 0

        for order in orders:
            request = {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": int(order.ticket),
                "symbol": str(order.symbol),
                "magic": magic,
            }
            result = mt5.order_send(request)
            if result is not None and int(result.retcode) == int(getattr(mt5, "TRADE_RETCODE_DONE", 10009)):
                cancelled += 1
            else:
                failures += 1
                logger.error("Cancel failed order=%s result=%s", order.ticket, result)

        broker = MT5Broker(config, logger)
        for pos in positions:
            logical = logical_symbol(str(pos.symbol))
            side = "BUY" if int(pos.type) == int(mt5.POSITION_TYPE_BUY) else "SELL"
            result = broker.close_position(
                int(pos.ticket), logical, side, float(pos.volume), reason="experiment_reset",
            )
            if result.get("success"):
                closed += 1
            else:
                failures += 1
                logger.error("Close failed position=%s result=%s", pos.ticket, result)

        remaining_orders = [o for o in (mt5.orders_get() or []) if int(getattr(o, "magic", -1)) == magic]
        remaining_positions = [p for p in (mt5.positions_get() or []) if int(getattr(p, "magic", -1)) == magic]
        if remaining_orders or remaining_positions or failures:
            raise RuntimeError(
                f"Flatten incomplete: remaining_orders={len(remaining_orders)} "
                f"remaining_positions={len(remaining_positions)} failures={failures}"
            )
        return {"cancelled": cancelled, "closed": closed, "failures": failures}
    finally:
        conn.disconnect()


def reset(*, backup: bool = True, flatten_demo: bool = False) -> dict[str, object]:
    from core.utils import load_config

    config = load_config()
    if config.get("active_profile") != "data-lab":
        raise RuntimeError(
            "Fresh data-lab reset requires active_profile=data-lab; "
            f"got {config.get('active_profile')!r}"
        )
    if config.get("execution", {}).get("mode") != "mt5":
        raise RuntimeError("Fresh data-lab reset requires execution.mode=mt5")
    flatten_result = {"cancelled": 0, "closed": 0, "failures": 0}
    if flatten_demo:
        flatten_result = _flatten_demo(config)

    if not flatten_demo:
        _assert_bot_stopped()
    now = _utc_now()
    # SQLite is a durable mirror of the JSON ledgers. Remove it with the
    # derived files so a fresh run cannot silently repopulate old evidence.
    sqlite_db = ROOT / "quant_os.db"
    if sqlite_db.exists():
        if backup:
            backup_dir = STATE / "backup_pre_data_lab_reset"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = now.replace(":", "").replace("+00:00", "Z")
            shutil.copy2(sqlite_db, backup_dir / f"quant_os.db.{stamp}.bak")
        sqlite_db.unlink()
    backup_dir = STATE / "backup_pre_data_lab_reset"
    if backup:
        backup_dir.mkdir(parents=True, exist_ok=True)
    cleared: list[str] = []
    missing: list[str] = []
    for name in RESET_FILES:
        src = STATE / name
        if not src.exists():
            missing.append(name)
            continue
        if backup:
            stamp = now.replace(":", "").replace("+00:00", "Z")
            shutil.copy2(src, backup_dir / f"{name}.{stamp}.bak")
        src.unlink()
        cleared.append(name)

    marker = {
        "reset_at": now,
        "profile": "data-lab",
        "scope": "derived_ledgers_only" if not flatten_demo else "derived_ledgers_and_bot_magic_demo_objects",
        "live_mt5_orders_touched": bool(flatten_demo),
        "live_mt5_positions_touched": bool(flatten_demo),
        "flatten": flatten_result,
    }
    (STATE / "experiment_reset.json").write_text(json.dumps(marker, indent=2), encoding="utf-8")
    return {"reset_at": now, "cleared": cleared, "missing": missing, "backup_dir": str(backup_dir) if backup else None, **flatten_result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flatten-demo", action="store_true", help="Cancel/close only this bot's demo MT5 objects before resetting")
    parser.add_argument("--no-backup", action="store_true", help="Do not copy old derived ledgers before clearing them")
    args = parser.parse_args()
    result = reset(backup=not args.no_backup, flatten_demo=bool(args.flatten_demo))
    print(f"Fresh data-lab reset at {result['reset_at']}")
    print(f"Cleared {len(result['cleared'])} files; missing {len(result['missing'])} files")
    if args.flatten_demo:
        print(f"Flattened bot demo objects: cancelled={result['cancelled']} closed={result['closed']}")
    else:
        print("No MT5 orders or positions touched. Use --flatten-demo after confirming the demo account.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
