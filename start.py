"""MT5 Quant OS — single entry point. Starts supervisor, all services, and dashboard.

Usage:
  python start.py
  START_AGENT.bat
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
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


def _configure_stdio() -> None:
    """Avoid UnicodeEncodeError when logging to cp1252 consoles (Windows)."""
    if sys.platform != "win32":
        return
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


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
    _safe_print(BANNER)
    _safe_print(f"  MT5 QUANT OS v{version}")
    _safe_print("  -----------------------------------------")
    if mode:
        label = mode.get("label", "practice")
        tag = {
            "practice": "PRACTICE",
            "growth": "GROWTH",
            "micro_growth": "MICRO GROWTH",
            "micro_live": "MICRO LIVE",
        }.get(label, "LIVE PLAN")
        _safe_print(f"  Mode: {tag} ({mode.get('account_mode', 'demo')} account)")
        if mode.get("active_profile"):
            src = mode.get("profile_source", "")
            src_tag = f" [{src}]" if src else ""
            _safe_print(f"  Profile: {mode['active_profile']}{src_tag}")
        if mode.get("account_login"):
            _safe_print(
                f"  MT5 login: {mode['account_login']}  "
                f"equity: ${float(mode.get('account_equity') or 0):.2f}"
            )
        _safe_print(f"  {mode.get('detail', '')}")
        _safe_print("  -----------------------------------------")
    services = [
        ("MT5 Connection", "pending"),
        ("Dashboard", dashboard_url or "starting…"),
        ("History Engine", "scheduled"),
        ("Trading Pipeline", "scheduled"),
        ("Research Engine", "scheduled"),
        ("Health Monitor", "active"),
    ]
    for label, status in services:
        _safe_print(f"  OK {label:<22} {status}")
    if remote and remote.get("tailscale_connected"):
        _safe_print("  -----------------------------------------")
        _safe_print("  Phone (Tailscale)")
        _safe_print(f"  Dashboard  {remote['dashboard_url']}")
        _safe_print(f"  MT5 / RDP  {remote['rdp_target']}  (Microsoft Remote Desktop app)")
    _safe_print("  -----------------------------------------")
    _safe_print("  Listening...  (Ctrl+C to stop)\n")


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
    _configure_stdio()
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
    from core.fast_mode import fast_mode_enabled, tick_interval_seconds
    fast_interval = tick_interval_seconds(config) if fast_mode_enabled(config) else 0.0

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
        session_report = ensure_practice_session(config, logger, force_rebaseline=False)
        if session_report.get("kill_switch_cleared") or session_report.get("rebaseline"):
            logger.info("Practice session ready: %s", session_report)

    dash_port = int(config.get("app", {}).get("dashboard", {}).get("port", 8080))
    remote = remote_access_info(dash_port, config)
    write_json_state("remote_access.json", remote)

    dashboard_url = _start_dashboard(config, logger)
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

    if fast_interval > 0:
        def _fast_mode() -> dict:
            from loops import fast_position_guard, fast_tick_loop
            tick_result = fast_tick_loop.run(config) or {}
            guard_result = fast_position_guard.run(config) or {}
            return {"fast_tick_loop": "OK", "fast_position_guard": "OK", **tick_result, **guard_result}

        supervisor.register(ManagedService(
            "fast_mode",
            "Fast Scalper",
            _fast_mode,
            fast_interval,
            logger,
        ))
        logger.info("Fast mode service registered (interval=%.2fs)", fast_interval)

    # Phase 2.4 normalized learning loop (observe-only by default). Never places
    # trades; only records/scores/proposes. Enabled when learning.enabled=true.
    learn_cfg = config.get("learning") or {}
    if learn_cfg.get("enabled", False):
        learn_interval = float(learn_cfg.get("loop_interval_seconds", 120))

        def _learning_review() -> dict:
            from loops import learning_review_loop
            return learning_review_loop.run() or {}

        supervisor.register(ManagedService(
            "learning_review",
            "Learning Review",
            _learning_review,
            learn_interval,
            logger,
        ))
        logger.info(
            "Learning review loop registered (interval=%.0fs, mode=%s)",
            learn_interval, learn_cfg.get("mode", "observe_only"),
        )

    # Self-learning loop (reward-weighted weights + learning self-monitor +
    # shadow experiments). OBSERVE-ONLY: proposes/reports, never trades or
    # writes live config. Enabled by default; disable via self_learning.enabled.
    sl_cfg = config.get("self_learning") or {}
    if sl_cfg.get("enabled", True):
        sl_interval = float(sl_cfg.get("loop_interval_seconds", 300))

        def _self_learning() -> dict:
            from loops import self_learning_loop
            return self_learning_loop.run(config) or {}

        supervisor.register(ManagedService(
            "self_learning",
            "Self-Learning",
            _self_learning,
            sl_interval,
            logger,
        ))
        logger.info(
            "Self-learning loop registered (interval=%.0fs, observe_only=True)",
            sl_interval,
        )

    # Heartbeat loop — writes state/heartbeat.json with service status, uptime,
    # and memory usage. Visible as a loop tile on the dashboard. Runs every 15s
    # by default; disable via heartbeat_loop.enabled: false.
    hb_cfg = config.get("heartbeat_loop") or {}
    if hb_cfg.get("enabled", True):
        hb_interval = float(hb_cfg.get("loop_interval_seconds", 15))

        def _heartbeat() -> dict:
            from loops import heartbeat_loop
            return heartbeat_loop.run(config) or {}

        supervisor.register(ManagedService(
            "heartbeat_loop",
            "Heartbeat",
            _heartbeat,
            hb_interval,
            logger,
        ))
        logger.info("Heartbeat loop registered (interval=%.0fs)", hb_interval)

    # Read-only MT5 audit loop — snapshots terminal/account/history/journal data
    # without calling order_send or changing trading state.
    audit_cfg = config.get("mt5_audit_loop") or {}
    if audit_cfg.get("enabled", True) and not once:
        audit_interval = float(audit_cfg.get("loop_interval_seconds", 300))

        def _mt5_audit() -> dict:
            from loops import mt5_audit_loop
            return mt5_audit_loop.run(config) or {}

        supervisor.register(ManagedService(
            "mt5_audit_loop",
            "MT5 Audit",
            _mt5_audit,
            audit_interval,
            logger,
        ))
        logger.info("MT5 read-only audit loop registered (interval=%.0fs)", audit_interval)

    # Hourly specialized setup observer — analytical only. It reads the normal
    # feature/context snapshots, classifies existing setup types, and never
    # starts MT5 or submits orders.
    specialized_cfg = config.get("specialized_setup_loop") or {}
    if specialized_cfg.get("enabled", True) and not once:
        specialized_interval = float(specialized_cfg.get("loop_interval_seconds", 3600))

        def _specialized_setup() -> dict:
            from loops import specialized_setup_loop
            return specialized_setup_loop.run(config) or {}

        supervisor.register(ManagedService(
            "specialized_setup_loop",
            "Specialized Setups",
            _specialized_setup,
            specialized_interval,
            logger,
        ))
        logger.info(
            "Specialized setup loop registered (interval=%.0fs, observe_only=True)",
            specialized_interval,
        )

    # Offline validation loop — runs in a child-test process and only writes
    # validation_cycle/history artifacts. It never starts MT5 or submits orders.
    # Keep it in the supervisor so the five-minute cadence survives restarts.
    validation_cfg = config.get("validation_loop") or {}
    if validation_cfg.get("enabled", True) and not once:
        validation_interval = float(validation_cfg.get("loop_interval_seconds", 300))

        def _validation() -> dict:
            # Execute outside the trading process. The worker runs focused
            # pytest checks and 10k consistency checks, then exits; this keeps
            # pytest imports/GIL/temporary state out of the live supervisor.
            timeout = max(30, int(validation_cfg.get("test_timeout_seconds", 120)) + 30)
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "loops.validation_loop"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    check=False,
                    env={**os.environ, "MT5_QUANT_VALIDATION": "1"},
                )
            except subprocess.TimeoutExpired as exc:
                write_json_state("validation_cycle.json", {
                    "timestamp": __import__("core.utils", fromlist=["utc_now_iso"]).utc_now_iso(),
                    "status": "attention",
                    "safety": {"offline_only": True, "mt5_started": False, "orders_submitted": 0, "config_changed": False},
                    "tests": {"status": "timeout", "summary": str(exc)},
                    "warnings": ["offline validation worker timed out; no trading action was taken"],
                    "next_action": "manual_review",
                    "auto_fix": {"enabled": False, "reason": "validation never edits source, config, or trading ledgers"},
                })
                logger.warning("Offline validation worker timed out after %ss", timeout)
                return {"validation_loop": "error"}
            except OSError as exc:
                write_json_state("validation_cycle.json", {
                    "timestamp": __import__("core.utils", fromlist=["utc_now_iso"]).utc_now_iso(),
                    "status": "attention",
                    "safety": {"offline_only": True, "mt5_started": False, "orders_submitted": 0, "config_changed": False},
                    "tests": {"status": "error", "summary": str(exc)},
                    "warnings": ["offline validation worker could not start; no trading action was taken"],
                    "next_action": "manual_review",
                    "auto_fix": {"enabled": False, "reason": "validation never edits source, config, or trading ledgers"},
                })
                logger.warning("Offline validation worker could not start: %s", exc)
                return {"validation_loop": "error"}
            if proc.returncode:
                logger.warning(
                    "Offline validation worker failed (exit=%s): %s",
                    proc.returncode,
                    (proc.stdout + "\\n" + proc.stderr)[-1500:],
                )
                return {"validation_loop": "error"}
            if proc.stdout.strip():
                logger.info("Offline validation worker: %s", proc.stdout.strip()[-1000:])
            # The detailed verdict is persisted in validation_cycle.json. Keep
            # findings as report data; only a crashed worker is a service error.
            return {"validation_loop": "OK"}

        supervisor.register(ManagedService(
            "validation_loop",
            "Offline Validation",
            _validation,
            validation_interval,
            logger,
        ))
        logger.info(
            "Offline validation loop registered (interval=%.0fs, instances=%s)",
            validation_interval,
            validation_cfg.get("instance_count", 10000),
        )

    # Live-trade thesis reviewer (net-new 2026-07-13). Re-scores OPEN positions
    # against the current regime/trend to detect thesis decay. OBSERVE-ONLY by
    # default (never closes unless config.thesis_reviewer.live_close_enabled).
    thesis_cfg = config.get("thesis_reviewer") or {}
    if thesis_cfg.get("enabled", False):
        thesis_interval = float(thesis_cfg.get("loop_interval_seconds", 60))

        def _thesis_review() -> dict:
            from loops import thesis_review_loop
            return thesis_review_loop.run(config) or {}

        supervisor.register(ManagedService(
            "thesis_review",
            "Thesis Review",
            _thesis_review,
            thesis_interval,
            logger,
        ))
        logger.info(
            "Thesis review loop registered (interval=%.0fs, live_close=%s)",
            thesis_interval, thesis_cfg.get("live_close_enabled", False),
        )

    supervisor.start_all()

    try:
        while not _shutdown:
            time.sleep(0.5)
            if once and pipeline_svc.state.status in ("ok", "error", "stopped"):
                break
    finally:
        logger.info("Shutting down MT5 Quant OS…")
        supervisor.stop_all()
        # Single-owner contract (2026-08-04): MT5Owner.shutdown() is the ONLY
        # mt5.shutdown() in the process. Runs exactly once here, at app exit,
        # after every service has stopped — so no worker is left holding a
        # torn-down session.
        try:
            from core.mt5_owner import MT5Owner

            MT5Owner.instance().shutdown()
            logger.info("MT5Owner shut down (session released)")
        except Exception as exc:
            logger.debug("MT5Owner shutdown skipped: %s", exc)
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
