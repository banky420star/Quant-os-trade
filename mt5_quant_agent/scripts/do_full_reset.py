"""Full factory reset — kill bot, back up state, clear learning/trade data, restart."""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).resolve().parent.parent
STATE = BASE / "state"
BACKUP = STATE / "backup_pre_reset"
BACKUP.mkdir(exist_ok=True)
ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

# ── 1. Back up state files ──────────────────────────────────────────────
print("=== Backing up state files ===")
for fname in ["learning_config_overrides.json", "learning_state.json", "trade_log.json"]:
    src = STATE / fname
    if src.exists():
        dst = BACKUP / f"{fname}.{ts}.bak"
        shutil.copy2(src, dst)
        print(f"  {fname} ({src.stat().st_size:,} bytes) -> {dst.name}")
    else:
        print(f"  {fname}: not found")

# ── 2. Kill the bot ──────────────────────────────────────────────────────
print("\n=== Killing bot processes ===")
r = subprocess.run(
    'wmic process where "name=\'python.exe\'" get processid,commandline /format:csv',
    capture_output=True, text=True, shell=True, timeout=15,
)
killed = 0
for line in r.stdout.split("\n"):
    if "start.py" in line:
        parts = line.strip().split(",")
        for p in parts:
            p = p.strip()
            if p.isdigit() and len(p) >= 4:
                subprocess.run(
                    ["wmic", "process", "where", f"processid={p}", "delete"],
                    capture_output=True, text=True, timeout=10,
                )
                print(f"  Killed PID {p}")
                killed += 1
if killed == 0:
    print("  No bot processes found running")

time.sleep(3)

# ── 3. Reset state files ────────────────────────────────────────────────
print("\n=== Resetting state files ===")

# learning_config_overrides.json — empty slate
lco = {"patches": [], "rollbacks": [], "updated_at": "2026-07-21T01:41:00+00:00"}
(STATE / "learning_config_overrides.json").write_text(
    json.dumps(lco, indent=2), encoding="utf-8"
)
print("  learning_config_overrides.json: reset to empty")

# learning_state.json — fresh counters
ls = {
    "reviewed_count": 0,
    "win_rate": 0.0,
    "expectancy": 0.0,
    "active_proposals": 0,
    "applied_patches": 0,
    "mistake_counts": {},
    "reset_at": "2026-07-21T01:41:00+00:00",
}
(STATE / "learning_state.json").write_text(json.dumps(ls, indent=2), encoding="utf-8")
print("  learning_state.json: reset to fresh")

# trade_log.json — empty
tl = {"trades": [], "reset_at": "2026-07-21T01:41:00+00:00"}
(STATE / "trade_log.json").write_text(json.dumps(tl, indent=2), encoding="utf-8")
print("  trade_log.json: reset to empty (0 trades)")

# ── 4. Wait for port to free ─────────────────────────────────────────────
print("\n=== Waiting for port 8080 to be free ===")
for attempt in range(6):
    r = subprocess.run(
        'netstat -ano | findstr ":8080" | findstr "LISTENING"',
        capture_output=True, text=True, shell=True, timeout=10,
    )
    if not r.stdout.strip():
        print("  Port 8080 is free")
        break
    print(f"  Port still busy (attempt {attempt+1}/6)...")
    time.sleep(5)
else:
    print("  Port 8080 still busy — forcing remaining python processes")
    subprocess.run(
        'wmic process where "commandline like \'%start.py%\'" delete',
        capture_output=True, text=True, shell=True, timeout=10,
    )
    time.sleep(5)

# ── 5. Restart the bot with growth profile ──────────────────────────────
print("\n=== Starting bot with growth profile ===")
log_path = BASE / ".freebuff" / "growth_bot.log"
log_path.parent.mkdir(exist_ok=True)

# Write a startup script for the bot
import os as _os
bot_pid = _os.spawnl(
    _os.P_NOWAIT,
    "python",
    "python", "start.py", "--profile", "growth",
)

print(f"  Bot started (process spawned, waiting for boot)...")

# Wait for it to bind
for attempt in range(12):
    time.sleep(5)
    r = subprocess.run(
        'netstat -ano | findstr ":8080" | findstr "LISTENING"',
        capture_output=True, text=True, shell=True, timeout=10,
    )
    if r.stdout.strip():
        pid_line = r.stdout.strip().split("\n")[0]
        pid = pid_line.strip().split()[-1] if pid_line.strip() else "?"
        print(f"  Bot listening on port 8080 (PID {pid})")
        break
    print(f"  Waiting for boot (attempt {attempt+1}/12)...")
else:
    print("  WARNING: Bot may not have started. Check log.")

print("\n=== Full reset complete ===")
print("State backups saved to: state/backup_pre_reset/")
