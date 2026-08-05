"""MT5 Connection Manager — attach to the logged-in demo account.

This is now a thin facade over :class:`core.mt5_owner.MT5Owner`, the
process-global single owner of the MetaTrader5 module connection.

KEY BEHAVIOUR CHANGE (2026-08-04 ownership repair): ``disconnect()`` is a
*reference release*, NOT an ``mt5.shutdown()``. The MetaTrader5 Python API
is process-global — one ``initialize()`` serves every worker and any
``shutdown()`` tears the connection down for ALL of them. Before this
repair, ``fast_position_guard`` connected+disconnected every 1s tick and
its ``disconnect()`` unplugged the persistent session that
``fast_tick_loop`` was holding (the "revolving door" seen in the
2026-08-04 runtime audit). All lifecycle now flows through
``MT5Owner.instance()`` and the only ``mt5.shutdown()`` in the process
happens once at application exit.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any

from core.mt5_owner import MT5Owner, _MT5_IMPORT_ERROR, IPC_TIMEOUT_CODE, mt5

# Backwards-compatible module-level names (tests and diagnostics patch these).
_MT5_SESSION_LOCK = threading.RLock()


class MT5ConnectionManager:
    """Facade over the process-global MT5Owner singleton.

    ``connect()`` attaches this caller to the shared session (reference
    counted). ``disconnect()`` releases the reference — it NEVER calls
    ``mt5.shutdown()``, so other workers keep their live connection.
    """

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("mt5_connection_manager")
        self._owner = MT5Owner.instance()
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
        if self._connected:
            return True
        if mt5 is None:
            detail = str(_MT5_IMPORT_ERROR or "module import returned None")
            raise ConnectionError(
                "MetaTrader5 is unavailable in this Python runtime "
                f"({sys.executable}). Import error: {detail}. "
                "Install it into this interpreter with "
                f'"{sys.executable}" -m pip install -r requirements.txt'
            )
        ok = self._owner.acquire(self.config, self.logger)
        if ok:
            self._connected = True
            self._terminal_path = self._owner.terminal_path
            self._connect_latency_ms = self._owner.connect_latency_ms
        return ok

    def disconnect(self) -> None:
        """Release this caller's reference to the shared MT5 session.

        This is intentionally NOT an ``mt5.shutdown()``. The session stays
        alive for every other worker; the single process-wide shutdown
        happens in ``MT5Owner.shutdown()`` at application exit.
        """
        self._owner.release()
        self._connected = False
        self.logger.info(
            "Released MT5 reference (shared session retained by MT5Owner, "
            "refcount=%d)",
            self._owner.refcount,
        )

    def reconnect(self) -> bool:
        """Force the owner to re-establish a dead session (owner-owned)."""
        ok = self._owner.reconnect(self.config, self.logger)
        if ok:
            self._connected = True
            self._terminal_path = self._owner.terminal_path
            self._connect_latency_ms = self._owner.connect_latency_ms
        else:
            self._connected = False
        return ok

    def account_snapshot(self) -> dict[str, Any]:
        if not self._connected or mt5 is None:
            return {"login": None, "server": None, "balance": None, "account_mode": None}
        account = self._owner.account_info()
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
        account = self._owner.account_info()
        terminal = self._owner.terminal_info()
        return {
            "alive": account is not None,
            "logged_in": account is not None and account.login > 0,
            "trade_allowed": bool(account.trade_allowed) if account else False,
            "connected": bool(terminal.connected) if terminal else False,
            "build": int(terminal.build) if terminal else None,
        }

    # ----- legacy convenience passthroughs (serialized via owner) ----------
    def positions_get(self, **kwargs: Any) -> Any:
        return self._owner.positions_get(**kwargs)

    def account_info(self) -> Any:
        return self._owner.account_info()

    def terminal_info(self) -> Any:
        return self._owner.terminal_info()

    def last_error(self) -> Any:
        return self._owner.last_error()
