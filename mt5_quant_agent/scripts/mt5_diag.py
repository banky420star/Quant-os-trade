"""Diagnose MT5 connection and list available symbols."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import MetaTrader5 as mt5
from core.utils import load_config

config = load_config()
mt5_cfg = config["mt5"]

paths = [
    mt5_cfg.get("path"),
    r"C:\Program Files\MetaTrader 5\terminal64.exe",
    r"C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe",
]

for path in paths:
    if not path:
        continue
    print(f"\n=== Trying path: {path} ===")
    if not Path(path).exists():
        print("  Path does not exist")
        continue

    ok = mt5.initialize(
        path=path,
        login=int(mt5_cfg["login"]),
        password=str(mt5_cfg["password"]),
        server=str(mt5_cfg["server"]),
        timeout=120000,
    )
    print(f"  initialize: {ok} error={mt5.last_error()}")
    if not ok:
        mt5.shutdown()
        continue

    account = mt5.account_info()
    if account:
        print(f"  account: {account.login} @ {account.server} balance={account.balance}")

    symbols = mt5_cfg["symbols"]
    for sym in symbols:
        selected = mt5.symbol_select(sym, True)
        info = mt5.symbol_info(sym)
        rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M5, 0, 3)
        print(f"  {sym}: select={selected} info={info is not None} rates={len(rates) if rates is not None else 0}")

    if not all(mt5.symbol_info(s) for s in symbols):
        print("  Searching similar symbol names...")
        all_syms = mt5.symbols_get()
        if all_syms:
            for target in ("XAU", "OIL", "BTC"):
                matches = [s.name for s in all_syms if target in s.name.upper()][:8]
                print(f"    {target}: {matches}")

    mt5.shutdown()
    break