"""Tests for MT5TerminalManager.is_alive path normalization (Phase 2.4 fix)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.mt5_terminal_manager import MT5TerminalManager


def _mgr(monkeypatch, processes):
    m = MT5TerminalManager({"mt5": {"path": r"C:\Users\Administrator\MT5Agent\terminal64.exe"}})
    monkeypatch.setattr(m, "list_processes", lambda: processes)
    return m


def test_is_alive_matches_when_exe_lacks_drive_letter(monkeypatch):
    """psutil sometimes drops the C: drive prefix; is_alive must still match."""
    mgr = _mgr(monkeypatch, [
        {"pid": 3312, "name": "terminal64.exe", "exe": r"\Users\Administrator\MT5Agent\terminal64.exe",
         "session_id": 2, "alive": True},
    ])
    assert mgr.is_alive(r"C:\Users\Administrator\MT5Agent\terminal64.exe") is True


def test_is_alive_matches_case_insensitive(monkeypatch):
    mgr = _mgr(monkeypatch, [
        {"pid": 1, "name": "terminal64.exe", "exe": r"C:\users\administrator\mt5agent\Terminal64.exe",
         "session_id": 2, "alive": True},
    ])
    assert mgr.is_alive(r"C:\Users\Administrator\MT5Agent\terminal64.exe") is True


def test_is_alive_false_for_noninteractive_session(monkeypatch):
    mgr = _mgr(monkeypatch, [
        {"pid": 5916, "name": "terminal64.exe", "exe": r"C:\Users\Administrator\MT5Agent\terminal64.exe",
         "session_id": 0, "alive": True},
    ])
    assert mgr.is_alive(r"C:\Users\Administrator\MT5Agent\terminal64.exe") is False


def test_is_alive_false_when_no_processes(monkeypatch):
    mgr = _gr = _mgr(monkeypatch, [])
    assert mgr.is_alive(r"C:\Users\Administrator\MT5Agent\terminal64.exe") is False
