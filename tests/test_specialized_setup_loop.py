"""Tests for the observe-only specialized setup loop."""

from __future__ import annotations

import logging

import loops.specialized_setup_loop as loop


def _config() -> dict:
    return {
        "specialized_setup_loop": {
            "enabled": True,
            "max_symbols_to_scan": 1,
        },
        "intelligence": {
            "regime_filter": False,
            "setup_confidence_mult": 1.0,
        },
        "trading": {"aggressive_mode": False},
    }


def _feature(price: float = 100.0) -> dict:
    return {
        "price": price,
        "m5_trend": "bullish",
        "m15_trend": "bullish",
        "timeframe_alignment": True,
        "bb_position": 0.5,
        "support": 99.0,
        "resistance": 101.0,
        "breakout": "none",
        "rejection": "none",
    }


def test_scan_is_observe_only_and_bounded(monkeypatch):
    writes: dict[str, dict] = {}

    def fake_read(name, default=None):
        return {
            "features.json": {"timestamp": "features-ts", "symbols": {
                "XAUUSDm": _feature(),
                "USOILm": _feature(80.0),
            }},
            "market_context.json": {
                "timestamp": "context-ts",
                "market_context": {"symbols": {
                    "XAUUSDm": {
                        "regime": "trending",
                        "phase": "neutral",
                        "move_type": "continuation",
                        "session": "london_open",
                        "market_regime": {"primary": "strong_trend"},
                    },
                    "USOILm": {},
                }},
                "evidence": {"symbols": {
                    "XAUUSDm": {"trend": 0.9, "momentum": 0.8},
                    "USOILm": {},
                }},
            },
        }.get(name, default)

    monkeypatch.setattr(loop, "read_json_state", fake_read)
    monkeypatch.setattr(loop, "write_json_state", lambda name, data: writes.setdefault(name, data))
    monkeypatch.setattr(loop, "setup_logger", lambda *args, **kwargs: logging.getLogger("test-specialized"))

    result = loop.run(_config())

    assert result["specialized_setup_loop"] == "OK"
    report = writes[loop.REPORT_FILE]
    assert report["safety"] == {
        "mode": "observe_only",
        "orders_submitted": 0,
        "mt5_started": False,
        "execution_called": False,
    }
    assert report["symbols_scanned"] == 1
    assert list(report["observations"]) == ["XAUUSDm"]
    assert report["observations"]["XAUUSDm"]["top_setup"] is not None


def test_missing_features_writes_waiting_report(monkeypatch):
    writes: dict[str, dict] = {}
    monkeypatch.setattr(loop, "read_json_state", lambda name, default=None: default)
    monkeypatch.setattr(loop, "write_json_state", lambda name, data: writes.setdefault(name, data))
    monkeypatch.setattr(loop, "setup_logger", lambda *args, **kwargs: logging.getLogger("test-specialized"))
    monkeypatch.setattr(loop, "fail_safe_missing", lambda name, logger: True)

    result = loop.run(_config())

    assert result == {"specialized_setup_loop": "OK", "status": "waiting"}
    assert writes[loop.REPORT_FILE]["status_reason"] == "features.json_missing"
    assert writes[loop.REPORT_FILE]["safety"]["orders_submitted"] == 0


def test_malformed_state_and_config_are_safe(monkeypatch):
    writes: dict[str, dict] = {}
    monkeypatch.setattr(loop, "read_json_state", lambda name, default=None: [] if name == "features.json" else {"market_context": [], "evidence": []})
    monkeypatch.setattr(loop, "write_json_state", lambda name, data: writes.setdefault(name, data))
    monkeypatch.setattr(loop, "setup_logger", lambda *args, **kwargs: logging.getLogger("test-specialized"))
    monkeypatch.setattr(loop, "fail_safe_missing", lambda name, logger: False)

    result = loop.run({"specialized_setup_loop": {"max_symbols_to_scan": "not-a-number"}})

    assert result == {"specialized_setup_loop": "OK", "status": "waiting"}
    assert writes[loop.REPORT_FILE]["status_reason"] == "features.json_invalid_shape"


def test_disabled_loop_does_not_read_market_state(monkeypatch):
    writes: dict[str, dict] = {}
    monkeypatch.setattr(loop, "write_json_state", lambda name, data: writes.setdefault(name, data))
    monkeypatch.setattr(loop, "read_json_state", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("read should not run")))

    result = loop.run({"specialized_setup_loop": {"enabled": False}})

    assert result == {"specialized_setup_loop": "OK", "status": "disabled"}
    assert writes[loop.REPORT_FILE]["status"] == "disabled"
    assert writes[loop.REPORT_FILE]["safety"]["execution_called"] is False
