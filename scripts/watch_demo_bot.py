"""Lightweight demo-bot monitor for the Monitor tool.

Reads the bot's STATE FILES directly (account.json, paper_positions.json,
health.json, kill_switch.json, supervisor.json) -- NOT the bot's /api/state HTTP
endpoint. The bot serves its dashboard IN-PROCESS on :8080, and when the trading
loop is busy that HTTP layer stalls and times out, which would make this monitor
blind to events. Reading the state files the bot writes continuously is always
fast and always works. (Same approach as scripts/nojs_dashboard.py.)

Emits ONE line per meaningful event:
  - a position opens/closes (open count changes)
  - kill switch flips
  - health drops below 'healthy'
  - the bot STOPS writing state files (likely crashed) -> BOT_STALLED
  - equity moves by > $0.50

Emits only on change so output stays selective. Run via the Monitor tool:
    Monitor: python scripts/watch_demo_bot.py
"""
import json
import os
import time
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "state"
POLL = 30.0
# Emit on equity moves >= ~0.5% of a ~$90 demo balance, not every cent of noise
# (otherwise the monitor fires every poll and gets auto-stopped). Position-count,
# kill-switch, and health events are always emitted regardless of size.
EQUITY_EPS = 5.0
# State files should update every loop iteration (~30-45s). If the newest is older
# than this, the bot has likely stalled/crashed.
STALL_SECONDS = 120.0


def _load(name):
    try:
        with open(STATE / name, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _summary():
    acc = _load("account.json") or {}
    pos = _load("paper_positions.json") or {}
    ks = _load("kill_switch.json") or {}
    hp = _load("health.json") or {}
    sup = _load("supervisor.json") or {}

    # bot-stall detection: how old is the freshest state file we care about?
    now = time.time()
    ages = []
    for name in ("account.json", "paper_positions.json", "health.json", "supervisor.json"):
        try:
            ages.append(now - os.path.getmtime(STATE / name))
        except OSError:
            pass
    freshest = min(ages) if ages else 1e9
    if freshest > STALL_SECONDS:
        return None, ("BOT_STALLED", f"no state file updated in {freshest:.0f}s")

    s = {
        "balance": acc.get("balance"),
        "equity": acc.get("equity"),
        "open": len(pos.get("positions", [])) if isinstance(pos, dict) else 0,
        "kill_switch": bool(ks.get("kill_switch")) if isinstance(ks, dict) else None,
        "health": hp.get("status") if isinstance(hp, dict) else None,
        "supervisor_status": (sup.get("status") if isinstance(sup, dict) else None),
    }
    return s, None


if __name__ == "__main__":
    prev = None
    while True:
        s, err = _summary()
        if err:
            print(f"EVENT {err[0]} {err[1]}", flush=True)
            time.sleep(POLL)
            continue
        if prev is None:
            prev = s
            print(f"EVENT BOT_UP balance=${s['balance']} equity=${s['equity']} "
                  f"open={s['open']} kill={s['kill_switch']} health={s['health']}", flush=True)
            time.sleep(POLL)
            continue
        events = []
        if s["open"] != prev["open"]:
            events.append(f"POSITIONS {prev['open']}->{s['open']} equity=${s['equity']} balance=${s['balance']}")
        if s["kill_switch"] != prev["kill_switch"]:
            events.append(f"KILL_SWITCH {prev['kill_switch']}->{s['kill_switch']}")
        if s["health"] != prev["health"] and s["health"] != "healthy":
            events.append(f"HEALTH={s['health']}")
        try:
            if s["equity"] is not None and prev["equity"] is not None:
                de = float(s["equity"]) - float(prev["equity"])
                if abs(de) > EQUITY_EPS:
                    events.append(f"EQUITY ${prev['equity']}->${s['equity']} (delta {de:+.2f}) open={s['open']}")
        except Exception:
            pass
        for e in events:
            print(f"EVENT {e}", flush=True)
        # silent tick when nothing changed (expected when market quiet); bot
        # liveness is confirmed by the absence of BOT_STALLED events
        prev = s
        time.sleep(POLL)