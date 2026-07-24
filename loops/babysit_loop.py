"""Babysit loop — observe live stack and apply bounded profitability guards.

Runs inside the trading pipeline (or standalone). Detects prior-class
regressions via core.babysit_metrics and writes state/babysit_status.json.
Does not invent equity; only hardens gates/blocklists already authorized.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.babysit_metrics import observe_snapshot
from core.micro_profile import sync_micro_profile
from core.utils import load_config, read_json_state, setup_logger, utc_now_iso, write_json_state


def run() -> dict:
    logger = setup_logger("babysit_loop", "babysit_loop.log")
    config = sync_micro_profile(load_config())
    account = read_json_state("account.json", default={}) or {}
    supervisor = read_json_state("supervisor.json", default={}) or {}
    trade_log = read_json_state("trade_log.json", default={}) or {}
    gates = read_json_state("adaptive_gates.json", default={}) or {}

    snap = observe_snapshot(
        account=account if isinstance(account, dict) else {},
        supervisor=supervisor if isinstance(supervisor, dict) else {},
        trade_log=trade_log if isinstance(trade_log, dict) else {},
        config=config,
        gates=gates if isinstance(gates, dict) else {},
    )
    snap["timestamp"] = utc_now_iso()

    # Bounded auto-act: if symbol still bleeding in full book, ensure blocklist
    # entry is present in state mirror for dashboard (config remains source of truth).
    actions: list[str] = []
    book = snap.get("book") or {}
    by_sym = book.get("by_symbol") or {}
    already_blocked = set(snap.get("symbol_blocklist") or [])
    bleed = []
    for sym, row in by_sym.items():
        if sym in already_blocked:
            continue  # already blocked — no need to re-flag historical bleed
        if int(row.get("n") or 0) >= 8 and float(row.get("pnl") or 0) <= -5:
            bleed.append(sym)
    if bleed:
        actions.append(f"bleed_symbols_detected:{','.join(sorted(bleed))}")
        # Persist recommended blocks for operators / next profile edit
        rec = {
            "timestamp": utc_now_iso(),
            "recommended_symbol_blocklist_add": sorted(bleed),
            "current_blocklist": snap.get("symbol_blocklist") or [],
        }
        write_json_state("babysit_recommendations.json", rec)

    # Close open positions on already-blocklisted symbols (stop live bleed).
    # New entries are blocked; stale open tickets can still drain equity.
    closed_blocked: list[dict] = []
    blocklist = set(snap.get("symbol_blocklist") or [])
    if blocklist and bool((config.get("trade_manager") or {}).get("close_blocklisted_open", True)):
        try:
            from core.mt5_connection_manager import MT5ConnectionManager
            from core.mt5_broker import MT5Broker
            from core.position_sync import fetch_mt5_agent_positions

            mode = (config.get("execution") or {}).get("mode", "paper")
            if mode == "mt5":
                conn = MT5ConnectionManager(config, logger)
                conn.connect()
                try:
                    broker = MT5Broker(config, logger)
                    for p in fetch_mt5_agent_positions(config, logger):
                        sym = str(p.get("symbol") or "")
                        if sym not in blocklist:
                            continue
                        ticket = int(p.get("ticket") or p.get("position_id") or 0)
                        side = str(p.get("side") or "BUY")
                        vol = float(p.get("size") or p.get("volume") or 0.01)
                        if ticket <= 0 or vol <= 0:
                            continue
                        res = broker.close_position(
                            ticket, sym, side, vol, reason="babysit_blocklist",
                        )
                        closed_blocked.append({
                            "symbol": sym,
                            "ticket": ticket,
                            "profit": p.get("profit"),
                            "result": res,
                        })
                        if res.get("success"):
                            actions.append(f"closed_blocklist:{sym}:{ticket}")
                finally:
                    conn.disconnect()
        except Exception as exc:
            logger.warning("babysit blocklist close failed: %s", exc)
            actions.append(f"close_blocklist_error:{exc}")

    active = book.get("active_universe") if isinstance(book.get("active_universe"), dict) else {}
    status = {
        "timestamp": utc_now_iso(),
        "equity": snap.get("equity"),
        "equity_ge_500000": snap.get("equity_ge_500000"),
        "supervisor_ok": snap.get("supervisor_ok"),
        # Prefer active-universe (allowed symbols/setups) over full historical book
        "expectancy_r": active.get("expectancy_r", book.get("expectancy_r")),
        "payoff_ratio": active.get("payoff_ratio", book.get("payoff_ratio")),
        "total_pnl": active.get("total_pnl", book.get("total_pnl")),
        "full_book_expectancy_r": book.get("expectancy_r"),
        "active_n": active.get("n"),
        "regressions": snap.get("regressions") or [],
        "critical": snap.get("critical_regressions") or [],
        "gates": snap.get("gates"),
        "xau_be": snap.get("xau_be"),
        "actions": actions,
        "closed_blocklisted": closed_blocked,
        "healthy_ops": snap.get("healthy_ops"),
    }
    write_json_state("babysit_status.json", status)
    logger.info(
        "Babysit: equity=%s active_E[R]=%s full_E[R]=%s payoff=%s regressions=%d critical=%d actions=%s",
        status.get("equity"),
        status.get("expectancy_r"),
        status.get("full_book_expectancy_r"),
        status.get("payoff_ratio"),
        len(status["regressions"]),
        len(status["critical"]),
        actions,
    )
    return status


if __name__ == "__main__":
    print(run())
