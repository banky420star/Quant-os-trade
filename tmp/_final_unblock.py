"""Final integrated unblock + verify.

1. Discover SQLite historic trade archive (state/quant_os.db)
2. Stamp counts into state/trade_log.json -> data_quality field
3. Launch fresh start.py --profile 30-c2 on port 8081 in background
4. Verify /api/state on 8081
"""
import json, os, sqlite3, subprocess, sys, time, urllib.request
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(os.getcwd()).resolve()
STATE = ROOT / "state"
DB = STATE / "quant_os.db"

print("=== CWD ===", ROOT)
print("=== DB exists ===", DB.exists(), "size=", DB.stat().st_size if DB.exists() else 0)

print()
print("=== SQLite Discovery ===")
trade_like = []
if DB.exists():
    conn = sqlite3.connect(str(DB), timeout=5)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r[0] for r in cur.fetchall()]
        # Browse all tables, count rows, find ones that look like trade archives.
        for t in tables:
            try:
                cur.execute(f"SELECT COUNT(*) FROM [{t}]")
                n = cur.fetchone()[0]
            except Exception:
                continue
            try:
                cur.execute(f"PRAGMA table_info([{t}])")
                cols = [r[1] for r in cur.fetchall()]
            except Exception:
                cols = []
            colset = " ".join(cols).lower()
            if any(k in colset for k in ("pnl","r_multiple","exit_reason","close_time","magic","trade_id","result")):
                trade_like.append({"table": t, "rows": n, "cols": cols[:20]})
        print(f"Trade-like tables: {len(trade_like)}")
        for c in trade_like[:8]:
            print(f"  {c['table']:<35} rows={c['rows']:<6} cols_sample={c['cols'][:6]}")
    finally:
        conn.close()
    total_archived = sum(c["rows"] for c in trade_like)
    print(f"total_archived_across_tables = {total_archived}")
else:
    total_archived = 0

print()
print("=== Stamp trade_log.json data_quality.sqlite_archive_count ===")
tl_path = STATE / "trade_log.json"
tl = json.loads(tl_path.read_text(encoding="utf-8"))
if not isinstance(tl, dict):
    tl = {"trades": [], "total": 0}
dq = tl.setdefault("data_quality", {})
dq["sqlite_archive_count"] = total_archived
dq["sqlite_archive_tables"] = [c["table"] for c in trade_like][:10]
dq["historic_session_count"] = total_archived  # how many closes the bot has SEEN across all sessions
dq["session_count"] = tl.get("total", 0)       # how many closes the bot has SEEN THIS session
dq["coverage_note"] = (
    f"build_log joins paper_trades.json + paper_orders.json + MT5 deal history ONLY. "
    f"state/quant_os.db has {total_archived} historic closes NOT surfaced through build_log yet "
    f"(this is a known data-layer gap). Session count = 0 because the bot has not closed a "
    f"trade since the live-unblock config was applied at ~{(time.time()):.0f}."
)
# Save back, but preserve the rebuilt fields from build_log too.
tl["data_quality"] = dq
tl_path.write_text(json.dumps(tl, indent=2, default=str), encoding="utf-8")
print("OK saved trade_log.json with historical archive count stamped in.")

print()
print("=== Launch fresh start.py --profile 30-c2 on 8081 in background ===")
LOG = ROOT / "logs" / "start_8081.log"
LOG.parent.mkdir(parents=True, exist_ok=True)
env = dict(os.environ)
env["DASHBOARD_PORT"] = "8081"
# Spawn detached via subprocess.Popen to survive this turn.
p = subprocess.Popen(
    [sys.executable, "start.py", "--profile", "30-c2", "--dashboard-port", "8081"],
    cwd=str(ROOT), env=env,
    stdout=open(LOG, "ab", 0), stderr=subprocess.STDOUT,
    close_fds=False,
)
print(f"launched PID {p.pid}, log={LOG}")

print()
print("=== Wait 20s then probe http://127.0.0.1:8081/ ===")
for i in range(8):
    time.sleep(3)
    try:
        r = urllib.request.urlopen("http://127.0.0.1:8081/api/state", timeout=3).read()
        d = json.loads(r)
        rt = d.get("runtime_mode", {})
        ts = d.get("trading_status", {})
        lp = d.get("live_portfolio", {})
        cs = d.get("candidate_signals", {})
        appr = d.get("approved_signals", {})
        cf = d.get("config", {})
        print(f"poll #{i+1}: equity={lp.get('equity')} balance={lp.get('balance')} "
              f"open={len(lp.get('open_positions') or [])} candidates={cs.get('count')} "
              f"approved={appr.get('count')} can_exec={ts.get('can_execute')} "
              f"status={ts.get('status')}")
        # Look at execution flag echo
        print(f"  cf.execution.mode={(cf.get('execution') or {}).get('mode')}")
        print(f"  cf.execution.live_trading_enabled={(cf.get('execution') or {}).get('live_trading_enabled')}")
        print(f"  runtime.label={rt.get('label')[:60]}")
        break
    except Exception as e:
        print(f"poll #{i+1}: {type(e).__name__}: {str(e)[:120]}")

print()
print("=== Done. New PID is", p.pid, "===")
