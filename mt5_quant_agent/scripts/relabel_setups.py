"""One-shot: relabel closed bot trades whose setup_type was lost as 'unknown'.

MT5 overwrites the CLOSING (OUT) deal comment with the close reason (e.g.
"[sl 4055.73]"), so setup_type could not be recovered from the OUT deal. This
reads the OPENING (IN) deal comment (qagent_pullback / qagent_trend_con) for each
closed position and writes the real setup_type back into state/quant_os.db.

Run AFTER stopping the bot (so the DB isn't locked). Idempotent: only touches
rows still labelled 'unknown' (or null).
"""
from __future__ import annotations
import json
import sqlite3
import MetaTrader5 as mt5
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, ".")
from core.utils import load_config, setup_logger
from core.mt5_connection_manager import MT5ConnectionManager
from core.trade_tracker import _setup_from_in_comment

cfg = load_config()
log = setup_logger("relabel", "relabel_setups.log")
MAGIC = int(cfg.get("execution", {}).get("magic_number", 20250625))

since = datetime.now(timezone.utc) - timedelta(days=10)

conn = MT5ConnectionManager(cfg, log)
conn.connect()
try:
    deals = mt5.history_deals_get(since, datetime.now(timezone.utc)) or []
finally:
    conn.disconnect()

in_comment_by_pos = {}
for d in deals:
    try:
        if d.entry == mt5.DEAL_ENTRY_IN and d.magic == MAGIC:
            in_comment_by_pos[getattr(d, "position_id", None)] = d.comment or ""
    except Exception:
        pass

c = sqlite3.connect("state/quant_os.db")
cur = c.cursor()
rows = cur.execute("SELECT trade_id, raw_json FROM trades").fetchall()
relabeled = 0
still = 0
for tid, rj in rows:
    d = json.loads(rj)
    if d.get("setup_type") not in (None, "unknown"):
        continue
    pos = d.get("mt5_position")
    in_cmt = in_comment_by_pos.get(pos, "") if pos else ""
    new_setup = _setup_from_in_comment(in_cmt)
    if new_setup and new_setup != "unknown":
        d["setup_type"] = new_setup
        cur.execute(
            "UPDATE trades SET setup_type=?, raw_json=? WHERE trade_id=?",
            (new_setup, json.dumps(d), tid),
        )
        relabeled += 1
    else:
        still += 1
c.commit()
c.close()
print(f"relabeled {relabeled} unknown -> setup; still unknown: {still}")
