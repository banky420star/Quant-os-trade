"""Reset equity curve to current account — clears snapshot history and closed-trade series."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import load_config, read_json_state, utc_now_iso, write_json_state


def reset_equity_curve() -> None:
    config = load_config()
    now = utc_now_iso()
    account = read_json_state("account.json", default={})
    mode = config.get("execution", {}).get("mode", "paper")

    equity = float(account.get("equity", account.get("balance", 1000)))
    cash = float(account.get("balance", equity))
    # Baseline from current equity so open-position unrealized PnL does not trip drawdown.
    baseline = round(equity, 2)
    open_positions = len(read_json_state("paper_positions.json", default={"positions": []}).get("positions", []))

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
            "source": "reset_equity_curve",
            "drawdown": 0.0,
            "open_positions": open_positions,
            "exposure_used_pct": 0.0,
        }],
    })

    write_json_state("paper_trades.json", {
        "timestamp": now,
        "mode": mode,
        "session_started_at": now,
        "trades": [],
    })

    orders = read_json_state("paper_orders.json", default={})
    write_json_state("paper_orders.json", {
        **orders,
        "timestamp": now,
        "mode": mode,
        "balance": {
            "cash": round(cash, 2),
            "equity": round(equity, 2),
            "starting_cash": baseline,
        },
        "orders": orders.get("orders", []),
    })

    if mode == "mt5" and account.get("login"):
        write_json_state("mt5_baseline.json", {
            "login": int(account["login"]),
            "server": account.get("server", ""),
            "starting_cash": baseline,
            "set_at": now,
        })

    write_json_state("kill_switch.json", {
        "kill_switch": False,
        "reason": None,
        "activated_at": None,
    })

    risk = read_json_state("risk_state.json", default={})
    write_json_state("risk_state.json", {
        **risk,
        "timestamp": now,
        "kill_switch": False,
        "drawdown": 0.0,
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "open_positions": open_positions,
        "risk_events": [],
    })

    print(f"Equity curve reset at {now}")
    print(f"  baseline={baseline:.2f} equity={equity:.2f} balance={cash:.2f} open_positions={open_positions}")
    print("  Cleared: equity_history snapshots, paper_trades (chart trade markers)")


if __name__ == "__main__":
    reset_equity_curve()