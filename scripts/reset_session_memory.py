"""Reset learned session memory while preserving live market/account snapshots."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import load_config, read_json_state, utc_now_iso, write_json_state

DELETE_FILES = (
    "test_e2e_ok.json",
    "weight_candidates.json",
)


def _account_starting_cash(config: dict, account: dict) -> float:
    mode = config.get("execution", {}).get("mode", "paper")
    if mode == "mt5" and account.get("balance") is not None:
        return float(account["balance"])
    return float(config.get("execution", {}).get("starting_cash", 1000))


def reset_session_memory(preserve_safety_gates: bool = True) -> None:
    """Reset learned session memory while preserving live market/account snapshots.

    Args:
        preserve_safety_gates: FAIL-SAFE DEFAULT True. When True, the kill
            switch, risk gates, and position-management state are NOT cleared.
            This blocks the reset path from acting as an UNBLOCK/RESUME bypass:
            resetting learning memory must never re-enable execution that the
            operator stopped or that risk gates disabled, and must never make
            the bot lose management state for live trades. Clearing the gates
            is an explicitly named out-of-band choice (CLI --full / campaign
            resets) — it must never happen because a caller forgot an argument.
    """
    config = load_config()
    now = utc_now_iso()
    account = read_json_state("account.json", default={})
    mode = config.get("execution", {}).get("mode", "paper")
    starting = _account_starting_cash(config, account)
    equity = float(account.get("equity", starting)) if account else starting
    cash = float(account.get("balance", starting)) if account else starting

    write_json_state("memory.json", {
        "timestamp": now,
        "records": [],
        "adjustments": [],
        "total_records": 0,
        "setup_performance": [],
    })
    write_json_state("edge_scores.json", {"setups": {}, "setup_stats": {}})
    write_json_state("edge_database.json", {"records": [], "aggregates": {}})

    write_json_state("paper_trades.json", {"timestamp": now, "trades": []})
    write_json_state("paper_orders.json", {
        "timestamp": now,
        "mode": mode,
        "balance": {"cash": cash, "equity": equity, "starting_cash": starting},
        "account": {},
        "orders": [],
    })
    write_json_state("paper_positions.json", {"timestamp": now, "positions": []})

    write_json_state("candidate_signals.json", {"timestamp": now, "candidates": []})
    write_json_state("approved_signals.json", {"timestamp": now, "approved": []})
    write_json_state("rejected_signals.json", {"timestamp": now, "rejected": []})
    write_json_state("strategy_rankings.json", {"timestamp": now, "rankings": []})
    write_json_state("strategy_arena.json", {})

    if not preserve_safety_gates:
        # Full reset: wipe position-management state too (explicit opt-out).
        write_json_state("position_management.json", {"timestamp": now, "positions": {}})
        # Full reset (scripts / CLI): clear the gates too.
        write_json_state("kill_switch.json", {
            "kill_switch": False,
            "reason": None,
            "activated_at": None,
        })
        write_json_state("risk_state.json", {
            "timestamp": now,
            "kill_switch": False,
            "risk_events": [],
            "total_exposure": 0,
            "symbol_exposure": {},
            "exposure_used_pct": 0.0,
            "max_total_exposure": float(config["risk"]["max_total_exposure_usd"]),
            "max_symbol_exposure": float(config["risk"]["max_symbol_exposure_usd"]),
            "drawdown": 0.0,
            "open_positions": 0,
            "consecutive_losses": 0,
            "equity": round(equity, 2),
            "cash": round(cash, 2),
        })
    else:
        # Phase 0 safety: preserve the operator kill switch and risk gates.
        # Re-read the current kill_switch so a stopped system stays stopped.
        # The full document (including ``source``) is carried through — the
        # risk manager relies on source == "operator" to never clear an
        # operator stop, so it must survive a reset-session call intact.
        _ks = read_json_state("kill_switch.json", default={}) or {}
        preserved = dict(_ks)
        preserved["kill_switch"] = bool(preserved.get("kill_switch", False))
        preserved["updated_at"] = now
        preserved["preserved_by_reset"] = True
        write_json_state("kill_switch.json", preserved)

    write_json_state("equity_history.json", {
        "timestamp": now,
        "count": 1,
        "starting_equity": round(equity, 2),
        "latest_equity": round(equity, 2),
        "points": [{
            "ts": now,
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "balance": round(cash, 2),
            "unrealized_pnl": round(equity - cash, 2),
            "source": "reset_session_memory",
            "drawdown": 0.0,
            "open_positions": 0,
            "exposure_used_pct": 0.0,
        }],
    })

    write_json_state("research_report.json", {"timestamp": now, "status": "reset"})
    write_json_state("adaptive_weights_candidate.json", {"timestamp": now, "weights": {}})
    write_json_state("research_validation.json", {"timestamp": now, "status": "reset"})
    write_json_state("replay_job.json", {"status": "idle", "timestamp": now})
    write_json_state("replay_results.json", {"timestamp": now, "results": []})

    if mode == "mt5" and account.get("login"):
        write_json_state("mt5_baseline.json", {
            "login": int(account["login"]),
            "server": account.get("server", ""),
            "starting_cash": starting,
            "set_at": now,
        })

    micro = (config.get("practice") or {}).get("micro") or {}
    growth = (config.get("practice") or {}).get("growth") or {}
    write_json_state("daily_growth.json", {
        "enabled": False,
        "login": int(account["login"]) if account.get("login") else None,
        "day": now[:10],
        "day_start_equity": round(equity, 2),
        "current_equity": round(equity, 2),
        "daily_pnl": 0.0,
        "daily_pnl_pct": 0.0,
        "target_pct": float(growth.get("daily_target_pct", 20)),
        "remaining_pct": float(growth.get("daily_target_pct", 20)),
        "target_hit": False,
        "max_daily_loss_pct": float(micro.get("max_daily_loss_pct") or growth.get("max_daily_loss_pct", 10)),
        "trading_paused": False,
        "pause_reason": None,
        "updated_at": now,
    })

    state_dir = ROOT / "state"
    for name in DELETE_FILES:
        path = state_dir / name
        if path.exists():
            path.unlink()

    print(f"Session memory reset at {now}")
    print(f"  mode={mode} starting_cash={starting:.2f} equity={equity:.2f}")
    cleared = "memory, edge DB/scores, trades/orders, signals"
    if preserve_safety_gates:
        cleared += " (kill switch + risk gates + position management PRESERVED — fail-safe)"
    else:
        cleared += ", risk/kill switch, position management"
    print(f"  Cleared: {cleared}")
    print("  Preserved: latest_candles, features, account, broker_symbols, history")


if __name__ == "__main__":
    # Fail-safe default: preserve safety gates. A true full reset (which also
    # clears the kill switch, risk gates, and position management) requires the
    # explicitly named --full flag.
    reset_session_memory(preserve_safety_gates="--full" not in sys.argv)