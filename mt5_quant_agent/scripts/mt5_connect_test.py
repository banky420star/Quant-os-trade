"""Minimal MT5 connection probes — uses MT5Agent path from config."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import MetaTrader5 as mt5
from core.utils import load_config

config = load_config()
terminal = config["mt5"].get("path", r"C:\Users\Administrator\MT5Agent\terminal64.exe")
timeout = int(config["mt5"].get("timeout_ms", 60000))

PROBES = [
    ("mt5agent_path", {"path": terminal, "timeout": timeout}),
    ("no_args", {"timeout": timeout}),
]

print(f"Terminal: {terminal}")
print("For full session diagnostics use: python scripts/mt5_connect_test_session.py")

for name, kwargs in PROBES:
    print(f"\n--- {name} ---")
    mt5.shutdown()
    ok = mt5.initialize(**kwargs)
    print(f"initialize: {ok}")
    print(f"last_error: {mt5.last_error()}")
    if ok:
        acc = mt5.account_info()
        print(f"account: {acc.login if acc else None} @ {acc.server if acc else None}")
        print(f"balance: {acc.balance if acc else None}")
        print(f"symbols_total: {mt5.symbols_total()}")
        mt5.shutdown()
        break