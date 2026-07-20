"""Profit-protection give-back guard for the live real account.

USER-AUTHORIZED 2026-06-30: if the bot reaches the +20% daily goal and then
gives back 5 percentage points (daily PnL drops back to <= +15%), CLOSE ALL
open positions and pause real trading for 1 hour, then auto-resume.

Standalone + isolated — does NOT touch the bot's core code. It:
  * reads state/daily_growth.json + state/account.json (read-only),
  * arms once daily_growth.target_hit is True (the bot hit +20%),
  * on give-back (daily_pnl_pct <= TARGET - GIVE_BACK), flattens every open
    MT5 position via opposite market deals, then
  * pauses new real orders by atomically flipping config.yaml
    execution.live_trading_enabled: true -> false (the bot reloads config each
    tick, so the pause takes effect on the next tick without a restart), and
  * after PAUSE_HOURS, flips it back to true and re-arms (re-arming requires
    hitting +20% again, so it cannot re-trigger immediately).

A new UTC day resets the guard (armed/paused cleared).

Run (from repo root, real account only):
    python scripts/giveback_guard.py            # live
    python scripts/giveback_guard.py --dry-run  # detect+log, no flatten/pause
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
CONFIG = ROOT / "config.yaml"
GUARD_STATE = STATE / "giveback_guard.json"
LOG = ROOT / "logs" / "giveback_guard.log"

TARGET_PCT = 20.0      # the +20%/day goal
GIVE_BACK_PCT = 5.0    # give-back tolerance: fire at TARGET - GIVE_BACK = +15%
PAUSE_HOURS = 1        # pause duration after a flatten
POLL_SEC = 15          # state poll cadence
TERMINAL = r"C:\Users\Administrator\MT5Agent\terminal64.exe"

LIVE_RE = re.compile(r"^(\s*live_trading_enabled:\s*)(true|false)\s*(#.*)?$",
                     re.IGNORECASE | re.MULTILINE)


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc_day() -> str:
    return _now_utc().strftime("%Y-%m-%d")


def _iso() -> str:
    return _now_utc().isoformat()


def log(msg: str) -> None:
    line = f"{_iso()} | {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _read(path: Path) -> dict:
    try:
        return json.load(path.open(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(s: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    s["updated_at"] = _iso()
    tmp = GUARD_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2), encoding="utf-8")
    os.replace(tmp, GUARD_STATE)


def _set_live_trading_enabled(value: bool, dry: bool) -> bool:
    """Atomically flip execution.live_trading_enabled in config.yaml.

    Targeted regex replace on the single line — never yaml.dump (preserves all
    comments/formatting). Returns True if the file was changed.
    """
    text = CONFIG.read_text(encoding="utf-8")
    m = LIVE_RE.search(text)
    if not m:
        log("ERROR: live_trading_enabled line not found in config.yaml — cannot pause")
        return False
    want = "true" if value else "false"
    cur = m.group(2).lower()
    if cur == want:
        return False  # already in desired state
    if dry:
        log(f"[dry-run] would set live_trading_enabled -> {want}")
        return False
    new_text = LIVE_RE.sub(lambda mm: mm.group(1) + want + (mm.group(3) or ""), text, count=1)
    tmp = CONFIG.with_suffix(".yaml.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, CONFIG)
    log(f"config.yaml: execution.live_trading_enabled -> {want}")
    return True


def _flatten_all(dry: bool) -> int:
    """Close every open MT5 position via opposite market deal. Returns count closed."""
    if dry:
        log("[dry-run] would flatten all open positions")
        return 0
    if mt5 is None:
        log("ERROR: MetaTrader5 not importable — cannot flatten")
        return 0
    if not mt5.initialize(TERMINAL):
        log(f"ERROR: mt5.initialize failed: {mt5.last_error()}")
        return 0
    try:
        positions = mt5.positions_get() or []
        closed = 0
        for p in positions:
            tick = mt5.symbol_info_tick(p.symbol)
            if not tick:
                continue
            # opposite side to close: BUY pos -> SELL at bid, SELL pos -> BUY at ask
            if p.type == mt5.POSITION_TYPE_BUY:
                order_type = mt5.ORDER_TYPE_SELL
                price = tick.bid
            else:
                order_type = mt5.ORDER_TYPE_BUY
                price = tick.ask
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": p.symbol,
                "volume": p.volume,
                "type": order_type,
                "position": p.ticket,
                "price": price,
                "deviation": 50,
                "magic": 20250625,
                "comment": "giveback_guard_flatten",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            r = mt5.order_send(req)
            if r is None:
                log(f"  close FAIL #{p.ticket} {p.symbol} retcode=None err={mt5.last_error()}")
                continue
            if r.retcode != mt5.TRADE_RETCODE_DONE:
                log(f"  close FAIL #{p.ticket} {p.symbol} retcode={r.retcode}")
                continue
            closed += 1
            log(f"  closed #{p.ticket} {p.symbol} vol={p.volume} @ {price}")
        log(f"flatten: closed {closed}/{len(positions)} positions")
        return closed
    finally:
        mt5.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="detect + log only; do not flatten or pause")
    args = ap.parse_args()

    log(f"giveback_guard start (target=+{TARGET_PCT:.0f}% give_back={GIVE_BACK_PCT:.0f}pp "
        f"-> fire<=+{TARGET_PCT-GIVE_BACK_PCT:.0f}% pause={PAUSE_HOURS}h dry={args.dry_run})")
    s = _read(GUARD_STATE)
    if s.get("day") != _utc_day():
        s = {"day": _utc_day(), "armed": False, "paused": False,
             "resume_at": None, "flatten_done": False}
        _write_state(s)

    fire_pct = TARGET_PCT - GIVE_BACK_PCT  # +15
    while True:
        try:
            dg = _read(STATE / "daily_growth.json")
            acc = _read(STATE / "account.json")
            day = dg.get("day")
            pnl_pct = float(dg.get("daily_pnl_pct", 0) or 0)
            target_hit = bool(dg.get("target_hit", False))
            eq = float(acc.get("equity", 0) or 0)

            # new UTC day resets
            if day != s.get("day"):
                log(f"new day {day} -> reset guard")
                s = {"day": day, "armed": False, "paused": False,
                     "resume_at": None, "flatten_done": False}

            # arm once target reached
            if target_hit and not s.get("armed"):
                s["armed"] = True
                s["target_reached_at"] = _iso()
                log(f"ARMED: +{TARGET_PCT:.0f}% target reached (pnl={pnl_pct:+.2f}% eq=${eq:.2f}); "
                    f"will flatten+pause if pnl drops below +{fire_pct:.0f}%")

            # check resume
            if s.get("paused") and s.get("resume_at"):
                if _now_utc() >= dt.datetime.fromisoformat(s["resume_at"]):
                    log(f"PAUSE elapsed -> resume (flip live_trading_enabled=true, re-arm)")
                    _set_live_trading_enabled(True, args.dry_run)
                    s["paused"] = False
                    s["resume_at"] = None
                    s["armed"] = False  # require re-hitting +20% to re-arm
                    s["flatten_done"] = False
                else:
                    remaining = dt.datetime.fromisoformat(s["resume_at"]) - _now_utc()
                    log(f"paused, {int(remaining.total_seconds()/60)} min to resume")
            _write_state(s)

            # fire condition: armed, not paused, gave back below threshold
            if s.get("armed") and not s.get("paused") and pnl_pct <= fire_pct:
                log(f"TRIGGER: pnl {pnl_pct:+.2f}% <= +{fire_pct:.0f}% give-back line "
                    f"(eq=${eq:.2f}) -> flatten all + pause {PAUSE_HOURS}h")
                _flatten_all(args.dry_run)
                _set_live_trading_enabled(False, args.dry_run)
                s["paused"] = True
                s["resume_at"] = (_now_utc() + dt.timedelta(hours=PAUSE_HOURS)).isoformat()
                s["flatten_done"] = True
                s["fired_at"] = _iso()
                _write_state(s)

            # while paused, keep positions flat (bot cannot open new ones, but
            # defensively close anything that sneaks through)
            if s.get("paused") and not args.dry_run:
                _flatten_all(False)

            time.sleep(POLL_SEC)
        except KeyboardInterrupt:
            log("stop (interrupted)")
            return 0
        except Exception as e:
            log(f"loop error: {e!r}")
            time.sleep(POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())