"""Fresh reset for full-tilt strategy arena campaign."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.strategy_arena import arena_settings, reset_arena
from core.utils import load_config, read_json_state, utc_now_iso, write_json_state
from scripts.reset_session_memory import reset_session_memory


def reset_arena_campaign() -> None:
    config = load_config()
    # Campaign restarts are an explicit full-slate reset (arena practice):
    # clear the gates intentionally rather than relying on the fail-safe
    # default. Dashboard resets always preserve them.
    reset_session_memory(preserve_safety_gates=False)
    arena_state = reset_arena(config, campaign_id=f"full-tilt-{utc_now_iso()[:10]}")
    settings = arena_settings(config)

    write_json_state("forward_test_ledger.json", {"cells": {}, "updated_at": utc_now_iso()})
    write_json_state("daily_growth.json", {
        "day": utc_now_iso()[:10],
        "day_start_equity": read_json_state("account.json", default={}).get("equity", 0),
        "target_hit": False,
        "paused": False,
    })

    print("Full-tilt arena campaign reset")
    print(f"  campaign_id: {arena_state['campaign_id']}")
    print(f"  symbols: {settings['symbols']}")
    print(f"  kelly_fraction: {config['signals']['kelly_sizing']['kelly_fraction']}")
    print(f"  setups competing: {len(arena_state['setup_types'])}")
    print("  Logs: logs/strategy_arena.log")
    print("  Leaderboard: state/strategy_arena.json")


if __name__ == "__main__":
    reset_arena_campaign()