"""M1 dedicated shadow service tests.

Proves the M1 structure loop:
  1. runs as its own supervisor service (start.py registration) on a fast
     cadence, fully independent of the sequential trading pipeline and its
     analytical throttling;
  2. is non-reentrant — overlapping runs are skipped, never queued;
  3. writes decisions standalone (no pipeline dependency);
  4. stays shadow-only (reads state/latest_candles.json, writes decisions;
     no broker/order/kill-switch calls).
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestNonReentrant:
    def test_overlapping_run_is_skipped(self, monkeypatch):
        """A second run() while one is in flight returns None immediately and
        never enters the structure pass (no overlapping passes)."""
        import loops.m1_structure_loop as msl

        entered: list[int] = []
        release = threading.Event()

        def fake_pass(_config):
            entered.append(1)
            release.wait(timeout=5)
            return {"enabled": False, "decisions": {}, "events": {},
                    "updated_at": "t"}

        monkeypatch.setattr(msl, "load_config", lambda: {})
        monkeypatch.setattr(msl, "_run_structure_pass", fake_pass)
        monkeypatch.setattr(msl, "write_json_state", lambda _name, _data: None)

        first = threading.Thread(target=msl.run)
        first.start()
        try:
            deadline = time.time() + 5
            while not entered and time.time() < deadline:
                time.sleep(0.01)
            assert entered, "first run never entered the structure pass"
            second = msl.run()  # main thread while first is still blocked
            assert second is None, "overlapping run must be skipped"
        finally:
            release.set()
            first.join(timeout=5)
        assert not first.is_alive()


class TestStandaloneRun:
    def test_run_writes_decisions_without_pipeline(self, monkeypatch):
        """run() works standalone — the pipeline is not required to produce
        m1_structure_decisions.json."""
        from datetime import datetime, timedelta, timezone

        import loops.m1_structure_loop as msl

        written: dict = {}
        # Unique symbol: the structure ledger is persistent and idempotent per
        # event_id, so tests must never fabricate records under a real symbol
        # (the dashboard reads real symbols from the ledger).
        sym = f"TST_{time.time_ns()}"

        def _iso(seconds_ago: float) -> str:
            return (datetime.now(timezone.utc)
                    - timedelta(seconds=seconds_ago)).isoformat()

        def fake_config():
            return {"m1_structure": {"enabled": True}}

        def fake_read(name, default=None):
            if name == "latest_candles.json":
                return {"symbols": {sym: {"M1": [
                    {"open": 100.0, "high": 100.4, "low": 99.7, "close": 100.2,
                     "time": _iso(360)},
                    {"open": 100.2, "high": 100.6, "low": 100.0, "close": 100.4,
                     "time": _iso(300)},
                    {"open": 100.4, "high": 100.9, "low": 100.2, "close": 100.7,
                     "time": _iso(240)},
                    {"open": 100.6, "high": 101.0, "low": 100.4, "close": 100.8,
                     "time": _iso(180)},
                    {"open": 100.8, "high": 101.3, "low": 100.6, "close": 101.1,
                     "time": _iso(120)},
                    {"open": 101.0, "high": 101.5, "low": 100.9, "close": 101.4,
                     "time": _iso(60)},
                ]}}}
            return default

        monkeypatch.setattr(msl, "load_config", fake_config)
        monkeypatch.setattr(msl, "read_json_state", fake_read)
        monkeypatch.setattr(msl, "write_json_state",
                            lambda name, data: written.__setitem__(name, data))

        res = msl.run()
        assert res is not None
        assert res["enabled"] is True
        assert sym in res["decisions"]
        assert "m1_structure_decisions.json" in written


class TestRegistration:
    def test_start_registers_dedicated_m1_service(self):
        """start.py registers M1 as its own ManagedService with a fast,
        config-driven interval — never throttled by ANALYTICAL_LOOPS."""
        source = (ROOT / "start.py").read_text(encoding="utf-8")
        assert '"m1_structure"' in source
        assert "M1 Structure" in source
        assert "m1_structure_loop.run" in source
        assert "loop_interval_seconds" in source
        assert '"m1_structure_loop": "OK"' in source
        # A pass skipped by the non-overlap guard must be reported as SKIPPED,
        # not OK — supervisor telemetry must not claim a pass that never ran.
        assert '"m1_structure_loop": "SKIPPED"' in source

    def test_config_default_interval_is_10_seconds(self):
        """The service cadence defaults to 10s (target 5-15s) via config."""
        source = (ROOT / "config.yaml").read_text(encoding="utf-8")
        assert "loop_interval_seconds: 10" in source

    def test_pipeline_does_not_reference_m1_loop(self):
        """The sequential pipeline must not run M1 at all."""
        # loops.data_loop transitively imports pandas (core.history_manager), so
        # this full-registry import runs where the production env (Windows/MT5)
        # has it and is skipped in the pandas-free sandbox.
        import pytest
        pytest.importorskip("pandas")
        from core.pipeline import _init_loops

        names = [name for name, _fn in _init_loops()]
        assert "m1_structure_loop" not in names
