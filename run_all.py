"""Agent 8: Orchestrator — run all loops continuously + dashboard.

Usage:
  python run_all.py          # continuous loops + dashboard (default)
  python run_all.py --once   # single cycle then exit

Loop order (each cycle):
  data_loop -> feature_loop -> market_context_loop -> signal_loop ->
  verifier_loop -> execution_loop -> risk_loop -> memory_loop -> health_loop

Every N cycles (config): history_loop, research_loop
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.mt5_terminal_manager import MT5TerminalManager
from core.utils import ensure_dirs, load_config, read_json_state, setup_logger
from loops import (
    data_loop,
    execution_loop,
    feature_loop,
    health_loop,
    history_loop,
    market_context_loop,
    memory_loop,
    research_loop,
    risk_loop,
    self_learning_loop,
    signal_loop,
    verifier_loop,
)

DATA_LOOP_INTERACTIVE_SCRIPT = ROOT / "scripts" / "run_data_loop_interactive.ps1"

LOOPS = [
    ("data_loop", data_loop.run),
    ("feature_loop", feature_loop.run),
    ("market_context_loop", market_context_loop.run),
    ("signal_loop", signal_loop.run),
    ("verifier_loop", verifier_loop.run),
    ("execution_loop", execution_loop.run),
    ("risk_loop", risk_loop.run),
    ("memory_loop", memory_loop.run),
    ("health_loop", lambda: health_loop.run(connect=True)),
    ("self_learning_loop", self_learning_loop.run),
]

_shutdown = False


def _handle_signal(signum, frame) -> None:
    global _shutdown
    _shutdown = True


def _log_data_loop_session_hint(logger, config: dict) -> None:
    terminal_mgr = MT5TerminalManager(config, logger)
    alignment = terminal_mgr.session_alignment()
    aligned = alignment.get("aligned", False)
    logger.info(
        "data_loop session: aligned=%s python_session=%s mt5_processes=%d",
        aligned,
        alignment.get("python_session_id"),
        len(alignment.get("mt5_processes", [])),
    )
    if alignment.get("warning"):
        logger.warning("data_loop session: %s", alignment["warning"])
    if not aligned:
        logger.warning(
            "Windows Server: run data_loop via %s (or align MT5 session first)",
            DATA_LOOP_INTERACTIVE_SCRIPT,
        )


def _start_dashboard(config: dict, logger) -> str | None:
    dash_cfg = config.get("app", {}).get("dashboard", {})
    if not dash_cfg.get("enabled", True):
        return None

    host = dash_cfg.get("host", "127.0.0.1")
    port = int(dash_cfg.get("port", 8080))
    url = f"http://{host}:{port}"

    def _serve() -> None:
        try:
            from dashboard.server import run
            run(host=host, port=port)
        except Exception as exc:
            logger.error("Dashboard server failed: %s", exc)

    thread = threading.Thread(target=_serve, name="dashboard", daemon=True)
    thread.start()
    time.sleep(0.6)

    if dash_cfg.get("auto_open_browser", True):
        threading.Thread(
            target=lambda: webbrowser.open(url),
            name="open_browser",
            daemon=True,
        ).start()

    logger.info("Dashboard started at %s", url)
    return url


def update_state_md(results: dict[str, str], cycle: int = 0, dashboard_url: str | None = None) -> None:
    """Write human-readable system state summary from all state files."""
    config = load_config()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    candles = read_json_state("latest_candles.json", default={})
    features = read_json_state("features.json", default={})
    market_ctx = read_json_state("market_context.json", default={})
    candidates = read_json_state("candidate_signals.json", default={})
    approved = read_json_state("approved_signals.json", default={})
    rejected = read_json_state("rejected_signals.json", default={})
    positions = read_json_state("paper_positions.json", default={})
    orders = read_json_state("paper_orders.json", default={})
    trades = read_json_state("paper_trades.json", default={})
    risk = read_json_state("risk_state.json", default={})
    kill = read_json_state("kill_switch.json", default={})
    memory = read_json_state("memory.json", default={})
    edge = read_json_state("edge_scores.json", default={})
    health = read_json_state("health.json", default={})
    account = read_json_state("account.json", default={})
    history = read_json_state("history_status.json", default={})
    broker_symbols = read_json_state("broker_symbols.json", default={})
    edge_db = read_json_state("edge_database.json", default={})

    balance = orders.get("balance", {})
    candle_account = candles.get("account", {})
    health_account = health.get("account", {})
    mt5_health = health.get("mt5", {})
    conn_health = health.get("connection", {})
    candles_health = health.get("candles", {})

    lines = [
        "# MT5 Quant Agent — System State",
        "",
        f"**Last run:** {now}",
        f"**Cycle:** {cycle}",
        f"**Mode:** {config['execution']['mode']} (mt5_trading={config['execution'].get('mt5_trading_enabled', False)})",
    ]
    if dashboard_url:
        lines.append(f"**Dashboard:** {dashboard_url}")
    lines.extend(["", "## Loop Status", ""])
    for name, status in results.items():
        lines.append(f"- {name}: **{status}**")

    lines.extend([
        "",
        "## Health",
        "",
        f"- Status: **{health.get('status', 'unknown')}**",
        f"- Issues: {len(health.get('issues', []))}",
    ])
    for issue in health.get("issues", [])[:5]:
        lines.append(f"  - {issue}")

    lines.extend([
        "",
        "## MT5 / Account",
        "",
        f"- Connection: alive={conn_health.get('alive')} logged_in={conn_health.get('logged_in')}",
    ])
    acct = account or health_account or candle_account
    if acct:
        lines.append(
            f"- Account: {acct.get('login')} @ {acct.get('server')} "
            f"balance={acct.get('balance')} {acct.get('currency', 'USD')}"
        )

    lines.extend([
        "",
        "## Signals",
        "",
        f"- Candidates: {candidates.get('count', 0)}",
        f"- Approved: {approved.get('count', 0)}",
        f"- Rejected: {rejected.get('count', 0)}",
        "",
        "## Portfolio",
        "",
        f"- Equity: ${balance.get('equity', config['execution']['starting_cash']):.2f}",
        f"- Open positions: {len(positions.get('positions', []))}",
        f"- Closed trades: {len(trades.get('trades', []))}",
        "",
        "## Risk",
        "",
        f"- Kill switch: **{kill.get('kill_switch', risk.get('kill_switch', False))}**",
        f"- Exposure used: {risk.get('exposure_used_pct', 0):.1f}%",
        f"- Drawdown: {risk.get('drawdown', 0):.2f}%",
        "",
        "## Memory / Edge DB",
        "",
        f"- Memory records: {memory.get('total_records', 0)}",
        f"- Edge DB records: {edge_db.get('count', len(edge_db.get('records', [])))}",
    ])
    setup_stats = edge.get("setup_stats", {}).get("global", {})
    for setup, s in list(setup_stats.items())[:5]:
        if s.get("total", 0) > 0:
            lines.append(f"  - {setup}: win rate {s.get('win_rate_pct')}% ({s['wins']}/{s['total']})")

    state_path = ROOT / "docs" / "STATE.md"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_cycle(
    config: dict,
    logger,
    cycle: int = 0,
    *,
    include_history: bool = False,
    include_research: bool = False,
) -> dict[str, str]:
    """Execute one full loop cycle."""
    results: dict[str, str] = {}

    if include_history and config.get("history", {}).get("enabled", True):
        try:
            logger.info("--- Running history_loop (cycle %d) ---", cycle)
            history_loop.run()
            results["history_loop"] = "OK"
        except Exception as exc:
            results["history_loop"] = f"FAILED: {exc}"
            logger.error("history_loop failed: %s", exc)

    for name, fn in LOOPS:
        try:
            logger.info("--- Running %s ---", name)
            if name == "data_loop":
                _log_data_loop_session_hint(logger, config)
            fn()
            results[name] = "OK"
            logger.info("--- %s completed ---", name)
        except Exception as exc:
            results[name] = f"FAILED: {exc}"
            logger.error("--- %s failed: %s ---", name, exc)
            logger.error(traceback.format_exc())
            if name == "data_loop":
                logger.error(
                    "data_loop failed — try: powershell -ExecutionPolicy Bypass -File %s",
                    DATA_LOOP_INTERACTIVE_SCRIPT,
                )

    if include_research:
        try:
            logger.info("--- Running research_loop (cycle %d) ---", cycle)
            research_loop.run()
            results["research_loop"] = "OK"
        except Exception as exc:
            results["research_loop"] = f"FAILED: {exc}"
            logger.error("research_loop failed: %s", exc)

    return results


def run_all(once: bool = False) -> dict[str, str]:
    """Run agent loops; continuous by default with dashboard."""
    global _shutdown
    ensure_dirs()
    config = load_config()
    logger = setup_logger("orchestrator", "system.log")
    app_cfg = config.get("app", {})
    interval = int(app_cfg.get("loop_interval_seconds", 60))
    history_every = int(app_cfg.get("history_every_cycles", 5))
    research_every = int(app_cfg.get("research_every_cycles", 30))

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("=== MT5 Quant Agent orchestrator starting ===")
    logger.info("Mode: %s | once=%s | interval=%ds", config["execution"]["mode"], once, interval)

    dashboard_url = _start_dashboard(config, logger)
    if dashboard_url:
        print(f"\n  Dashboard: {dashboard_url}\n")

    cycle = 0
    last_results: dict[str, str] = {}

    try:
        while not _shutdown:
            cycle += 1
            logger.info("=== Cycle %d starting ===", cycle)
            t0 = time.monotonic()

            last_results = run_cycle(
                config,
                logger,
                cycle,
                include_history=(cycle == 1 or cycle % history_every == 0),
                include_research=(cycle > 1 and cycle % research_every == 0),
            )
            update_state_md(last_results, cycle, dashboard_url)
            elapsed = time.monotonic() - t0
            logger.info("=== Cycle %d complete (%.1fs) ===", cycle, elapsed)
            logger.info("Results: %s", last_results)

            if once:
                break

            sleep_for = max(1.0, interval - elapsed)
            logger.info("Sleeping %.0fs until next cycle (Ctrl+C to stop)", sleep_for)
            end = time.monotonic() + sleep_for
            while time.monotonic() < end and not _shutdown:
                time.sleep(0.5)
    finally:
        # Single-owner contract (2026-08-04): the ONLY mt5.shutdown() in the
        # process, at app exit, after all loops have stopped. Runs in a finally
        # so an unhandled exception mid-run cannot leak the connection.
        logger.info("=== Orchestrator stopped ===")
        try:
            from core.mt5_owner import MT5Owner

            MT5Owner.instance().shutdown()
            logger.info("MT5Owner shut down (session released)")
        except Exception as exc:
            logger.debug("MT5Owner shutdown skipped: %s", exc)
    return last_results


if __name__ == "__main__":
    print("Note: use python start.py or START_AGENT.bat for MT5 Quant OS supervisor.")
    parser = argparse.ArgumentParser(description="MT5 Quant Agent — legacy orchestrator")
    parser.add_argument("--once", action="store_true", help="Run a single cycle then exit")
    args = parser.parse_args()
    run_all(once=args.once)