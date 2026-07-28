"""Diagnostic: probe BOTH MT5 terminals to identify which holds the funded
Exness account vs the empty one. Used to fix the bot's $0 balance issue."""

from __future__ import annotations

import json
import sys

import MetaTrader5 as mt5


def probe(path: str) -> dict:
    info: dict = {"path": path}
    try:
        ok = mt5.initialize(path=path, timeout=15000, portable=False)
    except Exception as e:
        mt5.shutdown()
        return {**info, "init_exception": repr(e)}

    info["init_ok"] = bool(ok)
    info["init_last_err"] = mt5.last_error()

    if not ok:
        mt5.shutdown()
        return info

    ai = mt5.account_info()
    if ai:
        d = ai._asdict()
        info["account"] = {
            "login": d.get("login"),
            "balance": d.get("balance"),
            "equity": d.get("equity"),
            "margin": d.get("margin"),
            "margin_free": d.get("margin_free"),
            "margin_level": d.get("margin_level"),
            "name": d.get("name"),
            "server": d.get("server"),
            "currency": d.get("currency"),
            "trade_allowed": d.get("trade_allowed"),
            "company": d.get("company"),
        }
    else:
        info["account"] = None
        info["account_info_last_err"] = mt5.last_error()

    ti = mt5.terminal_info()
    if ti:
        td = ti._asdict()
        info["terminal"] = {
            "connected": td.get("connected"),
            "name": td.get("name"),
            "path": td.get("path"),
            "trade_allowed": td.get("trade_allowed"),
            "company": td.get("company"),
        }
    else:
        info["terminal"] = None

    mt5.shutdown()
    return info


def main() -> int:
    paths = [
        r"C:\Users\Administrator\MT5Agent\terminal64.exe",
        r"C:\Program Files\MetaTrader 5\terminal64.exe",
    ]
    results = []
    for p in paths:
        r = probe(p)
        results.append(r)
        print(f"--- PROBE {p} ---")
        print(json.dumps(r, indent=2, default=str))
    print("\n--- VERDICT ---")
    for r in results:
        acc = r.get("account") or {}
        print(
            f"  {r['path']}: balance={acc.get('balance')} "
            f"free={acc.get('margin_free')} login={acc.get('login')} "
            f"server={acc.get('server')} trade_ok={acc.get('trade_allowed')}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
