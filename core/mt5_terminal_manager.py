"""MT5 Terminal Manager — detect, launch, and recover terminal processes."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from core.utils import PROJECT_ROOT, utc_now_iso

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore


def _session_id_for_pid(pid: int | None) -> int | None:
    if pid is None:
        return None
    try:
        import ctypes

        session_id = ctypes.c_uint()
        if ctypes.windll.kernel32.ProcessIdToSessionId(int(pid), ctypes.byref(session_id)):
            return int(session_id.value)
    except Exception:
        pass
    if psutil:
        try:
            return int(psutil.Process(int(pid)).session_id())
        except Exception:
            pass
    return None


def get_python_session_id() -> int | None:
    return _session_id_for_pid(os.getpid())


class MT5TerminalManager:
    """Manage MT5 terminal64.exe lifecycle and session alignment."""

    DEFAULT_CANDIDATES = (
        r"C:\Users\Administrator\MT5Agent\terminal64.exe",
        r"C:\Program Files\MetaTrader 5\terminal64.exe",
        r"C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe",
        r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
    )

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("mt5_terminal_manager")
        self.mt5_cfg = config.get("mt5", {})

    def list_processes(self) -> list[dict[str, Any]]:
        processes: list[dict[str, Any]] = []
        if not psutil:
            return processes
        for proc in psutil.process_iter(["pid", "name", "exe", "create_time"]):
            if proc.info.get("name") != "terminal64.exe":
                continue
            pid = proc.info.get("pid")
            processes.append(
                {
                    "pid": pid,
                    "session_id": _session_id_for_pid(pid),
                    "exe": proc.info.get("exe"),
                    "alive": proc.is_running(),
                }
            )
        return processes

    def session_alignment(self) -> dict[str, Any]:
        python_session = get_python_session_id()
        mt5_processes = self.list_processes()
        info: dict[str, Any] = {
            "timestamp": utc_now_iso(),
            "python_pid": os.getpid(),
            "python_session_id": python_session,
            "mt5_processes": mt5_processes,
            "aligned": False,
            "recommended_terminal": None,
            "warning": None,
        }

        if python_session is None:
            info["warning"] = "Could not detect Python session ID."
            return info

        interactive = [p for p in mt5_processes if p.get("session_id") not in (None, 0)]
        matching = [p for p in interactive if p.get("session_id") == python_session]
        info["aligned"] = len(matching) > 0

        if matching:
            info["recommended_terminal"] = matching[0].get("exe")
        elif interactive:
            info["recommended_terminal"] = interactive[0].get("exe")

        if not mt5_processes:
            info["warning"] = "No terminal64.exe running. Launch MT5 in your RDP session."
        elif not info["aligned"]:
            desc = ", ".join(
                f"PID={p['pid']} Session={p.get('session_id')}" for p in mt5_processes
            )
            info["warning"] = (
                f"Session mismatch: Python={python_session}, MT5=[{desc}]. "
                "Use scripts/launch_mt5_interactive.ps1 and run_data_loop_interactive.ps1"
            )
        return info

    def discover_paths(self) -> list[str]:
        python_session = get_python_session_id()
        seen: set[str] = set()
        ranked: list[tuple[int, str]] = []

        def add(path: str | None, priority: int = 10) -> None:
            if not path:
                return
            p = Path(path)
            if p.exists() and str(p) not in seen:
                seen.add(str(p))
                ranked.append((priority, str(p)))

        use_logged_in = bool(self.mt5_cfg.get("use_logged_in_account", True))

        if psutil:
            for proc in self.list_processes():
                if proc.get("session_id") == 0:
                    continue
                if use_logged_in:
                    priority = 0 if proc.get("session_id") == python_session else 4
                else:
                    priority = 1 if proc.get("session_id") == python_session else 3
                add(proc.get("exe"), priority)

        cfg_priority = 3 if use_logged_in else 0
        add(os.environ.get("MT5_PATH"), cfg_priority)
        add(self.mt5_cfg.get("path"), cfg_priority)

        for candidate in self.DEFAULT_CANDIDATES:
            add(candidate, 5)

        ranked.sort(key=lambda x: x[0])
        paths = [p for _, p in ranked]
        self.logger.info("Terminal paths (session-aware): %s", paths)
        return paths

    def is_alive(self, path: str | None = None) -> bool:
        processes = self.list_processes()
        if not processes:
            return False

        def _norm(p: str | None) -> str:
            # psutil sometimes returns a process exe WITHOUT the drive letter
            # (e.g. "\Users\Admin\MT5Agent\terminal64.exe") when reading
            # another session's process, which broke an exact-string compare and
            # caused a false "terminal_not_running" health alert. Normalize by
            # stripping the drive, lower-casing, and using forward slashes.
            if not p:
                return ""
            from pathlib import PurePath
            pp = PurePath(p)
            return "/".join(pp.parts[1:]).lower() if len(pp.parts) > 1 and PurePath(p).anchor else str(p).lower().replace("\\", "/")

        target = _norm(path)
        target_base = Path(path).name.lower() if path else ""
        for p in processes:
            if p.get("session_id") in (None, 0):
                continue
            exe = p.get("exe")
            if not exe:
                continue
            n = _norm(exe)
            if (target and n == target) or (target_base and Path(exe).name.lower() == target_base):
                return True
        # No path given, or path didn't match: an interactive terminal process
        # existing at all is enough to consider the terminal alive.
        if not path:
            return any(p.get("session_id") not in (None, 0) for p in processes)
        return False

    def launch_interactive(self) -> bool:
        """Launch MT5 via interactive scheduled task script."""
        script = PROJECT_ROOT / "scripts" / "launch_mt5_interactive.ps1"
        if not script.exists():
            self.logger.error("Launch script missing: %s", script)
            return False
        self.logger.info("Launching MT5 via %s", script)
        result = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.stdout:
            self.logger.info(result.stdout.strip())
        if result.returncode != 0:
            self.logger.warning("Launch script exit=%s stderr=%s", result.returncode, result.stderr)
        return result.returncode == 0

    def ensure_terminal(self, auto_launch: bool = False) -> dict[str, Any]:
        """Ensure an interactive MT5 terminal is running; optionally launch."""
        alignment = self.session_alignment()
        terminal_path = self.mt5_cfg.get("path") or alignment.get("recommended_terminal")

        if alignment["aligned"] and self.is_alive(terminal_path):
            alignment["status"] = "ok"
            return alignment

        if auto_launch and self.mt5_cfg.get("auto_launch_terminal", False):
            self.logger.warning("MT5 not aligned — attempting interactive launch")
            self.launch_interactive()
            alignment = self.session_alignment()

        alignment["status"] = "ok" if alignment["aligned"] else "degraded"
        return alignment

    def ipc_recovery_hint(self) -> str:
        alignment = self.session_alignment()
        if alignment.get("warning"):
            return alignment["warning"]
        return "MT5 terminal aligned and reachable."