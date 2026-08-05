"""MT5Owner — process-global single-owner lifecycle tests (2026-08-04 repair).

Proves the ownership contract that fixed the "revolving door" bug:

* exactly one ``mt5.initialize()`` per process (reference-counted acquire);
* ``release()`` NEVER calls ``mt5.shutdown()`` — the shared session survives
  every worker's detach;
* ``reconnect()`` is the only owner-owned teardown+re-init (dead session);
* ``shutdown()`` runs exactly once and is idempotent;
* acquire() raises a clear ConnectionError when the package is missing.
"""

from __future__ import annotations

import logging

import core.mt5_owner as owner_mod


class _FakeMT5:
    def __init__(self) -> None:
        self.initialize_calls = 0
        self.shutdown_calls = 0

    def initialize(self, **kwargs) -> bool:
        self.initialize_calls += 1
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def account_info(self):
        return None


def _reset_owner() -> None:
    owner_mod.MT5Owner._instance = None


def _stub_connect_once(monkeypatch, fake: _FakeMT5) -> None:
    """Replace terminal discovery/initialize with a direct fake call."""

    def _fake_connect_once(self, config, logger):
        fake.initialize()
        return True

    monkeypatch.setattr(owner_mod.MT5Owner, "_connect_once", _fake_connect_once)


def test_acquire_initializes_once_and_release_never_shuts_down(monkeypatch):
    _reset_owner()
    fake = _FakeMT5()
    monkeypatch.setattr(owner_mod, "mt5", fake)
    _stub_connect_once(monkeypatch, fake)

    owner = owner_mod.MT5Owner.instance()
    log = logging.getLogger("test-owner")

    # First worker: the real initialize happens exactly once.
    assert owner.acquire({}, log) is True
    assert fake.initialize_calls == 1
    assert owner.refcount == 1
    assert owner.connected

    # Second worker attaches to the SAME session — no second initialize.
    assert owner.acquire({}, log) is True
    assert fake.initialize_calls == 1
    assert owner.refcount == 2

    # Detaching workers never calls mt5.shutdown().
    owner.release()
    owner.release()
    assert fake.shutdown_calls == 0
    assert owner.refcount == 0
    assert owner.connected  # session still alive for every worker

    # The single process-wide shutdown happens once at app exit.
    owner.shutdown()
    assert fake.shutdown_calls == 1
    assert not owner.connected
    assert owner.refcount == 0

    # Idempotent.
    owner.shutdown()
    assert fake.shutdown_calls == 1


def test_reconnect_is_owner_owned_teardown_and_reinit(monkeypatch):
    _reset_owner()
    fake = _FakeMT5()
    monkeypatch.setattr(owner_mod, "mt5", fake)
    _stub_connect_once(monkeypatch, fake)

    owner = owner_mod.MT5Owner.instance()
    log = logging.getLogger("test-owner")
    owner.acquire({}, log)
    assert fake.initialize_calls == 1

    # A dead session is torn down + re-initialized by the owner itself.
    assert owner.reconnect({}, log) is True
    assert fake.shutdown_calls == 1
    assert fake.initialize_calls == 2
    assert owner.connected
    assert owner.refcount == 1


def test_acquire_raises_when_mt5_unavailable(monkeypatch):
    _reset_owner()
    monkeypatch.setattr(owner_mod, "mt5", None)
    monkeypatch.setattr(owner_mod, "_MT5_IMPORT_ERROR", ImportError("No module named MetaTrader5"))

    owner = owner_mod.MT5Owner.instance()
    try:
        owner.acquire({})
    except ConnectionError as exc:
        assert "MetaTrader5 is unavailable" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("acquire() must raise ConnectionError when MetaTrader5 is missing")
    assert not owner.connected
