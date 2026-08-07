#!/usr/bin/env python3
"""Start growth campaign on ALL symbols — reset, arena, 30-day run, launch bot."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.account_mode import runtime_mode_summary
from core.campaign_symbols import ALL_CAMPAIGN_SYMBOLS, apply_all_symbols
from core.growth_campaign import start_campaign
from core.practice_session import ensure_practice_session
from core.strategy_arena import arena_settings, reset_arena
from core.utils import load_config, read_json_state, setup_logger, utc_now_iso, write_json_state
from scripts.reset_session_memory import reset_session_memory


def _sync_account_from_mt5(config: dict, logger) -> float:
    """Best-effort MT5 equity read for campaign baseline."""
    try:
        from core.mt5_connection_manager import MT5ConnectionManager
        conn = MT5ConnectionManager(config, logger)
        conn.connect()
        import MetaTrader5 as mt5
        acct = mt5.account_info()
        conn.disconnect()
        if acct:
            equity = float(acct.equity)
            write_json_state("account.json", {
                "timestamp": utc_now_iso(),
                "login": int(acct.login),
                "server": acct.server,
                "balance": float(acct.balance),
                "equity": equity,
                "currency": acct.currency,
                "account_mode": "demo" if acct.trade_mode == 0 else "real",
                "trade_allowed": bool(acct.trade_allowed),
            })
            return equity
    except Exception as exc:
        logger.warning("MT5 equity sync skipped: %s", exc)
    account = read_json_state("account.json", default={})
    return float(account.get("equity") or account.get("balance") or 0)


def run_growth_campaign(*, start_bot: bool = True) -> int:
    logger = setup_logger("growth_campaign", "system.log")
    config = apply_all_symbols(load_config())
    growth = config.get("practice", {}).get("growth", {})

    equity = _sync_account_from_mt5(config, logger)
    if equity <= 0:
        print("ERROR: No equity — open MT5 Exness demo and retry.")
        return 1

    # Campaign restarts are an explicit full-slate reset: clear the gates
    # intentionally rather than relying on the fail-safe default.
    reset_session_memory(preserve_safety_gates=False)
    arena_state = reset_arena(config, campaign_id=f"growth-all-{utc_now_iso()[:10]}")
    settings = arena_settings(config)

    session = ensure_practice_session(config, logger, force_rebaseline=True)
    campaign = start_campaign(equity, config)
    mode = runtime_mode_summary(config)
    write_json_state("runtime_mode.json", {
        **mode,
        "campaign_active": True,
        "campaign_days": int(growth.get("campaign_days", 30)),
        "campaign_end_date": campaign.get("end_date"),
        "symbols": list(ALL_CAMPAIGN_SYMBOLS),
    })
    write_json_state("forward_test_ledger.json", {"cells": {}, "updated_at": utc_now_iso()})
    write_json_state("kill_switch.json", {
        "kill_switch": False,
        "reason": None,
        "activated_at": None,
    })

    daily = float(growth.get("daily_target_pct", 35))
    days = int(growth.get("campaign_days", 30))

    print("=" * 64)
    print("  GROWTH CAMPAIGN — ALL SYMBOLS")
    print("=" * 64)
    print(f"  Account equity:   ${equity:,.2f}")
    print(f"  Daily target:     +{daily:.0f}% (continuous, no daily lock)")
    print(f"  Campaign:         {days} days → {campaign.get('end_date')}")
    print(f"  Compound target:  ${campaign.get('compound_target_equity'):,.2f}")
    print(f"  Symbols ({len(ALL_CAMPAIGN_SYMBOLS)}):")
    print(f"    {', '.join(ALL_CAMPAIGN_SYMBOLS)}")
    print(f"  Arena:            {arena_state['campaign_id']}")
    print(f"  Setups:           {len(arena_state.get('setup_types', []))} competing")
    print(f"  Practice reset:   {session.get('rebaseline', False)}")
    print("=" * 64)

    if start_bot:
        print("  Starting MT5 Quant OS (start.py)…")
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "start.py")],
            cwd=str(ROOT),
            creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
        )
        print(f"  Bot PID: {proc.pid}")
        print("  Dashboard: http://127.0.0.1:8080")
        print("  TUI:       python scripts/terminal_view.py")
    else:
        print("  Run: START_AGENT.bat  or  python start.py")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-start", action="store_true", help="Reset campaign only, do not launch bot")
    args = ap.parse_args()
    raise SystemExit(run_growth_campaign(start_bot=not args.no_start))