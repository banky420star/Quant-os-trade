"""Connection-manager dependency diagnostics."""

from __future__ import annotations

import logging
import sys

import core.mt5_connection_manager as manager


def test_missing_mt5_reports_runtime_and_install_command(monkeypatch):
    monkeypatch.setattr(manager, "mt5", None)
    monkeypatch.setattr(manager, "_MT5_IMPORT_ERROR", ImportError("No module named MetaTrader5"))

    connection = manager.MT5ConnectionManager({"mt5": {}}, logging.getLogger("test-mt5"))

    try:
        connection.connect()
    except ConnectionError as exc:
        message = str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("connect() must fail when MetaTrader5 is unavailable")

    assert "MetaTrader5 is unavailable in this Python runtime" in message
    assert sys.executable in message
    assert "-m pip install -r requirements.txt" in message
