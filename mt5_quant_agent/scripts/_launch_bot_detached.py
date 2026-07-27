"""Detached launch of start.py for the MT5 trading bot.
Uses Windows DETACHED_PROCESS (0x00000008) so the child survives this script's exit.
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / ".freebuff" / "bot-restart.log"
LOG.parent.mkdir(parents=True, exist_ok=True)

profile = sys.argv[1] if len(sys.argv) > 1 else "100"

cmd = [sys.executable, "start.py", "--profile", profile]
creationflags = 0x00000008  # DETACHED_PROCESS on Windows

with LOG.open("ab") as f:
    p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT,
                         creationflags=creationflags)

# Write PIDs of launcher + child to a marker file for kill convenience
(ROOT / ".freebuff" / "bot-restart.pid").write_text(f"{p.pid}\n", encoding="utf-8")
print(f"launched via {sys.executable}")
print(f"  cmd      = {cmd}")
print(f"  child_pid= {p.pid}")
print(f"  log      = {LOG}")
print(f"  detach=ok, waiting 3s for boot")
time.sleep(3)
print("exiting launcher")
