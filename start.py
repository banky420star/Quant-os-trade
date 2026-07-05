"""MT5 Quant OS — single entry point. Starts supervisor, all services, and dashboard.

Usage:
  python start.py
  START_AGENT.bat
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.account_mode import performance_gates_active, runtime_mode_summary, validate_runtime_profile
from core.profile_launcher import apply_logged_in_account_mode, auto_select_profile
from core.profile_guard import assert_profile
from core.remote_access import remote_access_info
from core.practice_session import ensure_practice_session
from core.pipeline import run_pipeline
from core.supervisor import ManagedService, Supervisor
from core.utils import ensure_dirs, load_config, setup_logger, write_json_state

_shutdown = False

BANNER = r"""
 ███╗   ███╗████████╗███████╗     ██████╗ ██╗   ██╗ █████╗ ███╗   ██╗████████╗     ██████╗ ███████╗
 ████╗ ████║╚══██╔══╝██╔════╝    ██╔═══██╗██║   ██║██╔══██╗████╗  ██║╚══██╔══╝    ██╔═══██╗██╔════╝
 ██╔████╔██║   ██║   █████╗      ██║   ██║██║   ██║███████║██╔██╗ ██║   ██║       ██║   ██║███████╗
 ██║╚██╔╝██║   ██║   ██╔══╝      ██║▄▄ ██║██║   ██║██╔══██║██║╚██╗██║   ██║       ██║   ██║╚════██║
 ██║ ╚═╝ ██║   ██║   ███████╗    ╚██████╔╝╚██████╔╝██║  ██║██║ ╚████║   ██║       ╚██████╔╝███████║
 ╚═╝     ╚═╝   ╚═╝   ╚══════╝     ╚══▀▀═╝  ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═══╝   ╚═╝        ╚═════╝ ╚══════╝
"""


def _print_banner(
    version: str,
    dashboard_url: str | None,
    mode: dict | None = None,
    remote: dict | None = None,
) -> None:
    print(BANNER)
    print(f"  MT5 QUANT OS v{version}")
    print("  ─────────────────────────────────────────")
    if mode:
        label = mode.get("label", "practice")
        tag = {
            "practice": "PRACTICE",
            "growth": "GROWTH",
            "micro_growth": "MICRO GROWTH",
        }.get(label, "LIVE PLAN")
        print(f"  Mode: {tag} ({mode.get('account_mode', 'demo')} account)")
        if mode.get("active_profile"):
            src = mode.get("profile_source", "")
            src_tag = f" [{src}]" if src else ""
            print(f"  Profile: {mode['active_profile']}{src_tag}")
        if mode.get("account_login"):
            print(
                f"  MT5 login: {mode['account_login']}  "
                f"equity: ${float(mode.get('account_equity') or 0):.2f}"
            )
        print(f"  {mode.get('detail', '')}")
        print("  ─────────────────────────────────────────")
    services = [
        ("MT5 Connection", "pending"),
        ("Dashboard", dashboard_url or "starting…"),
        ("History Engine", "scheduled"),
        ("Trading Pipeline", "scheduled"),
        ("Research Engine", "scheduled"),
        ("Health Monitor", "active"),
    ]
    for label, status in services:
        print(f"  ✓ {label:<22} {status}")
    if remote and remote.get("tailscale_connected"):
        print("  ─────────────────────────────────────────")
        print("  Phone (Tailscale)")
        print(f"  Dashboard  {remote['dashboard_url']}")
        print(f"  MT5 / RDP  {remote['rdp_target']}  (Microsoft Remote Desktop app)")
    print("  ─────────────────────────────────────────")
    print("  Listening…  (Ctrl+C to stop)\n")


def _handle_signal(signum, frame) -> None:
    global _shutdown
    _shutdown = True


def _start_dashboard(config: dict, logger) -> str | None:
    dash_cfg = config.get("app", {}).get("dashboard", {})
    if not dash_cfg.get("enabled", True):
        return None

    host = dash_cfg.get("host", "127.0.0.1")
    port = int(dash_cfg.get("port", 8080))
    bind = "0.0.0.0" if host in ("0.0.0.0", "::") else host
    url = f"http://127.0.0.1:{port}" if bind == "0.0.0.0" else f"http://{host}:{port}"

    def _serve() -> None:
        from dashboard.server import run
        run(host=bind, port=port)

    # Guard duplicate launch: if the port is already bound, another start.py /
    # dashboard is already serving -- log and skip instead of raising OSError
    # inside the daemon thread (which dies silently and leaves no dashboard).
    import socket
    _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Match HTTPServer.allow_reuse_address=True: without SO_REUSEADDR a stale
    # TIME_WAIT from a crashed dashboard would make this bind-only probe report
    # "port in use" even though the real server (which sets allow_reuse_address)
    # could have bound -- a false negative that silently kills the dashboard.
    _probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        _probe.bind((bind, port))
    except OSError:
        logger.warning("Dashboard port %s already in use -- not launching a second one.", port)
        return url
    finally:
        _probe.close()

    threading.Thread(target=_serve, name="dashboard", daemon=True).start()
    time.sleep(0.5)

    if dash_cfg.get("auto_open_browser", True):
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    logger.info("Dashboard at %s", url)
    return url


def _write_agent_lock() -> None:
    write_json_state("agent_lock.json", {
        "pid": os.getpid(),
        "started_at": __import__("core.utils", fromlist=["utc_now_iso"]).utc_now_iso(),
        "entry": "start.py",
    })


def start(once: bool = False, profile: str | None = None) -> None:
    global _shutdown
    ensure_dirs()
    logger = setup_logger("quant_os", "system.log")
    selection = auto_select_profile(explicit=profile, logger=logger)
    if selection.get("account"):
        write_json_state("profile_selection.json", {
            "profile": selection["profile"],
            "source": selection["source"],
            "account_login": selection["account"].get("login"),
            "account_mode": selection["account"].get("account_mode"),
            "equity": selection["account"].get("equity") or selection["account"].get("balance"),
            "selected_at": __import__("core.utils", fromlist=["utc_now_iso"]).utc_now_iso(),
        })
    _write_agent_lock()
    config = load_config()
    config = apply_logged_in_account_mode(config, selection.get("account"))
    app_cfg = config.get("app", {})
    sup_cfg = app_cfg.get("supervisor", {})

    interval = float(app_cfg.get("loop_interval_seconds", 60))
    history_interval = float(sup_cfg.get("history_interval_seconds", 300))
    research_interval = float(sup_cfg.get("research_interval_seconds", 1800))

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    mode = runtime_mode_summary(config)
    if config.get("active_profile"):
        mode["active_profile"] = config["active_profile"]
    mode["profile_source"] = selection.get("source")
    if selection.get("account"):
        mode["account_login"] = selection["account"].get("login")
        mode["account_equity"] = (
            selection["account"].get("equity") or selection["account"].get("balance")
        )
    validate_runtime_profile(config)
    # USER-AUTHORIZED 2026-07-01: refuse to boot on the dangerous real+growth+
    # live-trading combination (the 2026-06-30 wipe scenario) and enforce the
    # explicit config.mode label. See core/profile_guard.py.
    assert_profile(config)
    write_json_state("runtime_mode.json", mode)
    logger.info("Runtime mode: %s — %s", mode["label"], mode["detail"])
    if not performance_gates_active(config):
        session_report = ensure_practice_session(config, logger, force_rebaseline=True)
        if session_report.get("kill_switch_cleared") or session_report.get("rebaseline"):
            logger.info("Practice session ready: %s", session_report)

    dash_port = int(config.get("app", {}).get("dashboard", {}).get("port", 8080))
    remote = remote_access_info(dash_port)
    write_json_state("remote_access.json", remote)

    dashboard_url = _start_dashboard(config, logger)
    if remote.get("tailscale_connected"):
        dashboard_url = remote["dashboard_url"]
    _print_banner(app_cfg.get("version", "1.0"), dashboard_url, mode, remote)

    supervisor = Supervisor(config, logger)

    def _pipeline() -> dict:
        return run_pipeline(config, logger)

    def _history() -> dict:
        from loops import history_loop
        history_loop.run()
        return {"history_loop": "OK"}

    def _research() -> dict:
        from loops import research_loop
        research_loop.run()
        return {"research_loop": "OK"}

    pipeline_svc = ManagedService(
        "trading_pipeline",
        "Trading Pipeline",
        _pipeline,
        interval,
        logger,
        run_once=once,
    )
    pipeline_svc._on_result = supervisor._record_loop_results
    supervisor.register(pipeline_svc)

    if not once:
        supervisor.register(ManagedService(
            "history_engine",
            "History Engine",
            _history,
            history_interval,
            logger,
        ))
        supervisor.register(ManagedService(
            "research_engine",
            "Research Engine",
            _research,
            research_interval,
            logger,
        ))

    supervisor.start_all()

    try:
        while not _shutdown:
            time.sleep(0.5)
            if once and pipeline_svc.state.status in ("ok", "error", "stopped"):
                break
    finally:
        logger.info("Shutting down MT5 Quant OS…")
        supervisor.stop_all()
        print("\n  MT5 Quant OS stopped.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MT5 Quant OS")
    parser.add_argument("--once", action="store_true", help="Run one pipeline cycle then exit")
    parser.add_argument(
        "--profile",
        default=None,
        help=(
            "Profile overlay (30, 100, growth, live, 30-c1, etc.). "
            "Omit or pass 'auto' to detect from the logged-in MT5 account."
        ),
    )
    args = parser.parse_args()
    start(once=args.once, profile=args.profile)
