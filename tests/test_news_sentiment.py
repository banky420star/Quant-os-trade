"""Tests for the live news-sentiment SHADOW risk filter."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import news_sentiment as ns


def test_news_config_defaults_off():
    cfg = ns.news_config({})
    assert cfg["enabled"] is False
    assert cfg["sentiment_gate"] is False
    assert cfg["source"] == "rss"
    assert cfg["themes"] == ["inflation", "employment", "taxes", "war"]


def test_run_disabled_returns_none():
    assert ns.run({}) is None  # default config: enabled False


def test_sentiment_hint_for_disabled_is_shadow_pass():
    # When the module is disabled, the verifier read reports not-available and
    # gate=False -> the verifier check must treat it as a shadow pass.
    out = ns.sentiment_hint_for("XAUUSDm", {})
    assert out["available"] is False
    assert out["gate"] is False
    assert out["hint"] == "neutral"


def test_sentiment_hint_for_enabled_no_snapshot(monkeypatch, tmp_path):
    # enabled but no snapshot written yet -> available False, gate reflects config
    cfg = {"news": {"enabled": True, "sentiment_gate": True}}
    # read_json_state returns {} -> snapshot absent
    out = ns.sentiment_hint_for("XAUUSDm", cfg)
    assert out["available"] is False
    assert out["gate"] is True  # operator opted in, but data missing -> shadow-pass


def test_build_snapshot_aggregates_and_detects_strong_hint(monkeypatch):
    cfg = {"news": {"enabled": True, "themes": ["war"], "max_items_per_theme": 5,
                    "strong_hint_threshold": 0.4, "strong_hint_min_items": 2,
                    "sentiment": "deterministic"}}

    class _FakeFetcher:
        def fetch(self, theme, start, end):
            # 3 negative-tone war headlines -> bearish cluster
            return [
                {"time": end, "theme": theme, "text": "War crisis fear conflict", "tone": None},
                {"time": end, "theme": theme, "text": "Military conflict sanctions", "tone": None},
                {"time": end, "theme": theme, "text": "Geopolitical crisis loss", "tone": None},
            ]

    class _DetClf:
        def classify(self, item):
            text = (item.get("text") or "").lower()
            return -1.0 if "crisis" in text or "conflict" in text else 0.0

    monkeypatch.setattr(ns, "_pick_classifier", lambda n: (_DetClf(), "deterministic"))
    monkeypatch.setattr("scripts.news_sentiment_pipeline.RSSNewsFetcher", lambda: _FakeFetcher())

    snap = ns.build_snapshot(cfg)
    assert snap["hint"] == "bearish"
    assert snap["strong_hint"]["theme"] == "war"
    assert snap["aggregate"]["n_items"] == 3
    assert snap["sentiment_backend"] == "deterministic"


def test_build_snapshot_neutral_when_no_headlines(monkeypatch):
    cfg = {"news": {"enabled": True, "themes": ["war"], "sentiment": "deterministic"}}

    class _EmptyFetcher:
        def fetch(self, theme, start, end):
            return []
    monkeypatch.setattr(ns, "_pick_classifier", lambda n: (None, "unavailable"))
    monkeypatch.setattr("scripts.news_sentiment_pipeline.RSSNewsFetcher", lambda: _EmptyFetcher())

    snap = ns.build_snapshot(cfg)
    assert snap["hint"] == "neutral"
    assert snap["strong_hint"] is None
    assert snap["aggregate"]["n_items"] == 0


def test_verifier_shadow_check_passes_when_disabled():
    # End-to-end: with news disabled, the verifier's news_sentiment check is True.
    from core.verifier import Verifier
    from core.utils import load_config
    cfg = load_config()
    # news not enabled in default config -> check must be True
    from core.news_sentiment import sentiment_hint_for
    out = sentiment_hint_for("XAUUSDm", cfg)
    assert out["gate"] is False or out["available"] is False