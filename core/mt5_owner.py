"""MT5Owner — process-global single owner of the MetaTrader5 module connection.

The ``MetaTrader5`` Python API is process-global: one ``mt5.initialize()``
serves every worker in the process, and ANY ``mt5.shutdown()`` tears the
connection down for ALL of them. Historically every loop created its own
``MT5ConnectionManager`` and called ``connect()``/``disconnect()``, so a
single ``disconnect()`` from e.g. ``fast_position_guard`` unplugged the
session ``fast_tick_loop`` was holding — the "revolving door" (2026-08-04
runtime audit: 20:19:05 Persistent MT5 connection established →
20:19:07 fast_position_guard Disconnected → 20:19:08 fast_tick_loop
Disconnected, repeating every few seconds while 12 symbols traded).

This module is the ONE owner of the MT5 module connection:

* exactly one ``mt5.initialize()`` per process (reference-counted);
* ``release()`` NEVER calls ``mt5.shutdown()`` — the session stays alive
  for every worker;
* ``shutdown()`` is the ONLY ``mt5.shutdown()`` in the process and runs
  once at application exit (see start.py / run_all.py);
* every MT5 call is serialized through locks (``_lock`` for reads,
  ``_mutation_lock`` for ``order_send``).

Workers must go through ``MT5Owner.instance()`` — never call
``mt5.initialize()`` / ``mt5.shutdown()`` themselves.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any

_MT5_IMPORT_ERROR: Exception | None = None
try:
    import MetaTrader5 as mt5
except ImportError as exc:  # pragma: no cover - exercised via unavailable path
    mt5 = None  # type: ignore
    _MT5_IMPORT_ERROR = exc

IPC_TIMEOUT_CODE = -10005


class MT5Owner:
    """Process-global singleton that owns the MetaTrader5 module connection."""

    _instance: "MT5Owner | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        # Serializes every MT5 module read AND the connect/disconnect lifecycle.
        self._lock = threading.RLock()
        # Dedicated mutation lock: order_send is never interleaved with another
        # order_send (or with a read) even across worker threads.
        self._mutation_lock = threading.RLock()
        self._refcount = 0
        self._initialized = False
        self._terminal_path: str | None = None
        self._connect_latency_ms: float | None = None

    # ----- singleton ------------------------------------------------------
    @classmethod
    def instance(cls) -> "MT5Owner":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ----- introspection ---------------------------------------------------
    @property
    def connected(self) -> bool:
        with self._lock:
            return self._initialized

    @property
    def refcount(self) -> int:
        with self._lock:
            return self._refcount

    @property
    def terminal_path(self) -> str | None:
        with self._lock:
            return self._terminal_path

    @property
    def connect_latency_ms(self) -> float | None:
        with self._lock:
            return self._connect_latency_ms

    # ----- lifecycle -------------------------------------------------------
    def acquire(self, config: dict[str, Any], logger: logging.Logger | None = None) -> bool:
        """Attach a worker to the shared session.

        The real ``mt5.initialize()`` runs only on the FIRST acquire; every
        later acquire just bumps the reference count and returns immediately.
        Raises ``ConnectionError`` when MetaTrader5 is not installed.
        """
        with self._lock:
            if self._initialized:
                self._refcount += 1
                return True
            if mt5 is None:
                detail = str(_MT5_IMPORT_ERROR or "module import returned None")
                raise ConnectionError(
                    "MetaTrader5 is unavailable in this Python runtime "
                    f"({sys.executable}). Import error: {detail}. "
                    "Install it into this interpreter with "
                    f'"{sys.executable}" -m pip install -r requirements.txt'
                )
            ok = self._connect_once(config, logger)
            if ok:
                self._initialized = True
                self._refcount += 1
                self._log_connected(config, logger)
            return ok

    def release(self) -> None:
        """Detach a worker. NEVER calls ``mt5.shutdown()`` — the session
        stays alive for every other worker."""
        with self._lock:
            if self._refcount > 0:
                self._refcount -= 1

    def shutdown(self) -> None:
        """Final teardown — the ONLY ``mt5.shutdown()`` in the process.

        Called once by the main application during full shutdown. Idempotent.
        """
        with self._lock:
            if not self._initialized or mt5 is None:
                self._initialized = False
                self._refcount = 0
                return
            try:
                mt5.shutdown()
            except Exception as exc:  # pragma: no cover - defensive
                logger = logging.getLogger("mt5_owner")
                logger.debug("mt5.shutdown error: %s", exc)
            finally:
                self._initialized = False
                self._refcount = 0
                self._terminal_path = None

    def reconnect(self, config: dict[str, Any], logger: logging.Logger | None = None) -> bool:
        """Force a fresh initialize (worker detected a dead session).

        The owner tears down its own (dead) session and re-initializes once.
        Callers still hold their reference across the reconnect. NOTE: a dead
        session is by definition already unusable, so the refcount is reset to
        0 then 1 — any other workers' counts are dropped. This is intentional
        bookkeeping (the session is re-established for everyone and only the
        process-wide shutdown cares about the count); a caller that still holds
        a stale reference is exactly what triggered the reconnect.
        """
        with self._lock:
            if self._initialized and mt5 is not None:
                try:
                    mt5.shutdown()
                except Exception:  # pragma: no cover - defensive
                    pass
                self._initialized = False
                self._refcount = 0
            ok = self._connect_once(config, logger)
            if ok:
                self._initialized = True
                self._refcount += 1
                self._log_connected(config, logger)
            return ok

    # ----- serialized read/write passthroughs ------------------------------
    def positions_get(self, **kwargs: Any) -> Any:
        with self._lock:
            return mt5.positions_get(**kwargs) if mt5 is not None else None

    def orders_get(self, **kwargs: Any) -> Any:
        with self._lock:
            return mt5.orders_get(**kwargs) if mt5 is not None else None

    def history_deals_get(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return mt5.history_deals_get(*args, **kwargs) if mt5 is not None else None

    def history_orders_get(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return mt5.history_orders_get(*args, **kwargs) if mt5 is not None else None

    def account_info(self) -> Any:
        with self._lock:
            return mt5.account_info() if mt5 is not None else None

    def terminal_info(self) -> Any:
        with self._lock:
            return mt5.terminal_info() if mt5 is not None else None

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        with self._lock:
            return bool(mt5.symbol_select(symbol, enable)) if mt5 is not None else False

    def symbol_info(self, symbol: str) -> Any:
        with self._lock:
            return mt5.symbol_info(symbol) if mt5 is not None else None

    def symbol_info_tick(self, symbol: str) -> Any:
        with self._lock:
            return mt5.symbol_info_tick(symbol) if mt5 is not None else None

    def symbols_get(self, **kwargs: Any) -> Any:
        with self._lock:
            return mt5.symbols_get(**kwargs) if mt5 is not None else None

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start: int, count: int) -> Any:
        with self._lock:
            return mt5.copy_rates_from_pos(symbol, timeframe, start, count) if mt5 is not None else None

    def order_send(self, request: dict[str, Any]) -> Any:
        # Single global mutation chokepoint (2026-08-04 review fix): MT5Broker
        # serializes its own order_send under
        # ``MT5TerminalManager._shared_trade_lock``, while the position_manager
        # paths reach order_send through this owner. Taking the SAME lock here
        # (reentrant RLock) means both routes serialize on one lock — no two
        # threads can reach ``mt5.order_send`` concurrently. Deferred import
        # mirrors ``_connect_once`` and avoids any import cycle.
        from core.mt5_terminal_manager import MT5TerminalManager

        with MT5TerminalManager._shared_trade_lock:
            with self._mutation_lock:
                return mt5.order_send(request) if mt5 is not None else None

    def last_error(self) -> Any:
        with self._lock:
            return mt5.last_error() if mt5 is not None else None

    def timeframe(self, name: str) -> Any:
        """Resolve a TIMEFRAME_* constant (e.g. 'TIMEFRAME_M5')."""
        if mt5 is None:
            return None
        return getattr(mt5, name, None)

    # ----- internals -------------------------------------------------------
    def _connect_once(self, config: dict[str, Any], logger: logging.Logger | None) -> bool:
        """Run the real connect retry (path discovery + initialize).

        Caller holds ``self._lock`` and ``self._initialized`` is False.
        Returns True on success. Raises ConnectionError with a formatted
        message when every path/mode attempt fails.
        """
        log = logger or logging.getLogger("mt5_owner")
        from core.mt5_terminal_manager import MT5TerminalManager, get_python_session_id

        terminal_manager = MT5TerminalManager(config, log)
        alignment = terminal_manager.session_alignment()
        log.info(
            "Session alignment: python_session=%s aligned=%s processes=%s",
            alignment.get("python_session_id"),
            alignment.get("aligned"),
            alignment.get("mt5_processes"),
        )
        if alignment.get("warning"):
            log.warning(alignment["warning"])

        if not alignment.get("aligned") and config.get("mt5", {}).get("auto_launch_terminal", False):
            terminal_manager.ensure_terminal(auto_launch=True)
            alignment = terminal_manager.session_alignment()

        timeout = int(config.get("mt5", {}).get("timeout_ms", 120000))
        paths = terminal_manager.discover_paths()
        if not paths:
            raise ConnectionError("No terminal64.exe found")

        mt5_cfg = config.get("mt5", {})
        logged_in_only = bool(mt5_cfg.get("use_logged_in_account", True))
        modes = (False,) if logged_in_only else (False, True)

        if logged_in_only:
            log.info("Attaching to logged-in MT5 account (no stored-credentials re-login)")

        last_error: Any = "no attempts"
        for path in paths:
            for use_creds in modes:
                t0 = time.perf_counter()
                err = self._try_path(path, timeout, use_creds, config, log)
                elapsed = (time.perf_counter() - t0) * 1000
                if self._initialized:
                    self._connect_latency_ms = round(elapsed, 1)
                    return True
                last_error = err

        msg = self._format_error(last_error, alignment)
        raise ConnectionError(msg)

    def _try_path(self, path: str, timeout: int, use_credentials: bool, config: dict[str, Any], logger: logging.Logger) -> Any:
        from core.mt5_terminal_manager import get_python_session_id

        mode = "explicit_login" if use_credentials else "logged_in_session"
        logger.info(
            "Connect attempt: path=%s mode=%s timeout=%d session=%s",
            path,
            mode,
            timeout,
            get_python_session_id(),
        )
        mt5.shutdown()
        kwargs: dict[str, Any] = {"path": path, "timeout": timeout}
        if use_credentials:
            creds = self._credentials(config)
            if not creds:
                return "credentials not configured"
            kwargs.update(creds)

        if not mt5.initialize(**kwargs):
            err = mt5.last_error()
            logger.warning("initialize failed: %s %s", path, err)
            return err

        account = mt5.account_info()
        if account is None:
            mt5.shutdown()
            return "no account info"

        self._initialized = True
        self._terminal_path = path
        return "ok"

    def _log_connected(self, config: dict[str, Any], logger: logging.Logger | None) -> None:
        log = logger or logging.getLogger("mt5_owner")
        try:
            account = mt5.account_info() if mt5 is not None else None
            login = int(account.login) if account is not None else None
            server = str(account.server) if account is not None else None
            balance = float(account.balance) if account is not None else None
            mode = {0: "demo", 1: "contest", 2: "real"}.get(
                int(getattr(account, "trade_mode", -1)), "unknown"
            ) if account is not None else "unknown"
            log.info(
                "MT5 owner connected: login=%s server=%s balance=%.2f mode=%s path=%s",
                login, server, balance or 0.0, mode, self._terminal_path,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("MT5 owner connect log skipped: %s", exc)

    def _credentials(self, config: dict[str, Any]) -> dict[str, Any]:
        mt5_cfg = config.get("mt5", {})
        if bool(mt5_cfg.get("use_logged_in_account", True)) or bool(
            mt5_cfg.get("ignore_stored_credentials", True)
        ):
            return {}
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
