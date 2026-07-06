"""Pre-flight checks before starting or after account switch."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import load_config, read_json_state


def run_preflight() -> int:
    config = load_config()
    issues: list[str] = []
    warnings: list[str] = []

    # Single bot instance
    lock = read_json_state("agent_lock.json", default={})
    if lock.get("pid"):
        try:
            import psutil
            if psutil.pid_exists(int(lock["pid"])):
                warnings.append(f"agent_lock pid {lock['pid']} still running — run kill_agent.bat first")
        except Exception:
            pass

    kill = read_json_state("kill_switch.json", default={})
    if kill.get("kill_switch"):
        issues.append(f"Kill switch ON: {kill.get('reason')}")

    health = read_json_state("health.json", default={})
    if health.get("status") and health["status"] != "healthy":
        issues.extend(health.get("issues") or ["health not healthy"])

    account = read_json_state("account.json", default={})
    if config.get("execution", {}).get("mode") == "mt5":
        if not account.get("login"):
            warnings.append("No account.json — MT5 may not be connected yet")
        elif account.get("trade_allowed") is False:
            issues.append("MT5 trade_allowed=false — enable Algo Trading")

    equity = float(account.get("equity") or account.get("balance") or 0)
    if equity <= 0 and config.get("execution", {}).get("mode") == "mt5":
        issues.append("Account equity is zero")

    print("=== MT5 Quant OS Pre-flight ===")
    if warnings:
        print("Warnings:")
        for w in warnings:
            print(f"  ! {w}")
    if issues:
        print("BLOCKERS:")
        for i in issues:
            print(f"  X {i}")
        print("Fix blockers before trading, or run: python scripts/reset_session_memory.py")
        return 1
    print("OK — ready to start")
    if account.get("login"):
        print(f"  login={account['login']} equity=${equity:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_preflight())