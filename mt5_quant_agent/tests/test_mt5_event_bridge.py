"""Tests for the MT5 event/threading bridge (2026-07-22).

Covers the four race-elimination primitives:
  1. MT5TradeEvent dataclass + validation
  2. intent_queue push/drain semantics
  3. _shared_trade_lock mutual exclusion (RLock reentrance)
  4. start_event_loop / stop_event_loop / subscribe lifecycle
  5. MT5Broker wraps process_approved_signals + close_position with the lock
  6. execution_loop._drain_intents_phase drains the queue correctly
  7. fast_tick_loop pushes intents when an entry fires

These tests do NOT require a live MT5 connection; they stub the
MetaTrader5 import where broker code activates it.
"""

from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_fake_mt5() -> types.ModuleType:
    """Install a stub MetaTrader5 module so ``core.mt5_broker.mt5``
    is non-None during tests. Returns the fake module for tests to
    configure attributes on."""
    fake = types.ModuleType("MetaTrader5")
    fake.TRADE_RETCODE_DONE = 10009
    fake.TRADE_RETCODE_PLACED = 10008
    fake.TRADE_ACTION_DEAL = 1
    fake.TRADE_ACTION_PENDING = 5
    fake.ORDER_TYPE_BUY = 0
    fake.ORDER_TYPE_SELL = 1
    fake.ORDER_TYPE_BUY_LIMIT = 2
    fake.ORDER_TYPE_SELL_LIMIT = 3
    fake.ORDER_TYPE_BUY_STOP = 4
    fake.ORDER_TYPE_SELL_STOP = 5
    fake.ORDER_TIME_GTC = 0
    fake.ORDER_FILLING_FOK = 0
    fake.ORDER_FILLING_IOC = 1
    fake.ORDER_FILLING_RETURN = 2
    fake.POSITION_TYPE_BUY = 0
    fake.POSITION_TYPE_SELL = 1
    fake.initialize = lambda *a, **kw: True
    fake.last_error = lambda: (0, "ok")
    fake.terminal_info = lambda: None
    fake.account_info = lambda: _FakeAccount()
    fake.symbol_info = lambda s: None
    fake.symbol_info_tick = lambda s: None
    fake.symbol_select = lambda s, b: True
    fake.positions_get = lambda: []
    fake.orders_get = lambda: []
    fake.history_deals_get = lambda *a, **kw: []
    fake.order_send = lambda req: _FakeOrderResult(req)
    # Persist in sys.modules so subsequent imports pick it up.
    sys.modules["MetaTrader5"] = fake
    return fake


class _FakeAccount:
    login = 12345
    server = "demo"
    balance = 1000.0
    equity = 1000.0
    trade_mode = 0
    trade_allowed = True


class _FakeOrderResult:
    def __init__(self, req):
        self.order = 555
        self.deal = 666
        self.volume = req.get("volume", 0.01)
        self.price = req.get("price", 1.0)
        self.retcode = 10009  # TRADE_RETCODE_DONE
        self.comment = "fake"


# Make MetaTrader5 importable for all tests in this module at parametrize.
@pytest.fixture(autouse=True)
def _ensure_fake_mt5():
    """Inject a stub MetaTrader5 module before each test so the
    broker import path doesn't short-circuit to ``mt5 is None``."""
    if "MetaTrader5" not in sys.modules:
        _install_fake_mt5()
    # Also patch the bound symbol on already-imported mt5_broker.
    import core.mt5_broker as mb
    if getattr(mb, "mt5", None) is None:
        mb.mt5 = sys.modules["MetaTrader5"]


# ---------------------------------------------------------------------------
# 1. MT5TradeEvent validation
# ---------------------------------------------------------------------------
def test_mt5_trade_event_validates_event_type():
    from core.mt5_terminal_manager import MT5TradeEvent
    ev = MT5TradeEvent(event_type="position_opened", ticket="123")
    assert ev.event_type == "position_opened"
    assert ev.ticket == "123"
    assert ev.to_dict()["event_type"] == "position_opened"
    with pytest.raises(ValueError, match="unknown MT5TradeEvent.event_type"):
        MT5TradeEvent(event_type="not_a_real_event")


def test_mt5_trade_event_is_frozen():
    from core.mt5_terminal_manager import MT5TradeEvent
    ev = MT5TradeEvent(event_type="position_opened", ticket="1")
    with pytest.raises(Exception):
        ev.ticket = "2"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. intent_queue push/drain semantics
# ---------------------------------------------------------------------------
def test_intent_queue_push_and_drain_fifo():
    """Push 5 intents, drain 5 — order preserved."""
    from core.mt5_terminal_manager import MT5TerminalManager
    import queue as _q
    MT5TerminalManager._shared_intent_queue = _q.Queue(maxsize=1000)

    mgr = MT5TerminalManager(_cfg_stub(), _log_silent())
    for i in range(5):
        mgr.push_intent({"action": "open", "symbol": f"S{i}", "i": i})

    drained = mgr.drain_intents(max_items=10)
    assert len(drained) == 5
    assert [d["i"] for d in drained] == [0, 1, 2, 3, 4]
    assert mgr.drain_intents(max_items=10) == []


def test_intent_queue_full_raises():
    """queue.Full propagates to caller — operators decide what to do."""
    from core.mt5_terminal_manager import MT5TerminalManager
    import queue as _q
    MT5TerminalManager._shared_intent_queue = _q.Queue(maxsize=2)

    mgr = MT5TerminalManager(_cfg_stub(), _log_silent())
    mgr.push_intent({"a": 1})
    mgr.push_intent({"a": 2})
    with pytest.raises(_q.Full):
        mgr.push_intent({"a": 3})


# ---------------------------------------------------------------------------
# 3. _shared_trade_lock: mutual exclusion + RLock reentrance
# ---------------------------------------------------------------------------
def test_trade_lock_serializes_concurrent_callers():
    """Two threads cannot be inside the lock at the same time."""
    from core.mt5_terminal_manager import MT5TerminalManager

    lock = MT5TerminalManager._shared_trade_lock
    assert lock.acquire(blocking=False)
    try:
        acquired: list = []
        def worker():
            acquired.append(lock.acquire(blocking=False))
        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=1.0)
        assert acquired == [False], (
            f"non-holder thread acquired the lock; expected [False], got {acquired}"
        )
    finally:
        lock.release()


def test_trade_lock_is_reentrant():
    """RLock allows the SAME thread to acquire twice — needed when
    process_approved_signals internally calls close_position."""
    from core.mt5_terminal_manager import MT5TerminalManager
    lock = MT5TerminalManager._shared_trade_lock
    if not lock.acquire(blocking=False):
        pytest.skip("lock already held by another test")
    try:
        # Reentrant acquire from same thread must succeed.
        assert lock.acquire(blocking=False) is True
        lock.release()
        lock.release()
    finally:
        # Defensive double-release cleanup
        try:
            while True:
                lock.release()
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# 4. start_event_loop / stop_event_loop + subscribe lifecycle
# ---------------------------------------------------------------------------
def test_subscribe_returns_unsubscribe_callable():
    from core.mt5_terminal_manager import MT5TerminalManager, MT5TradeEvent
    mgr = MT5TerminalManager(_cfg_stub(), _log_silent())
    received: list = []
    def cb(ev): received.append(ev)
    unsub = mgr.subscribe_trade_transactions(cb)
    assert mgr.dispatch_for_test(MT5TradeEvent(event_type="position_opened", ticket="99")) is True
    assert any(ev.event_type == "position_opened" for ev in received)
    unsub()
    received.clear()
    mgr.dispatch_for_test(MT5TradeEvent(event_type="position_opened", ticket="100"))
    assert received == [], "subscriber was not de-registered"


def test_event_loop_starts_and_stops(monkeypatch):
    """start_event_loop spawns a thread that we can stop cleanly.

    We disable the per-poll-call to MetaTrader5 so the worker is
    pure-Python and never blocks on network."""
    from core.mt5_terminal_manager import MT5TerminalManager
    mgr = MT5TerminalManager(_cfg_stub(), _log_silent())

    started = mgr.start_event_loop(poll_interval_sec=0.05, candle_interval_sec=0.10, connect_now=False)
    assert started is True, f"first start_event_loop should return True; got {started}"
    assert mgr._event_thread is not None
    assert mgr._event_thread.is_alive()

    # Second call should be a no-op (already running)
    started2 = mgr.start_event_loop(poll_interval_sec=0.05, candle_interval_sec=0.10, connect_now=False)
    assert started2 is False

    stopped = mgr.stop_event_loop(timeout=2.0)
    assert stopped is True
    assert mgr._event_thread is None


def test_dispatch_runs_all_subscribers_in_order():
    """Events fan out to every registered subscriber; exceptions in
    one don't crash others."""
    from core.mt5_terminal_manager import MT5TerminalManager, MT5TradeEvent
    mgr = MT5TerminalManager(_cfg_stub(), _log_silent())
    out_a: list = []
    out_b: list = []

    def cb_a(ev): out_a.append(ev.event_type)
    def cb_b(ev): out_b.append(ev.event_type); raise RuntimeError("boom")

    mgr.subscribe_trade_transactions(cb_a)
    mgr.subscribe_trade_transactions(cb_b)
    mgr.dispatch_for_test(MT5TradeEvent(event_type="position_opened"))
    assert "position_opened" in out_a
    assert out_b == ["position_opened"], (
        f"B should still have received the event despite raising; got {out_b}"
    )


def test_subscribe_rejects_non_callable():
    from core.mt5_terminal_manager import MT5TerminalManager
    mgr = MT5TerminalManager(_cfg_stub(), _log_silent())
    with pytest.raises(TypeError, match="expected a callable"):
        mgr.subscribe_trade_transactions("not_callable")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5. MT5Broker wraps process_approved_signals + close_position with the lock
# ---------------------------------------------------------------------------
def test_broker_wraps_process_approved_signals_with_lock(monkeypatch):
    """process_approved_signals must acquire the shared lock around
    the unsafe impl. The stub records whether the lock was held at entry."""
    import core.mt5_broker as mb
    from core import mt5_terminal_manager as mt
    held_inside: list[bool] = []
    lock = mt.MT5TerminalManager._shared_trade_lock

    def _unsafe(self, approved, *args, **kwargs):
        # When the wrapper holds the lock, a second acquire from this
        # thread (now RLock) succeeds — but the original lock object's
        # _is_owned() returns True if the calling thread owns it.
        held_inside.append(lock._is_owned())
        return {"timestamp": "now", "mode": "mt5", "placed": [],
                "orders": [], "positions": [], "errors": [],
                "trades": [], "new_closed_trades": [],
                "account": {"login": 1, "server": "demo",
                            "balance": 1000.0, "equity": 1000.0,
                            "account_mode": "demo"},
                "balance": {"cash": 1000.0, "equity": 1000.0,
                            "starting_cash": 1000.0}}

    monkeypatch.setattr(
        mb.MT5Broker, "_process_approved_signals_unsafe", _unsafe
    )

    # Stub the few remaining collaborators the unsafe impl would call.
    monkeypatch.setattr(mb, "enrich_positions_with_orders", lambda pos, orders: pos or [])
    monkeypatch.setattr(mb, "is_duplicate_position", lambda *a, **kw: False)
    monkeypatch.setattr(mb, "symbol_capacity_available", lambda *a, **kw: True)
    monkeypatch.setattr(mb, "session_trade_capacity_available", lambda *a, **kw: True)
    mb.entry_gates = lambda cfg, pos, sig: (True, "", {})
    # Stub TradeTracker as no-op
    class _NoOpTracker:
        def __init__(self, *a, **kw): pass
        def sync_mt5_closed_deals(self, trades, magic): return trades, []
    monkeypatch.setattr(mb, "TradeTracker", _NoOpTracker)
    monkeypatch.setattr(mb, "write_json_state", lambda name, obj: None)
    monkeypatch.setattr(mb, "read_json_state",
                        lambda name, default=None: {"symbols": {}} if name == "features.json" else (default or {}))
    monkeypatch.setattr(mb, "record_position_open", lambda *a, **kw: None)

    broker = mb.MT5Broker(_broker_cfg(), _log_silent())
    broker.process_approved_signals([])
    assert held_inside == [True], (
        f"process_approved_signals did not hold the lock; held_inside={held_inside}"
    )


def test_broker_wraps_close_position_with_lock(monkeypatch):
    import core.mt5_broker as mb
    from core import mt5_terminal_manager as mt
    held_inside: list[bool] = []
    lock = mt.MT5TerminalManager._shared_trade_lock

    def _unsafe(self, ticket, symbol, side, volume, reason):
        held_inside.append(lock._is_owned())
        return {"success": True, "ticket": ticket}

    monkeypatch.setattr(mb.MT5Broker, "_close_position_unsafe", _unsafe)
    monkeypatch.setattr(mb, "blue_guardian_enabled", lambda cfg: False)
    monkeypatch.setattr(mb, "can_close_position", lambda cfg, pos, reason: (True, "ok"))

    broker = mb.MT5Broker(_broker_cfg(), _log_silent())
    result = broker.close_position(123, "XAUUSDm", "BUY", 0.01)
    assert result["success"] is True
    assert held_inside == [True], (
        f"close_position did not hold the lock; held_inside={held_inside}"
    )


# ---------------------------------------------------------------------------
# 6. execution_loop._drain_intents_phase drains the queue correctly
# ---------------------------------------------------------------------------
def test_drain_intents_phase_drains_and_calls_broker(monkeypatch):
    """If the queue has 2 opens, _drain_intents_phase calls
    process_approved_signals with those 2 signs."""
    from loops import execution_loop
    from core.mt5_terminal_manager import MT5TerminalManager
    import queue as _q
    MT5TerminalManager._shared_intent_queue = _q.Queue(maxsize=100)

    MT5TerminalManager._shared_intent_queue.put_nowait({
        "action": "open", "symbol": "XAUUSDm", "side": "BUY",
    })
    MT5TerminalManager._shared_intent_queue.put_nowait({
        "action": "open", "symbol": "EURUSDm", "side": "SELL",
    })

    captured: list = []
    class _FakeBroker:
        def __init__(self, *a, **kw): pass
        def process_approved_signals(self, approved):
            captured.append(approved)
            return {"placed": [{"x": 1}] * len(approved)}
        def close_position(self, *a, **kw):
            return {"success": True}

    monkeypatch.setattr(execution_loop, "MT5Broker", _FakeBroker)
    cfg = {"execution": {"mode": "mt5"}}
    log = _log_silent()
    out = execution_loop._drain_intents_phase(cfg, "mt5", log)

    assert out["drained"] == 2
    assert out["placed"] == 2
    assert out["mode_effective"] == "mt5"
    assert out["broker_invoked"] is True
    assert len(captured) == 1
    assert len(captured[0]) == 2
    # Each approved row is wrapped as {"signal": <intent>}, so symbol lives nested.
    assert {a["signal"]["symbol"] for a in captured[0]} == {"XAUUSDm", "EURUSDm"}


def test_drain_intents_phase_handles_close_intent(monkeypatch):
    from loops import execution_loop
    from core.mt5_terminal_manager import MT5TerminalManager
    import queue as _q
    MT5TerminalManager._shared_intent_queue = _q.Queue(maxsize=100)
    MT5TerminalManager._shared_intent_queue.put_nowait({
        "action": "close", "ticket": 999, "symbol": "XAUUSDm",
        "side": "BUY", "volume": 0.05, "reason": "signal_reversal",
    })

    closes_called: list = []
    class _FakeBroker:
        def __init__(self, *a, **kw): pass
        def process_approved_signals(self, approved):
            return {"placed": []}
        def close_position(self, ticket, symbol, side, volume, reason):
            closes_called.append({
                "ticket": ticket, "symbol": symbol, "reason": reason,
            })
            return {"success": True}

    monkeypatch.setattr(execution_loop, "MT5Broker", _FakeBroker)
    cfg = {"execution": {"mode": "mt5"}}
    log = _log_silent()
    out = execution_loop._drain_intents_phase(cfg, "mt5", log)
    assert out["drained"] == 1
    assert out["placed"] == 1
    assert out["mode_effective"] == "mt5"
    assert out["broker_invoked"] is True
    assert len(closes_called) == 1
    assert closes_called[0]["ticket"] == 999
    assert closes_called[0]["reason"] == "signal_reversal"


def test_drain_intents_phase_empty_returns_none():
    from loops import execution_loop
    from core.mt5_terminal_manager import MT5TerminalManager
    import queue as _q
    MT5TerminalManager._shared_intent_queue = _q.Queue(maxsize=100)
    out = execution_loop._drain_intents_phase({}, "mt5", _log_silent())
    assert out is None


def test_drain_intents_phase_paper_returns_no_broker(monkeypatch):
    """Paper mode must return broker_invoked=False so dashboard can distinguish."""
    from loops import execution_loop
    from core.mt5_terminal_manager import MT5TerminalManager
    import queue as _q
    MT5TerminalManager._shared_intent_queue = _q.Queue(maxsize=100)
    MT5TerminalManager._shared_intent_queue.put_nowait({
        "action": "open", "symbol": "XAUUSDm", "side": "BUY",
    })

    broker_invoked_called: list = []
    class _FakeBroker:
        def __init__(self, *a, **kw): broker_invoked_called.append("ctor")

    monkeypatch.setattr(execution_loop, "MT5Broker", _FakeBroker)
    out = execution_loop._drain_intents_phase({"execution": {"mode": "paper"}},
                                              "paper", _log_silent())
    assert out["mode_effective"] == "paper"
    assert out["broker_invoked"] is False
    assert out["placed"] == 0
    assert out["drained"] == 1
    assert broker_invoked_called == [], (
        f"paper mode must not construct MT5Broker; got {broker_invoked_called}"
    )


# ---------------------------------------------------------------------------
# 7. fast_tick_loop pushes intents when an entry fires
# ---------------------------------------------------------------------------
def test_fast_tick_loop_pushes_intent_for_enter_decision(monkeypatch):
    """When evaluate_entry returns action=enter, fast_tick_loop must
    push an intent onto MT5TerminalManager.intent_queue BEFORE calling
    execute_fast_entry."""
    import loops.fast_tick_loop as ftl
    from core.mt5_terminal_manager import MT5TerminalManager
    import queue as _q
    MT5TerminalManager._shared_intent_queue = _q.Queue(maxsize=100)

    cache = {"symbols": {"XAUUSDm": {"signal_id": "S1", "side": "BUY"}}}

    monkeypatch.setattr(ftl, "fast_mode_enabled", lambda cfg: True)
    monkeypatch.setattr(ftl, "fast_mode_settings", lambda cfg: {"live_enabled": False})
    monkeypatch.setattr(ftl, "fast_mode_symbols", lambda cfg: ["XAUUSDm"])
    monkeypatch.setattr(ftl, "read_cache", lambda cfg: cache)
    monkeypatch.setattr(ftl, "prune_expired", lambda c: c)
    monkeypatch.setattr(ftl, "read_fast_state", lambda: {})
    monkeypatch.setattr(ftl, "_tick_prices", lambda cfg, syms, log: {
        "XAUUSDm": {"mid": 1.0, "bid": 0.999, "ask": 1.001,
                    "spread_points": 5.0},
    })
    monkeypatch.setattr(ftl, "read_json_state",
                        lambda name, default=None: {"symbols": {
            "XAUUSDm": {"close": 1.0, "atr": 0.001, "spread_points": 5.0}
        }} if name == "features.json" else (default or {}))
    monkeypatch.setattr(ftl, "evaluate_entry", lambda *a, **kw: {
        "timestamp": "now", "symbol": "XAUUSDm", "side": "BUY",
        "action": "enter", "reasons": ["signal"],
    })
    monkeypatch.setattr(ftl, "append_event", lambda *a, **kw: None)
    monkeypatch.setattr(ftl, "_write_decisions", lambda doc, cfg: None)

    executed: list = []

    def _fake_execute(dec, entry, *, config, state, logger):
        executed.append({"symbol": dec.get("symbol")})
        return {"blocked": False, "skipped": True}

    fake_exec_mod = types.ModuleType("core.fast_live_executor")
    fake_exec_mod.execute_fast_entry = _fake_execute
    sys.modules["core.fast_live_executor"] = fake_exec_mod

    ftl.run({})
    pushed = list(MT5TerminalManager._shared_intent_queue.queue)
    assert any(it.get("action") == "open" and it["symbol"] == "XAUUSDm"
               for it in pushed), (
        f"fast_tick_loop did not push an open intent; queue={pushed}"
    )
    assert executed == [{"symbol": "XAUUSDm"}]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _log_silent():
    class _L:
        def info(self, *a, **kw): pass
        def error(self, *a, **kw): pass
        def debug(self, *a, **kw): pass
        def warning(self, *a, **kw): pass
    return _L()


def _cfg_stub():
    class _C:
        def get(self, *a, **kw): return {}
    return _C()


def _broker_cfg() -> dict:
    return {
        "execution": {
            "mode": "mt5",
            "magic_number": 12345,
            "deviation": 20,
            "mt5_trading_enabled": True,
            "live_trading_enabled": False,
        },
        "mt5": {"account_mode": "demo", "symbols": []},
    }
