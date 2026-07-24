"""Tests for /api/profit_quality/stream SSE handler + _ProfitQualityBroadcaster.

The broadcaster polls _build_profit_quality() every TICK_S=5s and pushes a
snapshot to each subscriber queue when the data fingerprint changes. Idle
ticks push a heartbeat event. The SSE handler subscribes on connect, fans
out events as `event: snapshot\\ndata: {...}\\n\\n` or `: heartbeat\\n\\n`
chunks, and unsubscribes on disconnect (BrokenPipeError / OSError).

What's covered:
  - Cold subscribe returns an empty queue (no synthetic first-paint).
  - After at least one tick, subscribe() pushes the cached snapshot
    immediately so the SPA never waits 5s for a 0-latency connect.
  - Idle ticks fire ('heartbeat', {ts,...}), not ('snapshot', snap).
  - Fingerprint-change ticks fire ('snapshot', NEW snap) and bump cache.
  - Unsubscribe removes the queue from _SUBSCRIBERS (no per-tick writes).
  - End-to-end: raw socket GET /api/profit_quality/stream returns 200 OK,
    Content-Type: text/event-stream, X-Accel-Buffering: no, and at least
    one `event: snapshot` chunk carrying JSON with the seeded n_total.
"""

import json
import os
import queue
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dashboard.server import (  # noqa: E402  (after sys.path tweak)
    DashboardHandler,
    ThreadingDashboardServer,
    _ProfitQualityBroadcaster,
)


_SNAP_BASE: dict = {
    "n_total": 0,
    "data_quality": {"pct_complete_be_and_r": 0.0},
    "r_buckets": [],
    "payoff_buckets": [],
    "bleed_top": [],
    "be": [],
    "partial_tp": [],
    "stale": [],
}


def _make_snap(n_total: int = 5, axis_bleed_top=None, **kw) -> dict:
    snap = json.loads(json.dumps(_SNAP_BASE))  # deep copy via JSON round-trip
    snap["n_total"] = n_total
    snap["data_quality"]["pct_complete_be_and_r"] = min(round(n_total * 2.5, 1), 100.0)
    if axis_bleed_top is not None:
        snap["bleed_top"] = axis_bleed_top
    snap.update(kw)
    return snap


@pytest.fixture(autouse=True)
def _reset_broadcaster():
    """Wipe singleton state between tests so subscriber lists, fingerprint,
    cached snapshot, and the daemon thread don't leak across cases."""
    _ProfitQualityBroadcaster.reset_for_tests()
    yield
    _ProfitQualityBroadcaster.reset_for_tests()


def test_subscribe_on_cold_broadcaster_returns_empty_queue(monkeypatch):
    """No tick has run → _LAST_SNAPSHOT is None → subscribe() must NOT push
    a synthetic empty snapshot to the subscriber. The SSE handler will
    instead write `: initializing\\n\\n` and let the next tick fill it in.
    The handler doing its own first-paint avoids blocking subscribe()."""
    monkeypatch.setattr(
        _ProfitQualityBroadcaster, "_default_build",
        classmethod(lambda cls: _make_snap(n_total=5)),
    )
    q = _ProfitQualityBroadcaster.subscribe()
    try:
        assert q.empty(), (
            "cold broadcaster must not synthesize an initial snapshot. "
            "Otherwise the SPA renders an empty tile from a stub snapshot "
            "before the first real _build_profit_quality() runs."
        )
    finally:
        _ProfitQualityBroadcaster.unsubscribe(q)


def test_subscribe_gets_cached_snapshot_after_first_tick(monkeypatch):
    """First _tick_once() runs _build_profit_quality and stores _LAST_SNAPSHOT.
    Subsequent subscribe() must push that cached snapshot immediately so a
    reconnecting browser renders the current state without waiting 5s."""
    state = {"snap": _make_snap(n_total=7)}
    monkeypatch.setattr(
        _ProfitQualityBroadcaster, "_default_build",
        classmethod(lambda cls: dict(state["snap"])),
    )
    _ProfitQualityBroadcaster._tick_once()  # runs _build_profit_quality, caches
    q = _ProfitQualityBroadcaster.subscribe()
    try:
        assert not q.empty(), "expected cached snapshot on subscribe after first tick"
        ev_type, payload = q.get_nowait()
        assert ev_type == "snapshot"
        assert payload["n_total"] == 7
    finally:
        _ProfitQualityBroadcaster.unsubscribe(q)


def test_heartbeat_on_idle(monkeypatch):
    """Fingerprint unchanged between two _tick_once() calls produces a
    heartbeat event — the broadcaster's 'still here' signal that does not
    force a full payload re-render on the SPA. Differs from a snapshot only
    in the leading byte of the SSE frame.

    Pre-warms with one _tick_once BEFORE subscribe() so subscribe() actually
    delivers a cached snapshot (otherwise subscribe() returns an empty queue
    by design — see test_subscribe_on_cold_broadcaster_returns_empty_queue)."""
    state = {"snap": _make_snap(n_total=5)}
    monkeypatch.setattr(
        _ProfitQualityBroadcaster, "_default_build",
        classmethod(lambda cls: dict(state["snap"])),
    )
    _ProfitQualityBroadcaster._tick_once()  # seed _LAST_FINGERPRINT + _LAST_SNAPSHOT
    q = _ProfitQualityBroadcaster.subscribe()
    try:
        # Drain the cached snapshot subscribe() pushed.
        ev_type, _ = q.get_nowait()
        assert ev_type == "snapshot"
        # Now a tick on a UNCHANGED snapshot must produce heartbeat.
        ev_type, payload = _ProfitQualityBroadcaster._tick_once()
        assert ev_type == "heartbeat", f"expected heartbeat on idle tick, got {ev_type!r}"
        assert isinstance(payload, dict)
        assert "ts" in payload
    finally:
        _ProfitQualityBroadcaster.unsubscribe(q)


def test_fingerprint_change_fans_out_snapshot_real_fanout(monkeypatch):
    """Drives the _fanout code path that _loop actually calls in production.
    Verifies a fingerprint-change event reaches every subscribed queue — not
    just the caller of _tick_once (which only computes/delivers, doesn't
    fanout). Mutates n_total to force a fingerprint delta and asserts the
    delivery via the same _fanout method _loop invokes."""
    state = {"snap": _make_snap(n_total=5)}
    monkeypatch.setattr(
        _ProfitQualityBroadcaster, "_default_build",
        classmethod(lambda cls: dict(state["snap"])),
    )
    # Pre-warm — first tick seeds _LAST_FINGERPRINT AND _LAST_SNAPSHOT.
    _ProfitQualityBroadcaster._tick_once()
    q = _ProfitQualityBroadcaster.subscribe()
    try:
        # Drain the cached snapshot subscribe() pushed.
        ev_type, _ = q.get_nowait()
        assert ev_type == "snapshot"
        # Unchanged tick → heartbeat (different event type, same None-net change).
        ev_type2, _ = _ProfitQualityBroadcaster._tick_once()
        assert ev_type2 == "heartbeat"
        # Move n_total — this changes every fingerprint component the
        # broadcaster watches (n_total, data_quality pct, all bucket sums),
        # so _tick_once returns a snapshot event.
        state["snap"] = _make_snap(n_total=6)
        event3 = _ProfitQualityBroadcaster._tick_once()
        assert event3[0] == "snapshot"
        assert event3[1]["n_total"] == 6
        # Drive _fanout (the SAME code _loop calls) — q gets the event.
        _ProfitQualityBroadcaster._fanout(event3)
        assert not q.empty()
        ev_type3, payload3 = q.get_nowait()
        assert ev_type3 == "snapshot"
        assert payload3["n_total"] == 6
    finally:
        _ProfitQualityBroadcaster.unsubscribe(q)


def test_cross_table_delta_fires_snapshot(monkeypatch):
    """The cross-tables (BE×Partial, Partial×Stale, BE×Stale) are the user's
    primary bleed-diagnostic. REGRESSION GUARD: a single-cell change in any
    cross-table MUST trigger a fingerprint delta (so SSE pushes), even if
    every other bucket + n_total stays the same. The previous fingerprint
    omitted the cross-tables — a cell flip would not surface for up to
    TICK_S=5s, defeating the user's 'real-time feel' headline ask."""
    cross_initial = {
        "axis_a_name": "BE", "axis_b_name": "Partial",
        "axis_a": ["not_triggered"], "axis_b": ["no_partial"],
        "rows": [{"axis_a": "not_triggered", "cells": [
            {"axis_b": "no_partial", "n": 5, "net_pnl": -7.5, "avg_r": -0.5}
        ]}],
    }
    snap_v1 = _make_snap(n_total=10)
    snap_v1["cross_be_partial"] = cross_initial
    monkeypatch.setattr(
        _ProfitQualityBroadcaster, "_default_build",
        classmethod(lambda cls: dict(_state["snap"])),
    )
    _state = {"snap": snap_v1}
    # Seed fingerprint.
    ev_type, _ = _ProfitQualityBroadcaster._tick_once()
    assert ev_type == "snapshot"
    # Same n_total + same bucket sums but a cross-cell flip.
    mutated = _make_snap(n_total=10)
    mutated["cross_be_partial"] = {
        **cross_initial,
        "rows": [{"axis_a": "not_triggered", "cells": [
            {"axis_b": "no_partial", "n": 6, "net_pnl": -9.25, "avg_r": -0.5}
        ]}],
    }
    _state["snap"] = mutated
    event = _ProfitQualityBroadcaster._tick_once()
    assert event[0] == "snapshot", (
        "cross-table cell flip must produce a snapshot event — "
        "this is the BLEED DIAGNOSTIC the user built these tiles to surface. "
        "If fingerprint ignores cross-tables, the SSE stream is silently "
        "broken for the user's primary use case."
    )


def test_default_build_passes_three_subsystems_positionally(monkeypatch):
    """REGRESSION GUARD (2026-07-20): the broadcaster's _default_build must
    invoke _build_profit_quality with the 3 subsystems (trade_log,
    position_management, trade_manager) in the correct order so the
    positional (trade_log, pos_mgmt, trade_mgr) signature is satisfied.

    The previous wiring called
    `_build_profit_quality(**{"trade_log": ..., "position_management": ...,
    "trade_manager": ...})` which raised TypeError on every live tick
    because _build_profit_quality's parameters are `pos_mgmt` and
    `trade_mgr`, not `position_management` and `trade_manager`. The error
    was caught by _tick_once's exception list and translated to a
    heartbeat-with-error frame, so the broadcaster silently never produced
    a real snapshot on the dashboard's first day.

    This test patches `dashboard.server.read_json_state` (the local
    binding dashboard.server actually uses, NOT core.utils.read_json_state)
    to return stub values, then calls the real _default_build. Passing this
    test is the contract that the wiring is intact."""
    monkeypatch.setattr(
        "dashboard.server.read_json_state",
        lambda name, default=None: (
            {"trades": [{"pnl": 0.5, "r_multiple": 0.3}]} if name == "trade_log.json"
            else {"positions": {}} if name == "position_management.json"
            else {"stale_candidates": []} if name == "trade_manager.json"
            else (default if default is not None else {})
        ),
    )
    snap = _ProfitQualityBroadcaster._default_build()
    assert isinstance(snap, dict)
    assert snap["n_total"] == 1
    assert snap["data_quality"]["trades_with_r_multiple"] == 1
    # Every panel the operator looks at must be present in the snapshot.
    for panel in ("r_buckets", "payoff_buckets", "bleed_top", "be",
                  "partial_tp", "cross_be_partial", "cross_partial_stale",
                  "cross_be_stale"):
        assert panel in snap, f"missing {panel!r} from default_build output"


def test_full_queue_unsubscribes_dead_subscriber(monkeypatch):
    """A subscriber whose queue never drains signals a broken TCP sink.
    The fix: drop it from _SUBSCRIBERS via _fanout so the SSE handler's
    BrokenPipeError catch cleans up on next attempt instead of holding a
    reference to a non-draining consumer forever."""
    monkeypatch.setattr(
        _ProfitQualityBroadcaster, "_default_build",
        classmethod(lambda cls: _make_snap(n_total=5)),
    )
    # Bypass subscribe() (which calls q.put_nowait under _LOCK and might
    # deadline) — manually attach a deliberately undersized queue.
    tiny_q: queue.Queue = queue.Queue(maxsize=1)
    pre_count = len(_ProfitQualityBroadcaster._SUBSCRIBERS)
    with _ProfitQualityBroadcaster._LOCK:
        _ProfitQualityBroadcaster._SUBSCRIBERS.append(tiny_q)
    try:
        # First fanout fills tiny_q (maxsize=1). Subscriber still listed.
        _ProfitQualityBroadcaster._fanout(("snapshot", _make_snap(n_total=6)))
        assert tiny_q.full()
        assert tiny_q in _ProfitQualityBroadcaster._SUBSCRIBERS, (
            "first fanout fills but does not drop the (still alive) subscriber"
        )
        # Second fanout triggers queue.Full → subscriber is removed.
        _ProfitQualityBroadcaster._fanout(("snapshot", _make_snap(n_total=7)))
        assert tiny_q not in _ProfitQualityBroadcaster._SUBSCRIBERS, (
            "second fanout on a still-full queue should drop the dead subscriber"
        )
    finally:
        with _ProfitQualityBroadcaster._LOCK:
            try:
                _ProfitQualityBroadcaster._SUBSCRIBERS.remove(tiny_q)
            except ValueError:
                pass
        # Sanity: subscriber count returned to the pre-test baseline.
        assert len(_ProfitQualityBroadcaster._SUBSCRIBERS) == pre_count


def test_unsubscribe_removes_queue_from_subscribers(monkeypatch):
    """After unsubscribe, the queue is gone from _SUBSCRIBERS and a subsequent
    _loop tick won't write to it. This guards against the leak where a
    disconnected SSE handler's queue lingers in the broadcaster forever."""
    monkeypatch.setattr(
        _ProfitQualityBroadcaster, "_default_build",
        classmethod(lambda cls: _make_snap(n_total=5)),
    )
    q = _ProfitQualityBroadcaster.subscribe()
    pre_count = len(_ProfitQualityBroadcaster._SUBSCRIBERS)
    assert pre_count >= 1
    _ProfitQualityBroadcaster.unsubscribe(q)
    assert q not in _ProfitQualityBroadcaster._SUBSCRIBERS, (
        "unsubscribe failed to remove q from broadcaster's _SUBSCRIBERS"
    )
    assert len(_ProfitQualityBroadcaster._SUBSCRIBERS) == pre_count - 1


def _free_port() -> int:
    """Bind to port 0 (kernel-assigned) then release. Small race exists but is
    fine for a test."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_sse_endpoint_via_raw_socket(monkeypatch):
    """End-to-end: real HTTP request to /api/profit_quality/stream, parsed
    by hand. This catches:
      - Status line must be '200 OK' (not 404 from prefix-vs-exact pitfalls).
      - Content-Type must be 'text/event-stream'.
      - X-Accel-Buffering must be 'no' (defends against reverse-proxy buffering).
      - At least one valid `event: snapshot\\ndata: {...}\\n\\n` chunk."""
    port = _free_port()

    state = {"snap": _make_snap(n_total=11)}
    monkeypatch.setattr(
        _ProfitQualityBroadcaster, "_default_build",
        classmethod(lambda cls: dict(state["snap"])),
    )
    # Pre-warm so subscribe() pushes immediately on connect.
    _ProfitQualityBroadcaster._tick_once()

    server = ThreadingDashboardServer(("127.0.0.1", port), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(5.0)
        client.connect(("127.0.0.1", port))
        client.sendall(
            b"GET /api/profit_quality/stream HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Accept: text/event-stream\r\n"
            b"\r\n"
        )

        buf = b""
        deadline = time.time() + 3.0
        # Read until we see at least one 'event: snapshot' frame OR the buffer
        # contains a double-newline indicating at least one frame sent.
        while time.time() < deadline:
            try:
                chunk = client.recv(4096)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            buf += chunk
            if b"event: snapshot\ndata: " in buf:
                break

        client.close()
        # Brief pause to let the server-side handler thread observe the
        # closed socket and exit cleanly.
        time.sleep(0.05)

        text = buf.decode("utf-8", errors="replace")

        # Status line.
        assert "HTTP/1.1 200 OK" in text or "HTTP/1.0 200 OK" in text, (
            f"unexpected status line. response: {text[:200]!r}"
        )
        # Required SSE headers.
        assert "Content-Type: text/event-stream" in text, (
            f"missing Content-Type: text/event-stream. response: {text[:300]!r}"
        )
        assert "X-Accel-Buffering: no" in text, (
            f"missing X-Accel-Buffering: no (defends against proxy buffering). "
            f"response: {text[:300]!r}"
        )
        # At least one snapshot frame.
        assert b"event: snapshot\ndata: " in buf or b"event: snapshot\r\ndata: " in buf, (
            f"never saw event: snapshot frame. response: {text[:600]!r}"
        )

        # Parse the first data line and assert its JSON has n_total == 11.
        # The format is "data: {...}\\n" within the frame.
        for line in text.splitlines():
            if line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
                # The SSE endpoint wraps the snapshot as
                # {"profit_quality": {...}, "meter": {...}} since the
                # payoff-paradox meter joined the stream.
                snap_payload = payload.get("profit_quality", payload)
                assert snap_payload.get("n_total") == 11, (
                    f"snapshot payload n_total mismatch: {payload!r}"
                )
                break
        else:
            pytest.fail(f"no 'data: ' line found in response: {text[:400]!r}")
    finally:
        server.shutdown()
        server.server_close()
