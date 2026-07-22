"""One-shot script to manually close all open paper positions.

Writes proper closed trade records into state/trade_log.json, empties
state/paper_positions.json, and archives the mgmt rows into
state/position_mgmt_archive.jsonl. Stops the bleeding immediately so
the user can review/learn without ticking PnL.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"

TRADE_LOG = STATE / "trade_log.json"
PAPER_POS = STATE / "paper_positions.json"
POSITION_MGMT = STATE / "position_management.json"
ARCHIVE = STATE / "position_mgmt_archive.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# Approximate USD value of 1.0 price move per 1.0 lot. Used ONLY to
# synthesise an exit_price from realised PnL when the broker snapshot is
# unavailable (paper simulation). Real MT5 fills would record the actual
# price; this is a best-effort approximation for ledger hygiene.
_CONTRACT_VALUE_USD_PER_LOT_PER_PRICE = {
    "XAUUSDm": 100.0,
    "USOILm": 1000.0,
    "BTCUSDm": 1.0,
    "US500m": 1.0,
    "US30m": 1.0,
    "NAS100m": 1.0,
    "USTEC_x100m": 1.0,
    "UK100m": 1.0,
    "FR40m": 1.0,
    "JP225m": 1.0,
    "EURUSDm": 100000.0,
    "GBPUSDm": 100000.0,
    "USDJPYm": 100000.0,
    "USDCHFm": 100000.0,
    "AUDUSDm": 100000.0,
}


def _derive_exit_price(side: str, entry: float, size: float, pnl_usd: float, symbol: str) -> float | None:
    if size <= 0:
        return None
    cv = _CONTRACT_VALUE_USD_PER_LOT_PER_PRICE.get(symbol)
    if not cv:
        return None
    sign = 1.0 if side == "BUY" else -1.0
    diff = sign * (pnl_usd / (size * cv))
    return round(entry + diff, 6)


def close_all() -> int:
    data = _load_json(PAPER_POS, default={"positions": []})
    positions = data.get("positions", []) if isinstance(data, dict) else data
    if not positions:
        print("No open positions to close.")
        return 0

    trade_log = _load_json(TRADE_LOG, default=[])
    if not isinstance(trade_log, list):
        trade_log = trade_log.get("trades", []) if isinstance(trade_log, dict) else []
    mgmt = _load_json(POSITION_MGMT, default={"positions": {}})

    closed_total = 0
    closed_wins = 0
    closed_loss = 0
    total_pnl = 0.0
    results = []

    for p in positions:
        sym = p.get("symbol")
        side = p.get("side", "BUY")
        entry = float(p.get("entry", 0) or 0)
        sl = float(p.get("sl", 0) or 0)
        size = float(p.get("size", 0.01) or 0.01)
        tp1 = float(p.get("tp1", 0) or 0)
        profit = float(p.get("profit", 0) or 0)
        opened_at = p.get("opened_at") or _now()
        ticket = p.get("position_id") or p.get("ticket")
        exit_price = _derive_exit_price(side, entry, size, profit, sym)

        risk_price = abs(entry - sl) if sl > 0 else 0.0
        cv = _CONTRACT_VALUE_USD_PER_LOT_PER_PRICE.get(sym, 1.0)
        risk_amount = risk_price * size * cv if risk_price else 0.0
        r_multiple = (profit / risk_amount) if risk_amount > 0 else 0.0

        won = profit > 0
        closed_total += 1
        if won:
            closed_wins += 1
        else:
            closed_loss += 1
        total_pnl += profit

        trade_id = f"manual-{ticket or uuid.uuid4().hex[:10]}"
        closed_at = _now()
        sec_open = p.get("opened_at_epoch")
        sec_close = datetime.now(timezone.utc).timestamp()
        if isinstance(sec_open, (int, float)):
            hold_seconds = max(0, sec_close - sec_open)
        else:
            hold_seconds = 0

        trade = {
            "trade_id": trade_id,
            "signal_id": None,
            "symbol": sym,
            "side": side,
            "setup": p.get("setup_type"),
            "result": "win" if won else "loss",
            "won": won,
            "pnl": round(profit, 4),
            "r_multiple": round(r_multiple, 4),
            "risk_price": round(risk_price, 6) if risk_price else None,
            "risk_amount": round(risk_amount, 4) if risk_amount else None,
            "entry": entry,
            "exit": exit_price,
            "exit_recorded": True,
            "sl": sl,
            "sl_initial": sl,
            "tp1": tp1,
            "tp2": p.get("tp2") or 0,
            "volume": size,
            "confidence": None,
            "opened_at": opened_at,
            "closed_at": closed_at,
            "hold_seconds": int(hold_seconds),
            "hold_human": f"{int(hold_seconds // 60)}m {int(hold_seconds % 60)}s",
            "open_price": entry,
            "exit_reason": "manual_close",
            "reason": "user_requested_immediate_close",
            "manual": True,
        }
        trade_log.append(trade)
        results.append((sym, side, profit, r_multiple, exit_price))
        print(
            f"  CLOSED {sym:10s} {side:4s} entry={entry:>10.4f}  pnl={profit:>+8.2f}  "
            f"exit~={exit_price if exit_price else 'n/a':>12}  R={r_multiple:+.2f}"
        )

        # Archive mgmt row
        tkt = str(ticket) if ticket else trade_id
        mgmt_row = (mgmt.get("positions") or {}).get(tkt, {})
        archive_record = {
            "ticket": tkt,
            "transaction_id": uuid.uuid4().hex[:12],
            "reason": "manual_close",
            "side": side,
            "entry": entry,
            "symbol": sym,
            "mgmt_row": {
                "break_even": bool(mgmt_row.get("break_even")),
                "partial_tp_done": bool(mgmt_row.get("partial_tp_done")),
                "trailing": bool(mgmt_row.get("trailing")),
                "stale_closed": True,
                "initial_sl": mgmt_row.get("initial_sl") or sl,
                "peak_price": mgmt_row.get("peak_price"),
                "worst_price": mgmt_row.get("worst_price"),
                "mfe_R": mgmt_row.get("mfe_R"),
                "mae_R": mgmt_row.get("mae_R"),
                "tp2": mgmt_row.get("tp2"),
                "manual_close": True,
            },
        }
        _append_jsonl(ARCHIVE, archive_record)

        if isinstance(mgmt.get("positions"), dict):
            mgmt["positions"].pop(tkt, None)

    _save_json(TRADE_LOG, trade_log)
    _save_json(POSITION_MGMT, mgmt)
    empty_paper = {
        "timestamp": _now(),
        "mode": data.get("mode", "paper") if isinstance(data, dict) else "paper",
        "source": "manual_close_all",
        "positions": [],
    }
    _save_json(PAPER_POS, empty_paper)

    print()
    print(f"=== CLOSED {closed_total} POSITIONS ===")
    print(f"  Wins:     {closed_wins}")
    print(f"  Losses:   {closed_loss}")
    print(f"  PnL:      ${total_pnl:+.2f}")
    print(f"  Wrote {len(trade_log)} trades total to trade_log.json")
    print(f"  Emptied paper_positions.json")
    print(f"  Archived mgmt rows to position_mgmt_archive.jsonl")
    return closed_total


if __name__ == "__main__":
    n = close_all()
    sys.exit(0 if n >= 0 else 1)
