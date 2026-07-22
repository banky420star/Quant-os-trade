#!/usr/bin/env python3
"""Start the 30-day growth campaign from current MT5 equity."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.growth_campaign import start_campaign
from core.practice_session import ensure_practice_session
from core.utils import load_config, read_json_state, setup_logger, write_json_state


def main() -> int:
    config = load_config()
    logger = setup_logger("growth_campaign", "system.log")
    account = read_json_state("account.json", default={})
    equity = float(account.get("equity") or account.get("balance") or 0)
    if equity <= 0:
        print("ERROR: No MT5 equity in account.json — is start.py running and MT5 connected?")
        return 1

    growth = config.get("practice", {}).get("growth", {})
    days = int(growth.get("campaign_days", 30))
    daily = float(growth.get("daily_target_pct", 20))

    session = ensure_practice_session(config, logger, force_rebaseline=True)
    campaign = start_campaign(equity, config)
    write_json_state(
        "runtime_mode.json",
        {
            **read_json_state("runtime_mode.json", default={}),
            "campaign_active": True,
            "campaign_days": days,
            "campaign_end_date": campaign.get("end_date"),
        },
    )

    print("=" * 60)
    print("  30-DAY GROWTH CAMPAIGN STARTED")
    print("=" * 60)
    print(f"  Start equity:     ${equity:.2f}")
    print(f"  Daily target:     +{daily:.0f}% per UTC day (keeps trading — no daily lock)")
    print(f"  Campaign length:  {days} days continuous")
    print(f"  End date:         {campaign.get('end_date')}")
    print(f"  Compound target:  ${campaign.get('compound_target_equity'):,.2f} (+{campaign.get('compound_target_pct'):,.0f}% if +{daily:.0f}% every day)")
    print(f"  Practice reset:   {session}")
    print("=" * 60)
    print("  Bot must stay running (python start.py).")
    print("  Dashboard: http://<tailscale-ip>:8080 — Day X/30 tracker")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())