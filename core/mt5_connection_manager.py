"""MT5 Connection Manager — attach to logged-in MT5 account, never place trades."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from core.mt5_terminal_manager import MT5TerminalManager, get_python_session_id

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore

IPC_TIMEOUT_CODE = -10005


class MT5ConnectionManager:
    """Initialize MT5 API connection using the logged-in terminal session."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("mt5_connection_manager")
        self.terminal_manager = MT5TerminalManager(config, logger)
        self._connected = False
        self._terminal_path: str | None = None
        self._connect_latency_ms: float | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def terminal_path(self) -> str | None:
        return self._terminal_path

    def connect(self) -> bool:
        if mt5 is None:
            raise ConnectionError("MetaTrader5 package not installed")

        import time

        alignment = self.terminal_manager.session_alignment()
        self.logger.info(
            "Session alignment: python_session=%s aligned=%s processes=%s",
            alignment.get("python_session_id"),
            alignment.get("aligned"),
            alignment.get("mt5_processes"),
        )
        if alignment.get("warning"):
            self.logger.warning(alignment["warning"])

        if not alignment.get("aligned") and self.config.get("mt5", {}).get("auto_launch_terminal", True):
            self.terminal_manager.ensure_terminal(auto_launch=True)
            alignment = self.terminal_manager.session_alignment()

        timeout = int(self.config.get("mt5", {}).get("timeout_ms", 120000))
        paths = self.terminal_manager.discover_paths()
        if not paths:
            raise ConnectionError("No terminal64.exe found")

        mt5_cfg = self.config.get("mt5", {})
        logged_in_only = bool(mt5_cfg.get("use_logged_in_account", True))
        modes = (False,) if logged_in_only else (False, True)

        if logged_in_only:
            expected_mode = str(self.config.get("mt5", {}).get("account_mode", "unknown"))
            self.logger.info(
                "Attaching to logged-in MT5 account (expected_mode=%s, no forced re-login)",
                expected_mode,
            )

        last_error: Any = "no attempts"
        for path in paths:
            for use_creds in modes:
                t0 = time.perf_counter()
                err = self._try_path(path, timeout, use_creds)
                elapsed = (time.perf_counter() - t0) * 1000
                if self._connected:
                    self._connect_latency_ms = round(elapsed, 1)
                    return True
                last_error = err

        msg = self._format_error(last_error, alignment)
        raise ConnectionError(msg)

    def disconnect(self) -> None:
        if mt5 and self._connected:
            mt5.shutdown()
            self._connected = False
            self.logger.info("Disconnected from MT5")

    def account_snapshot(self) -> dict[str, Any]:
        if not self._connected or mt5 is None:
            return {"login": None, "server": None, "balance": None, "account_mode": None}
        account = mt5.account_info()
        if account is None:
            return {"login": None, "server": None, "balance": None, "account_mode": None}

        mode_map = {0: "demo", 1: "contest", 2: "real"}
        trade_mode = int(getattr(account, "trade_mode", -1))
        return {
            "login": int(account.login),
            "server": str(account.server),
            "balance": float(account.balance),
            "equity": float(getattr(account, "equity", account.balance)),
            "currency": str(getattr(account, "currency", "")),
            "account_mode": mode_map.get(trade_mode, "unknown"),
            "trade_allowed": bool(getattr(account, "trade_allowed", False)),
            "connected_via": "logged_in_session",
            "terminal_path": self._terminal_path,
            "connect_latency_ms": self._connect_latency_ms,
        }

    def ping(self) -> dict[str, Any]:
        """Lightweight connection health check."""
        if not self._connected or mt5 is None:
            return {"alive": False, "error": "not connected"}
        account = mt5.account_info()
        terminal = mt5.terminal_info()
        return {
            "alive": account is not None,
            "logged_in": account is not None and account.login > 0,
            "trade_allowed": bool(account.trade_allowed) if account else False,
            "connected": bool(terminal.connected) if terminal else False,
            "build": int(terminal.build) if terminal else None,
        }

    def _try_path(self, path: str, timeout: int, use_credentials: bool) -> Any:
        mode = "explicit_login" if use_credentials else "logged_in_session"
        self.logger.info(
            "Connect attempt: path=%s mode=%s timeout=%d session=%s",
            path,
            mode,
            timeout,
            get_python_session_id(),
        )
        mt5.shutdown()
        kwargs: dict[str, Any] = {"path": path, "timeout": timeout}
        if use_credentials:
            creds = self._credentials()
            if not creds:
                return "credentials not configured"
            kwargs.update(creds)

        if not mt5.initialize(**kwargs):
            err = mt5.last_error()
            self.logger.warning("initialize failed: %s %s", path, err)
            return err

        account = mt5.account_info()
        if account is None:
            mt5.shutdown()
            return "no account info"

        self._connected = True
        self._terminal_path = path
        snap = self.account_snapshot()
        self.logger.info(
            "Connected: login=%s server=%s balance=%.2f mode=%s path=%s",
            snap["login"],
            snap["server"],
            snap["balance"],
            snap["account_mode"],
            path,
        )
        return "ok"

    def _credentials(self) -> dict[str, Any]:
        mt5_cfg = self.config.get("mt5", {})
        login = os.environ.get("MT5_LOGIN") or mt5_cfg.get("login")
        password = os.environ.get("MT5_PASSWORD") or mt5_cfg.get("password")
        server = os.environ.get("MT5_SERVER") or mt5_cfg.get("server")
        if not all([login, password, server]):
            return {}
        return {"login": int(login), "password": str(password), "server": str(server)}

    def _format_error(self, error: Any, alignment: dict[str, Any]) -> str:
        if isinstance(error, tuple) and error and error[0] == IPC_TIMEOUT_CODE:
            base = f"MT5 IPC timeout ({error})."
            if alignment.get("warning"):
                return f"{base} {alignment['warning']}"
            return f"{base} Ensure MT5 is running in the same Windows session as Python."
        return f"All connection attempts failed. Last error: {error}"

    @staticmethod
    def is_account_fresh(account: dict[str, Any] | None, max_age_seconds: int = 90) -> bool:
        """True when an account.json dict has a timestamp within max_age_seconds.

        Used by the risk loop to detect a stale on-disk snapshot before
        overwriting it with a fresh MT5 read. Empty or missing snapshots are
        never fresh.
        """
        if not account or not isinstance(account, dict):
            return False
        ts = account.get("timestamp")
        if not ts:
            return False
        try:
            then = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - then).total_seconds()
            return age <= max_age_seconds
        except Exception:
            return False
