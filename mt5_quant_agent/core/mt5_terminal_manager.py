"""MT5 Terminal Manager — detect, launch, and recover terminal processes.

Also hosts the synthetic ``OnTradeTransaction`` Python-side event layer
(see ``MT5TradeEvent`` below). The MQL5 OnTradeTransaction only fires
inside the MT5 terminal; from Python we approximate it with a 100ms
background poller on ``mt5.positions_get`` / ``orders_get`` /
``history_deals_get`` and emit typed ``MT5TradeEvent`` to subscribers.
A future MQL5 socket/EA bridge would only need to replace the worker
internals — the consumer-facing ``subscribe_trade_transactions``
contract stays stable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue as _queue_mod
import subprocess
import threading
import time as _time_mod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

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


# ---------------------------------------------------------------------------
# Synthetic OnTradeTransaction event layer (2026-07-22)
# ---------------------------------------------------------------------------
# `MT5TradeEvent` is the wire-level contract every consumer below operates
# against. The shape mirrors the MQL5 DEAL/ORDER enum names so the same
# consumers can be re-pointed at a future EA socket feed without changing
# call sites.
#
# EVENT TYPES:
#   "position_opened"   — new ticket not previously seen
#   "position_modified" — SL/TP/volume changed (SL ratchet by position_manager)
#   "position_closed"   — ticket no longer in positions_get()
#   "order_placed"      — pending order appeared in orders_get()
#   "order_filled"      — pending order disappeared (filled or expired)
#   "candle_update"     — latest candle tick for a symbol (1Hz refresh)

_VALID_EVENT_TYPES = frozenset({
    "position_opened",
    "position_modified",
    "position_closed",
    "order_placed",
    "order_filled",
    "candle_update",
})


@dataclass(frozen=True)
class MT5TradeEvent:
    event_type: str
    ticket: str = ""
    symbol: str = ""
    side: str = ""
    volume: float = 0.0
    price: float = 0.0
    timestamp: str = ""
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.event_type not in _VALID_EVENT_TYPES:
            raise ValueError(
                f"unknown MT5TradeEvent.event_type={self.event_type!r}; "
                f"valid={sorted(_VALID_EVENT_TYPES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Schema for an execution intent pushed onto terminal_manager.intent_queue.
# Both ``execution_loop.run()`` (slow pipeline) and ``fast_tick_loop`` live
# entries flow through this single chokepoint. The broker-side ``trade_lock``
# guarantees only one intent can mutate MT5 at a time — eliminating the
# fast_mode vs main_pipeline race that historically caused open-and-
# close-immediately behaviour.
IntentDict = dict[str, Any]   # alias for documentation purposes only


class MT5TerminalManager:
    """Manage MT5 terminal64.exe lifecycle and session alignment.

    Class-level singletons (the SHARED lock + intent queue) provide the
    race-elimination primitives every other call site (MT5Broker,
    execution_loop, fast_tick_loop, position_manager) uses. Per-instance
    state holds the subscriber list and the event-loop worker so each
    instance can be stopped independently without affecting the shared
    lock or queue.
    """

    DEFAULT_CANDIDATES = (
        r"C:\Users\Administrator\MT5Agent\terminal64.exe",
        r"C:\Program Files\MetaTrader 5\terminal64.exe",
        r"C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe",
        r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
    )

    # Shared across all MT5TerminalManager instances in a single process so
    # MT5Broker, fast_tick_loop, execution_loop, and position_manager all
    # synchronise on the SAME lock. Without this, each per-process
    # MT5TerminalManager() would create its own RLock and the race would
    # silently return.
    _shared_trade_lock: threading.RLock = threading.RLock()
    _shared_intent_queue: _queue_mod.Queue = _queue_mod.Queue(maxsize=1000)

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("mt5_terminal_manager")
        self.mt5_cfg = config.get("mt5", {})
        # Per-instance shared references — every instance reads/writes the
        # SAME lock + queue so cross-loop serialization works even when
        # multiple MT5TerminalManager() objects coexist in a process.
        self.trade_lock: threading.RLock = MT5TerminalManager._shared_trade_lock
        self.intent_queue: _queue_mod.Queue = MT5TerminalManager._shared_intent_queue
        # Per-instance subscriber list + event-loop worker.
        self._subscribers: list[Callable[[MT5TradeEvent], None]] = []
        self._subscribers_lock = threading.Lock()
        self._event_thread: threading.Thread | None = None
        self._shutdown_event = threading.Event()
        self._started_at: float | None = None
        self.last_event: MT5TradeEvent | None = None
        self._last_positions_hash: str = ""
        self._last_orders_hash: str = ""
        self._last_deals_hash: str = ""

    # ----- Public API used by every consumer -------------------------------
    def subscribe_trade_transactions(
        self, callback: Callable[[MT5TradeEvent], None]
    ) -> Callable[[], None]:
        """Register a sync callback invoked for every MT5TradeEvent emitted.

        Returns an ``unsubscribe()`` callable so callers can de-register.
        Raises if the event loop is not running (callers should always
        start_event_loop() first OR tolerate a no-op subscription).
        """
        if not callable(callback):
            raise TypeError("subscribe_trade_transactions expected a callable")
        with self._subscribers_lock:
            self._subscribers.append(callback)

        def _unsub() -> None:
            with self._subscribers_lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass
        return _unsub

    def push_intent(self, intent: IntentDict) -> None:
        """Push an execution intent onto the shared queue (non-blocking).

        Raises ``queue.Full`` if the queue is at capacity (1000). Callers
        should map that exception to a back-off / drop decision rather
        than crash.
        """
        try:
            self.intent_queue.put_nowait(intent)
        except _queue_mod.Full as exc:  # pragma: no cover - defensive
            self.logger.error(
                "intent_queue FULL (maxsize=%d) — dropping intent: %s",
                self.intent_queue.maxsize, intent,
            )
            raise exc

    def drain_intents(self, *, max_items: int = 50) -> list[IntentDict]:
        """Pop up to ``max_items`` pending intents without blocking.

        Returns the list of intents in FIFO order; consumers should hold
        ``self.trade_lock`` while executing each one.
        """
        drained: list[IntentDict] = []
        for _ in range(max_items):
            try:
                drained.append(self.intent_queue.get_nowait())
            except _queue_mod.Empty:
                break
        return drained

    # ----- Event loop worker (start/stop) ----------------------------------
    def start_event_loop(
        self,
        *,
        poll_interval_sec: float = 0.10,
        candle_interval_sec: float = 1.0,
        connect_now: bool = True,
    ) -> bool:
        """Spawn the background poller. Returns True if a new thread was started.

        The worker connects to MT5 once at start and reuses the connection
        until ``stop_event_loop()`` is called (reconnect on transient drops).
        Safe to call multiple times — subsequent calls are no-ops if the
        thread is already alive.
        """
        if self._event_thread is not None and self._event_thread.is_alive():
            return False
        self._shutdown_event.clear()
        self._started_at = _time_mod.monotonic()
        self._event_thread = threading.Thread(
            target=self._event_loop_worker,
            kwargs={
                "poll_interval_sec": poll_interval_sec,
                "candle_interval_sec": candle_interval_sec,
                "connect_now": connect_now,
            },
            daemon=True,
            name="MT5TradeEventLoop",
        )
        self._event_thread.start()
        self.logger.info(
            "start_event_loop: poll=%.0fms candle=%.0fs connect=%s thread=%s",
            poll_interval_sec * 1000, candle_interval_sec, connect_now,
            self._event_thread.name,
        )
        return True

    def stop_event_loop(self, *, timeout: float = 5.0) -> bool:
        """Signal the worker to shutdown and wait for it to drain."""
        self._shutdown_event.set()
        if self._event_thread is not None:
            self._event_thread.join(timeout=timeout)
            if self._event_thread.is_alive():
                self.logger.warning(
                    "stop_event_loop: worker did not exit within %.1fs", timeout,
                )
                return False
            self._event_thread = None
        return True

    def _dispatch_event(self, event: MT5TradeEvent) -> None:
        """Fan-out an event to every registered subscriber synchronously."""
        self.last_event = event
        with self._subscribers_lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(event)
            except Exception as exc:  # pragma: no cover - subscriber-driven
                self.logger.warning("subscriber error event=%s err=%s", event.event_type, exc)

    def dispatch_for_test(self, event: MT5TradeEvent) -> bool:
        """Test-only alias for ``_dispatch_event`` (public for tests).

        Production code should subscribe via ``subscribe_trade_transactions``
        and let the event-loop worker dispatch. Tests use this to inject
        synthetic events without spinning up the full worker thread.
        """
        self._dispatch_event(event)
        return True

    def _hash_state(self, items: list[Any]) -> str:
        """Stable hash of a list of position/order/deal dicts.

        The hash is purely structural (ticket+symbol+volume+price+state),
        so renumbering events fire position_opened once and stable updates
        don't spam position_modified. Cheap enough to run every poll.
        """
        rows: list[tuple[str, str, float, float, str]] = []
        for it in items or []:
            try:
                rows.append((
                    str(it.get("ticket") or it.get("order") or it.get("deal") or ""),
                    str(it.get("symbol") or ""),
                    float(it.get("volume") or it.get("size") or 0.0),
                    float(it.get("price") or it.get("price_open") or it.get("close_price") or 0.0),
                    str(it.get("state") or it.get("type") or ""),
                ))
            except (TypeError, ValueError):
                continue
        rows.sort()
        payload = json.dumps(rows, default=str, sort_keys=True).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _maybe_poll_event(self, *, attach_mt5: Any = None) -> None:
        """One poll cycle — diff against last hash and fire synthetic events.

        The MT5 package import is optional; tests can pass a stub via the
        ``attach_mt5`` kwarg without needing a real connection.
        """
        mt5 = attach_mt5  # local alias; tests inject here
        ts_now = utc_now_iso()
        if mt5 is None:
            try:
                import MetaTrader5 as mt5
            except ImportError:
                mt5 = None  # type: ignore
        if mt5 is None:
            return  # no package available — quietly no-op

        # ---- positions_get ----
        try:
            positions_snapshot = mt5.positions_get() or []
        except Exception as exc:
            self.logger.debug("positions_get failed: %s", exc)
            positions_snapshot = []
        current_hash = self._hash_state([
            {
                "ticket": getattr(p, "ticket", None),
                "symbol": getattr(p, "symbol", None),
                "volume": getattr(p, "volume", None),
                "price": getattr(p, "price_open", None),
                "state": "open",
            }
            for p in positions_snapshot
        ])

        # ---- orders_get (pending) ----
        try:
            orders_snapshot = mt5.orders_get() or []
        except Exception as exc:
            self.logger.debug("orders_get failed: %s", exc)
            orders_snapshot = []
        orders_hash = self._hash_state([
            {
                "ticket": getattr(o, "ticket", None),
                "symbol": getattr(o, "symbol", None),
                "volume": getattr(o, "volume_initial", None),
                "price": getattr(o, "price_open", None),
                "state": str(getattr(o, "state", None) or ""),
            }
            for o in orders_snapshot
        ])

        # ---- history_deals_get (recent closes) ----
        try:
            deals_snapshot = mt5.history_deals_get(
                int((_time_mod.time() - 60) * 1000), int(_time_mod.time() * 1000)
            ) or []
        except Exception as exc:
            self.logger.debug("history_deals_get failed: %s", exc)
            deals_snapshot = []
        deals_hash = self._hash_state([
            {
                "ticket": getattr(d, "order", None),
                "deal": getattr(d, "ticket", None),
                "symbol": getattr(d, "symbol", None),
                "volume": getattr(d, "volume", None),
                "price": getattr(d, "price", None),
                "state": "deal",
            }
            for d in deals_snapshot
        ])

        if (
            current_hash != self._last_positions_hash
            or orders_hash != self._last_orders_hash
            or deals_hash != self._last_deals_hash
        ):
            # Fire one event per changed via simple per-ticket detection.
            seen_tickets = set()
            for p in positions_snapshot:
                ticket = str(getattr(p, "ticket", "") or "")
                symbol = getattr(p, "symbol", "") or ""
                volume = float(getattr(p, "volume", 0.0) or 0.0)
                price = float(getattr(p, "price_open", 0.0) or 0.0)
                ptype = int(getattr(p, "type", 0) or 0)
                side = "BUY" if ptype == 0 else "SELL"  # 0=BUY, 1=SELL in MT5
                ev = MT5TradeEvent(
                    event_type="position_opened",
                    ticket=ticket, symbol=symbol, side=side,
                    volume=volume, price=price, timestamp=ts_now,
                )
                self._dispatch_event(ev)
                seen_tickets.add(ticket)
            # Deals in the last 60s = "position_closed" events.
            for d in deals_snapshot:
                ev = MT5TradeEvent(
                    event_type="position_closed",
                    ticket=str(getattr(d, "order", "") or ""),
                    symbol=getattr(d, "symbol", "") or "",
                    side="",
                    volume=float(getattr(d, "volume", 0.0) or 0.0),
                    price=float(getattr(d, "price", 0.0) or 0.0),
                    timestamp=ts_now,
                    meta={"deal": getattr(d, "ticket", "") or ""},
                )
                self._dispatch_event(ev)
            self._last_positions_hash = current_hash
            self._last_orders_hash = orders_hash
            self._last_deals_hash = deals_hash

    def _event_loop_worker(
        self,
        *,
        poll_interval_sec: float,
        candle_interval_sec: float,
        connect_now: bool,
    ) -> None:
        """Background worker: connect once, poll, dispatch events.

        The lock + connect are best-effort; if MT5 ever drops, the worker
        backs off and tries again on the next tick. Tests inject a stub
        via ``attach_mt5`` indirectly (via the ``attach_mt5`` kwarg on
        ``_maybe_poll_event``); production paths go through the real
        MetaTrader5 package import.
        """
        mt5_mod = None
        if connect_now:
            try:
                import MetaTrader5 as mt5_mod  # type: ignore
            except ImportError:
                mt5_mod = None
            if mt5_mod is not None:
                try:
                    if not mt5_mod.initialize():
                        self.logger.debug(
                            "event worker: mt5.initialize returned False; "
                            "worker will retry via next poll",
                        )
                except Exception as exc:
                    self.logger.debug("event worker: mt5.initialize exception: %s", exc)

        last_candle_tick = 0.0
        while not self._shutdown_event.is_set():
            self._maybe_poll_event(attach_mt5=mt5_mod)
            if _time_mod.monotonic() - last_candle_tick >= candle_interval_sec:
                self._dispatch_event(MT5TradeEvent(
                    event_type="candle_update",
                    timestamp=utc_now_iso(),
                    meta={"interval_sec": candle_interval_sec},
                ))
                last_candle_tick = _time_mod.monotonic()
            _time_mod.sleep(poll_interval_sec)
        self.logger.info("event_loop_worker: shutdown complete")

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