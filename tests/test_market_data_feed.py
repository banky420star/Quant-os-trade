"""Live data-feed ownership + freshness tests (2026-08-08).

Proves the "ONE PROCESS -> ONE MT5 OWNER -> ONE CANDLE REFRESH WORKER"
contract:

  1. Worker ownership: first start spawns; repeated starts across ANY
     MT5TerminalManager instance never spawn a duplicate; a dead worker is
     deterministically restarted; shutdown terminates THE worker.
  2. Candle refresh: the worker's candle-update path actually invokes the
     refresh function, which is what writes state/latest_candles.json.
  3. Freshness: the market timestamp (newest bar) is the only source of
     freshness — a worker that keeps running while data stays old never
     reports fresh, and the M1 engine gates such a feed to WAIT.
  4. No concurrent duplicate writes: only one refresh stream can exist.

Core tests run without MetaTrader5/pandas. The loops.data_loop integration
tests transitively import pandas (core.history_manager) and are gated with
pytest.importorskip — they run in the production env (Windows/MT5).
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.mt5_terminal_manager import MT5TerminalManager, latest_market_timestamp
from core.utils import read_json_state, utc_now_iso, write_json_state


def _cfg() -> dict:
    return {
        "mt5": {"timeframes": {"entry": "M5", "bias": "M15"}},
        "m1_structure": {"enabled": True},
    }


def _log_silent() -> logging.Logger:
    log = logging.getLogger("test_market_data_feed")
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    return log


def _now_iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _stop_worker() -> None:
    MT5TerminalManager({}, None).stop_event_loop(timeout=2.0)


@pytest.fixture(autouse=True)
def _clean_shared_worker_state(tmp_path, monkeypatch):
    """Hermetic feed state: redirect all state writes to tmp_path and reset
    the process-global worker between tests so assertions are deterministic."""
    from core import utils

    monkeypatch.setattr(utils, "STATE_DIR", tmp_path)
    _stop_worker()
    MT5TerminalManager._feed_health = {}
    MT5TerminalManager.last_event = None
    MT5TerminalManager._shutdown_event = threading.Event()
    MT5TerminalManager._event_thread = None
    MT5TerminalManager._started_at = None
    yield
    _stop_worker()


# ---------------------------------------------------------------------------
# 1. Worker ownership
# ---------------------------------------------------------------------------


class TestWorkerOwnership:
    def test_first_start_spawns_worker(self):
        mgr = MT5TerminalManager(_cfg(), _log_silent())
        started = mgr.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=60.0, connect_now=False,
        )
        assert started is True
        assert mgr._event_thread is not None
        assert mgr._event_thread.is_alive()
        feed = mgr.feed_status()
        assert feed["worker_alive"] is True
        assert feed["worker_started_at"]

    def test_second_start_across_instances_no_duplicate(self):
        a = MT5TerminalManager(_cfg(), _log_silent())
        b = MT5TerminalManager(_cfg(), _log_silent())
        assert a.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=60.0, connect_now=False,
        ) is True
        assert b.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=60.0, connect_now=False,
        ) is False
        assert a._event_thread is b._event_thread, "both instances share one worker"
        loops = [t for t in threading.enumerate()
                 if t.name == "MT5TradeEventLoop" and t.is_alive()]
        assert len(loops) == 1, f"expected exactly one worker thread; got {len(loops)}"

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_dead_worker_restarts_deterministically(self, monkeypatch):
        from core import mt5_terminal_manager as mt

        orig = mt.MT5TerminalManager._maybe_poll_event

        def _boom(self, *a, **kw):
            raise SystemExit(0)

        monkeypatch.setattr(mt.MT5TerminalManager, "_maybe_poll_event", _boom)
        mgr = MT5TerminalManager(_cfg(), _log_silent())
        assert mgr.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=60.0, connect_now=False,
        ) is True
        deadline = time.monotonic() + 5
        while (mgr._event_thread is not None and mgr._event_thread.is_alive()
               and time.monotonic() < deadline):
            time.sleep(0.01)
        # Restore the healthy method BEFORE restarting the worker.
        monkeypatch.setattr(mt.MT5TerminalManager, "_maybe_poll_event", orig)
        assert mgr._event_thread is None or not mgr._event_thread.is_alive(), \
            "worker should be dead after the crash"
        started = mgr.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=60.0, connect_now=False,
        )
        assert started is True, "dead worker must be restarted by the next call"
        assert mgr._event_thread is not None and mgr._event_thread.is_alive()

    def test_shutdown_terminates_worker(self):
        mgr = MT5TerminalManager(_cfg(), _log_silent())
        assert mgr.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=60.0, connect_now=False,
        ) is True
        assert mgr.stop_event_loop(timeout=2.0) is True
        assert mgr._event_thread is None
        assert mgr.feed_status()["worker_alive"] is False
        loops = [t for t in threading.enumerate()
                 if t.name == "MT5TradeEventLoop" and t.is_alive()]
        assert loops == []


# ---------------------------------------------------------------------------
# 2. Candle refresh — the worker's update path reaches the state writer
# ---------------------------------------------------------------------------


class TestCandleRefresh:
    def test_worker_refresh_path_writes_latest_candles(self):
        """The worker invokes the refresh function on its candle cadence, and
        that path is what writes state/latest_candles.json + feed health."""
        payload = {
            "timestamp": utc_now_iso(),
            "source": "mt5",
            "symbols": {
                "XAUUSDm": {
                    "M5": [{"open": 1, "high": 2, "low": 1, "close": 1.5,
                            "time": _now_iso(10), "volume": 10}],
                    "M1": [{"open": 1, "high": 2, "low": 1, "close": 1.5,
                            "time": _now_iso(30), "volume": 5}],
                },
            },
        }

        def refresh_fn() -> dict:
            write_json_state("latest_candles.json", payload)
            return payload

        mgr = MT5TerminalManager(_cfg(), _log_silent())
        mgr.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=0.05,
            connect_now=False, candle_refresh_fn=refresh_fn,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                feed = mgr.feed_status()
                if feed.get("last_refresh_success_at") and feed.get("last_market_timestamp"):
                    break
                time.sleep(0.02)
            feed = mgr.feed_status()
            assert feed.get("last_refresh_success_at"), feed
            assert feed["refresh_failures_consecutive"] == 0
            assert feed["last_market_timestamp"] == payload["symbols"]["XAUUSDm"]["M5"][0]["time"]
            # The refresh actually reached the state writer.
            assert read_json_state("latest_candles.json") == payload
        finally:
            mgr.stop_event_loop(timeout=2.0)

    def test_worker_without_refresh_fn_records_no_phantom_failures(self):
        """A bare start_event_loop() caller (no candle_refresh_fn) must NOT
        pollute feed health with refresh failures it never made."""
        mgr = MT5TerminalManager(_cfg(), _log_silent())
        mgr.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=0.05, connect_now=False,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if (mgr.last_event is not None
                        and mgr.last_event.event_type == "candle_update"):
                    break
                time.sleep(0.02)
            assert mgr.last_event is not None
            assert mgr.last_event.meta.get("refresh") == "noop"
            feed = mgr.feed_status()
            assert feed.get("last_refresh_attempt_at") is None
            assert feed.get("last_refresh_error") is None
            assert feed.get("refresh_failures_consecutive", 0) == 0
        finally:
            mgr.stop_event_loop(timeout=2.0)

    def test_refresh_without_market_timestamp_is_flagged(self):
        """A 'successful' pull that recovers no bar timestamp must be visible
        as not-fresh at the feed layer (fail-closed observability)."""
        def refresh_fn() -> dict:
            return {"symbols": {"XAUUSDm": {
                "M1": [{"open": 1, "high": 2, "low": 1, "close": 1.5}],
            }}}

        mgr = MT5TerminalManager(_cfg(), _log_silent())
        mgr.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=0.05,
            connect_now=False, candle_refresh_fn=refresh_fn,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                feed = mgr.feed_status()
                if feed.get("last_refresh_error"):
                    break
                time.sleep(0.02)
            feed = mgr.feed_status()
            assert "no market timestamp" in (feed.get("last_refresh_error") or "")
            assert feed["refresh_failures_consecutive"] >= 1
            assert feed.get("last_market_timestamp") is None
        finally:
            mgr.stop_event_loop(timeout=2.0)

    def test_refresh_failure_tracks_feed_health(self):
        def refresh_fn() -> dict:
            raise RuntimeError("MT5 disconnected")

        mgr = MT5TerminalManager(_cfg(), _log_silent())
        mgr.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=0.05,
            connect_now=False, candle_refresh_fn=refresh_fn,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                feed = mgr.feed_status()
                if feed.get("last_refresh_error"):
                    break
                time.sleep(0.02)
            feed = mgr.feed_status()
            assert feed["refresh_failures_consecutive"] >= 1
            assert "MT5 disconnected" in (feed["last_refresh_error"] or "")
            assert feed.get("last_refresh_success_at") is None
            assert feed.get("last_market_timestamp") is None, \
                "a failed refresh must never fabricate a market timestamp"
        finally:
            mgr.stop_event_loop(timeout=2.0)


# ---------------------------------------------------------------------------
# 3. Freshness — market timestamp is the only source of truth
# ---------------------------------------------------------------------------


class TestMarketTimestampFreshness:
    def test_latest_market_timestamp_returns_newest_bar(self):
        payload = {
            "symbols": {
                "A": {"M5": [{"time": "2026-08-08T00:05:00+00:00"}]},
                "B": {"M1": [
                    {"time": "2026-08-08T00:09:00+00:00"},
                    {"time": "2026-08-08T00:10:00+00:00"},
                ]},
            },
        }
        assert latest_market_timestamp(payload) == "2026-08-08T00:10:00+00:00"
        assert latest_market_timestamp(None) is None
        assert latest_market_timestamp({"symbols": {}}) is None

    def test_market_timestamp_advances_when_new_data_supplied(self):
        MT5TerminalManager.record_candle_refresh_result(ok=True, market_ts=_now_iso(200))
        old = MT5TerminalManager.feed_status()["last_market_timestamp"]
        MT5TerminalManager.record_candle_refresh_result(ok=True, market_ts=_now_iso(5))
        new = MT5TerminalManager.feed_status()["last_market_timestamp"]
        assert new is not None and new > old
        persisted = read_json_state("market_data_feed.json")
        assert persisted["last_market_timestamp"] == new

    def test_worker_running_does_not_fake_freshness(self):
        """A worker that keeps running while the market timestamp stays put
        must NOT report freshness — and the M1 engine gates the same bars to
        WAIT, exactly as the fail-closed contract requires."""
        stale_ts = _now_iso(5000)

        def refresh_fn() -> dict:
            return {"symbols": {"XAUUSDm": {
                "M1": [{"open": 1, "high": 2, "low": 1, "close": 1.5, "time": stale_ts}],
            }}}

        mgr = MT5TerminalManager(_cfg(), _log_silent())
        mgr.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=0.05,
            connect_now=False, candle_refresh_fn=refresh_fn,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                feed = mgr.feed_status()
                if feed.get("last_refresh_success_at"):
                    break
                time.sleep(0.02)
            feed = mgr.feed_status()
            assert feed["refresh_failures_consecutive"] == 0  # refresh "succeeded"
            assert feed["last_market_timestamp"] == stale_ts    # but the DATA is old
            assert feed["worker_alive"] is True

            from core.m1_structure_engine import M1StructureEngine
            eng = M1StructureEngine({"m1_structure": {"enabled": True}})
            bars = [{"open": 1, "high": 2, "low": 1, "close": 1.5, "time": stale_ts}]
            eng.update("FEEDSTALE", bars, 1.5, atr=1.0)
            dec = eng.get_decision("FEEDSTALE")
            assert dec["data_fresh"] is False
            assert dec["state"] == "WAIT"
        finally:
            mgr.stop_event_loop(timeout=2.0)


# ---------------------------------------------------------------------------
# 4. Single worker — no duplicate workers, no concurrent refresh writes
# ---------------------------------------------------------------------------


class TestSingleWorkerNoConcurrentWrites:
    def test_only_one_worker_no_overlapping_refresh(self):
        inflight = {"n": 0, "max": 0, "runs": 0}

        def refresh_fn() -> dict:
            inflight["n"] += 1
            inflight["max"] = max(inflight["max"], inflight["n"])
            inflight["runs"] += 1
            time.sleep(0.01)
            inflight["n"] -= 1
            return {"symbols": {}}

        a = MT5TerminalManager(_cfg(), _log_silent())
        b = MT5TerminalManager(_cfg(), _log_silent())
        assert a.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=0.03,
            connect_now=False, candle_refresh_fn=refresh_fn,
        ) is True
        assert b.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=0.03,
            connect_now=False, candle_refresh_fn=refresh_fn,
        ) is False
        try:
            time.sleep(0.4)
            loops = [t for t in threading.enumerate()
                     if t.name == "MT5TradeEventLoop" and t.is_alive()]
            assert len(loops) == 1, "exactly one worker must exist"
            assert inflight["runs"] >= 1, "worker should have refreshed"
            assert inflight["max"] == 1, \
                f"refreshes must never overlap; max concurrent = {inflight['max']}"
        finally:
            a.stop_event_loop(timeout=2.0)


# ---------------------------------------------------------------------------
# 5. data_loop integration (pandas-gated — runs in the production env)
# ---------------------------------------------------------------------------


class TestDataLoopIntegration:
    def test_run_never_spawns_duplicate_worker_or_refreshes_while_fresh(self, monkeypatch):
        """data_loop.run() must not spawn a second worker while the shared
        process-owned worker is alive, and must not manually refresh while the
        shared feed is fresh — it just returns True."""
        pytest.importorskip("pandas")
        from loops import data_loop as dl

        # A REAL shared worker is already running (spawned once, process-owned).
        # Without it, feed_status()['worker_alive'] is False and run() would
        # legitimately take the manual-refresh fallback instead.
        mgr = MT5TerminalManager(_cfg(), _log_silent())
        assert mgr.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=60.0, connect_now=False,
        ) is True

        # The worker refreshed successfully just now -> the feed is fresh.
        MT5TerminalManager.record_candle_refresh_result(
            ok=True, market_ts=_now_iso(10), attempted_at=utc_now_iso(),
        )

        refreshed = []
        monkeypatch.setattr(
            dl, "candle_refresh_now",
            lambda config=None, logger=None: refreshed.append(1) or {"ok": True},
        )
        monkeypatch.setattr(
            dl, "load_config",
            lambda: {"mt5": {"timeframes": {"entry": "M5", "bias": "M15"}},
                     "m1_structure": {"enabled": True}},
        )
        try:
            assert dl.run() is True
            assert dl.run() is True
            assert refreshed == [], \
                "run() must not manually refresh while the shared feed is fresh"
            loops = [t for t in threading.enumerate()
                     if t.name == "MT5TradeEventLoop" and t.is_alive()]
            assert len(loops) == 1, "run() must never spawn a duplicate worker"
        finally:
            mgr.stop_event_loop(timeout=2.0)

    def test_run_refreshes_once_when_feed_quiet(self, monkeypatch):
        """run() falls back to exactly ONE manual refresh when the shared
        feed's last success is older than the staleness threshold — and never
        spawns a second worker to do it."""
        pytest.importorskip("pandas")
        from loops import data_loop as dl

        mgr = MT5TerminalManager(_cfg(), _log_silent())
        assert mgr.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=60.0, connect_now=False,
        ) is True

        # The feed's last successful refresh happened >30s ago -> quiet.
        MT5TerminalManager.record_candle_refresh_result(
            ok=True, market_ts=_now_iso(200), attempted_at=_now_iso(200),
        )

        refreshed = []
        monkeypatch.setattr(
            dl, "candle_refresh_now",
            lambda config=None, logger=None: refreshed.append(1) or {"ok": True},
        )
        monkeypatch.setattr(
            dl, "load_config",
            lambda: {"mt5": {"timeframes": {"entry": "M5", "bias": "M15"}},
                     "m1_structure": {"enabled": True}},
        )
        try:
            assert dl.run()  # dict result from the one manual refresh
            assert len(refreshed) == 1, \
                "exactly one manual refresh when the feed is quiet"
            loops = [t for t in threading.enumerate()
                     if t.name == "MT5TradeEventLoop" and t.is_alive()]
            assert len(loops) == 1, \
                "still exactly one worker — the fallback must not spawn another"
        finally:
            mgr.stop_event_loop(timeout=2.0)

    def test_candle_refresh_interval_config(self):
        pytest.importorskip("pandas")
        from loops.data_loop import _candle_refresh_interval

        assert _candle_refresh_interval({"m1_structure": {}}) == 10.0
        assert _candle_refresh_interval(
            {"m1_structure": {"candle_refresh_interval_seconds": 7}}) == 7.0

    def test_candle_refresh_now_writes_state_and_records_health(self, monkeypatch):
        pytest.importorskip("pandas")
        from loops import data_loop as dl

        class _Conn:
            def __init__(self, *a, **kw):
                self.connected = True

            def connect(self):
                return True

            def disconnect(self):
                return None

            def account_snapshot(self):
                return {"login": 1, "server": "demo", "account_mode": "demo"}

        class _Symbols:
            def __init__(self, *a, **kw):
                pass

            def discover(self):
                return {"resolved": {"XAUUSDm": "XAUUSDm"}}

        class _Collector:
            def __init__(self, *a, **kw):
                pass

            def pull_latest(self):
                return {
                    "timestamp": "t", "source": "mt5",
                    "symbol_map": {"XAUUSDm": "XAUUSDm"}, "account": {},
                    "symbols": {"XAUUSDm": {
                        "broker_symbol": "XAUUSDm",
                        "M5": [{"open": 1, "high": 2, "low": 1, "close": 1.5,
                                "time": _now_iso(10)}],
                        "M15": [],
                    }},
                }

        monkeypatch.setattr(dl, "MT5ConnectionManager", _Conn)
        monkeypatch.setattr(dl, "SymbolManager", _Symbols)
        monkeypatch.setattr(dl, "DataCollector", _Collector)
        written = {}
        monkeypatch.setattr(
            dl, "write_json_state", lambda name, data: written.__setitem__(name, data))
        monkeypatch.setattr(dl, "_validate_payload", lambda *a, **kw: None)

        out = dl.candle_refresh_now(
            config={"mt5": {"timeframes": {"entry": "M5", "bias": "M15"}},
                    "m1_structure": {"enabled": False}},
            logger=_log_silent(),
        )
        assert out is not False
        assert "latest_candles.json" in written
        feed = MT5TerminalManager.feed_status()
        assert feed.get("last_refresh_success_at")
        assert feed["refresh_failures_consecutive"] == 0

    def test_candle_refresh_now_failure_records_health(self, monkeypatch):
        pytest.importorskip("pandas")
        from loops import data_loop as dl

        class _Conn:
            def __init__(self, *a, **kw):
                self.connected = True

            def connect(self):
                return True

            def disconnect(self):
                return None

        class _Symbols:
            def __init__(self, *a, **kw):
                pass

            def discover(self):
                return {"resolved": {"XAUUSDm": "XAUUSDm"}}

        class _FailingCollector:
            def __init__(self, *a, **kw):
                pass

            def pull_latest(self):
                raise RuntimeError("Not logged in to MT5: (-10004, 'No IPC connection')")

        monkeypatch.setattr(dl, "MT5ConnectionManager", _Conn)
        monkeypatch.setattr(dl, "SymbolManager", _Symbols)
        monkeypatch.setattr(dl, "DataCollector", _FailingCollector)

        out = dl.candle_refresh_now(
            config={"mt5": {"timeframes": {"entry": "M5", "bias": "M15"}},
                    "m1_structure": {"enabled": False}},
            logger=_log_silent(),
        )
        assert out is False
        feed = MT5TerminalManager.feed_status()
        assert feed["refresh_failures_consecutive"] >= 1
        assert "No IPC connection" in (feed["last_refresh_error"] or "")

    def test_candle_refresh_now_record_health_false_defers_recording(self, monkeypatch):
        """Bug 2 (single-owner health): a refresh fn run with
        record_health=False must NOT touch feed health — the worker's
        _candle_tick is the sole recorder on that path."""
        pytest.importorskip("pandas")
        from loops import data_loop as dl

        class _Conn:
            def __init__(self, *a, **kw):
                self.connected = True

            def connect(self):
                return True

            def disconnect(self):
                return None

        class _Symbols:
            def __init__(self, *a, **kw):
                pass

            def discover(self):
                return {"resolved": {"XAUUSDm": "XAUUSDm"}}

        class _Collector:
            def __init__(self, *a, **kw):
                pass

            def pull_latest(self):
                return {
                    "timestamp": "t", "source": "mt5",
                    "symbol_map": {"XAUUSDm": "XAUUSDm"}, "account": {},
                    "symbols": {"XAUUSDm": {
                        "broker_symbol": "XAUUSDm",
                        "M5": [{"open": 1, "high": 2, "low": 1, "close": 1.5,
                                "time": _now_iso(10)}],
                        "M15": [],
                    }},
                }

        monkeypatch.setattr(dl, "MT5ConnectionManager", _Conn)
        monkeypatch.setattr(dl, "SymbolManager", _Symbols)
        monkeypatch.setattr(dl, "DataCollector", _Collector)
        monkeypatch.setattr(dl, "write_json_state", lambda *a, **k: None)
        monkeypatch.setattr(dl, "_validate_payload", lambda *a, **k: None)

        before = MT5TerminalManager.feed_status().get("last_refresh_attempt_at")
        out = dl.candle_refresh_now(
            config=_cfg(), logger=_log_silent(), record_health=False,
        )
        assert out is not False
        after = MT5TerminalManager.feed_status().get("last_refresh_attempt_at")
        assert after == before, \
            "record_health=False must NOT record feed health"

    def test_candle_refresh_now_record_health_false_reraises_real_error(self, monkeypatch):
        """Bug 2: on failure the record_health=False path re-raises the REAL
        error (instead of returning a generic False) so the worker records the
        actual cause — and it records nothing itself."""
        pytest.importorskip("pandas")
        from loops import data_loop as dl

        class _Conn:
            def __init__(self, *a, **kw):
                self.connected = True

            def connect(self):
                return True

            def disconnect(self):
                return None

        class _Symbols:
            def __init__(self, *a, **kw):
                pass

            def discover(self):
                return {"resolved": {"XAUUSDm": "XAUUSDm"}}

        class _FailingCollector:
            def __init__(self, *a, **kw):
                pass

            def pull_latest(self):
                raise RuntimeError("Not logged in to MT5: (-10004, 'No IPC connection')")

        monkeypatch.setattr(dl, "MT5ConnectionManager", _Conn)
        monkeypatch.setattr(dl, "SymbolManager", _Symbols)
        monkeypatch.setattr(dl, "DataCollector", _FailingCollector)

        with pytest.raises(RuntimeError, match="No IPC connection"):
            dl.candle_refresh_now(
                config=_cfg(), logger=_log_silent(), record_health=False,
            )
        feed = MT5TerminalManager.feed_status()
        assert feed.get("refresh_failures_consecutive", 0) == 0, \
            "record_health=False must not record — the worker records instead"

    def test_worker_success_records_health_exactly_once_per_tick(self, monkeypatch):
        """Bug 2 regression (success side): a SUCCESSFUL worker tick must also
        record feed health exactly once. Before the fix both
        candle_refresh_now() and _candle_tick() recorded, so one successful
        refresh caused duplicate health writes."""
        pytest.importorskip("pandas")
        from loops import data_loop as dl
        from core import mt5_terminal_manager as mt

        class _Conn:
            def __init__(self, *a, **kw):
                self.connected = True

            def connect(self):
                return True

            def disconnect(self):
                return None

        class _Symbols:
            def __init__(self, *a, **kw):
                pass

            def discover(self):
                return {"resolved": {"XAUUSDm": "XAUUSDm"}}

        class _Collector:
            calls = 0

            def __init__(self, *a, **kw):
                pass

            def pull_latest(self):
                type(self).calls += 1
                return {
                    "timestamp": "t", "source": "mt5",
                    "symbol_map": {"XAUUSDm": "XAUUSDm"}, "account": {},
                    "symbols": {"XAUUSDm": {
                        "broker_symbol": "XAUUSDm",
                        "M5": [{"open": 1, "high": 2, "low": 1, "close": 1.5,
                                "time": _now_iso(5)}],
                        "M1": [{"open": 1, "high": 2, "low": 1, "close": 1.5,
                                "time": _now_iso(2)}],
                        "M15": [],
                    }},
                }

        monkeypatch.setattr(dl, "MT5ConnectionManager", _Conn)
        monkeypatch.setattr(dl, "SymbolManager", _Symbols)
        monkeypatch.setattr(dl, "DataCollector", _Collector)
        monkeypatch.setattr(dl, "write_json_state", lambda *a, **k: None)
        monkeypatch.setattr(dl, "_validate_payload", lambda *a, **k: None)

        records = []
        orig = mt.MT5TerminalManager.record_candle_refresh_result

        def _counting(cls, **kw):
            result = orig(**kw)
            records.append(kw)
            return result

        monkeypatch.setattr(
            mt.MT5TerminalManager, "record_candle_refresh_result",
            classmethod(_counting),
        )

        mgr = MT5TerminalManager(_cfg(), _log_silent())
        mgr.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=0.05, connect_now=False,
            candle_refresh_fn=dl._make_candle_refresh_fn(_cfg(), _log_silent()),
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if len(records) == _Collector.calls and _Collector.calls >= 2:
                    break
                time.sleep(0.02)
            feed = mgr.feed_status()
            assert _Collector.calls >= 2, f"expected >=2 ticks; got {_Collector.calls}"
            assert len(records) == _Collector.calls, (
                "each SUCCESSFUL tick must record exactly once — duplicate "
                f"success writes would give records={len(records)} ticks={_Collector.calls}"
            )
            assert all(r.get("ok") for r in records), records
            assert feed.get("refresh_failures_consecutive", 0) == 0
            assert feed["last_refresh_success_at"]
        finally:
            mgr.stop_event_loop(timeout=2.0)

    def test_worker_path_records_health_exactly_once_per_tick(self, monkeypatch):
        """Bug 2 regression: the REAL worker refresh path (candle_refresh_now
        via _make_candle_refresh_fn) must record feed health exactly once per
        tick. Before the fix, candle_refresh_now() recorded internally AND
        _candle_tick() recorded again — a failed tick counted TWICE."""
        pytest.importorskip("pandas")
        from loops import data_loop as dl

        class _Conn:
            def __init__(self, *a, **kw):
                self.connected = True

            def connect(self):
                return True

            def disconnect(self):
                return None

        class _Symbols:
            def __init__(self, *a, **kw):
                pass

            def discover(self):
                return {"resolved": {"XAUUSDm": "XAUUSDm"}}

        class _FailingCollector:
            calls = 0

            def __init__(self, *a, **kw):
                pass

            def pull_latest(self):
                type(self).calls += 1
                raise RuntimeError("Not logged in to MT5: (-10004, 'No IPC connection')")

        monkeypatch.setattr(dl, "MT5ConnectionManager", _Conn)
        monkeypatch.setattr(dl, "SymbolManager", _Symbols)
        monkeypatch.setattr(dl, "DataCollector", _FailingCollector)

        mgr = MT5TerminalManager(_cfg(), _log_silent())
        mgr.start_event_loop(
            poll_interval_sec=0.02, candle_interval_sec=0.05, connect_now=False,
            candle_refresh_fn=dl._make_candle_refresh_fn(_cfg(), _log_silent()),
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                feed = mgr.feed_status()
                if (feed.get("refresh_failures_consecutive", 0) == _FailingCollector.calls
                        and _FailingCollector.calls >= 2):
                    break
                time.sleep(0.02)
            feed = mgr.feed_status()
            assert _FailingCollector.calls >= 2, \
                f"expected >=2 failed ticks; got {_FailingCollector.calls}"
            assert feed["refresh_failures_consecutive"] == _FailingCollector.calls, (
                "each failed tick must count EXACTLY once — double recording "
                f"would make failures={feed['refresh_failures_consecutive']} "
                f"outrun ticks={_FailingCollector.calls}"
            )
            assert "No IPC connection" in (feed["last_refresh_error"] or "")
        finally:
            mgr.stop_event_loop(timeout=2.0)
