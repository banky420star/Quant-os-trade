"""Live news-sentiment SHADOW risk filter (writes state/news_sentiment_snapshot.json).

USER REQUEST 2026-07-31 — wire the LLM news pipeline (scripts/news_sentiment_pipeline.py,
built cycle 19 but never wired into the live bot) into the live loop as a SHADOW
risk filter. The research verdict on news (VERDICT.md, cycle-19 / 2026-06-28)
found NO tradeable OOS edge from news-sentiment at retail 30bps — so this is NOT
a trade signal. It is a risk filter: a strongly bearish macro-news backdrop
(e.g. acute war/inflation headline cluster) is a reason to be *cautious* on
risk-on entries, not a reason to trade. Shadow by default — it records a
``sentiment_hint`` the verifier exposes as a diagnostic check that ALWAYS
PASSES unless an operator explicitly turns on ``news.sentiment_gate``.

Reuses the tested fetcher + classifier classes from the research pipeline so
the live and research surfaces stay consistent. The LLM (Ollama/cloud) is the
RIGHT tool here — this is LIVE, non-reproducible is fine, we want the most
accurate polarity per headline. Falls back to deterministic keyword polarity
when no LLM is reachable so the loop never hard-fails.

NO LIVE TRADING implications by default. This module only writes state.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from core.utils import read_json_state, utc_now_iso, write_json_state

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SNAPSHOT_FILE = "news_sentiment_snapshot.json"


def news_config(config: dict[str, Any]) -> dict[str, Any]:
    cfg = config.get("news") or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        # sentiment_gate=false -> the verifier's news_sentiment check ALWAYS
        # passes (pure shadow / observability). true -> signals opposing the
        # macro-news sentiment are hard-rejected. Default off: research found
        # no tradeable news edge, so a gate would be acting on noise.
        "sentiment_gate": bool(cfg.get("sentiment_gate", False)),
        "source": str(cfg.get("source", "rss")),
        "sentiment": str(cfg.get("sentiment", "auto")),
        "ollama_model": str(cfg.get("ollama_model", "kimi-k2.5:cloud")),
        "lookback_hours": int(cfg.get("lookback_hours", 24)),
        "max_items_per_theme": int(cfg.get("max_items_per_theme", 10)),
        "themes": list(cfg.get("themes", ["inflation", "employment", "taxes", "war"])),
        # Aggregation: a theme cluster counts as a strong hint only when its
        # mean sentiment magnitude exceeds this floor AND it has >= min_items
        # headlines (one headline is not a regime).
        "strong_hint_threshold": float(cfg.get("strong_hint_threshold", 0.4)),
        "strong_hint_min_items": int(cfg.get("strong_hint_min_items", 3)),
    }


def _window(config: dict[str, Any]) -> tuple[Any, Any]:
    import pandas as pd

    end = pd.Timestamp.now(tz="UTC")
    start = end - pd.Timedelta(hours=news_config(config)["lookback_hours"])
    return start, end


def _pick_classifier(ncfg: dict[str, Any]) -> tuple[Any, str]:
    """Choose the sentiment backend. Mirrors the research pipeline's auto logic."""
    from scripts.news_sentiment_pipeline import (
        DeterministicToneClassifier,
        LLMClassifier,
        OllamaClassifier,
    )

    sent = ncfg["sentiment"]
    llm = LLMClassifier()
    ollama = OllamaClassifier(model=ncfg["ollama_model"])
    if sent == "ollama" and ollama.is_available():
        return ollama, "ollama"
    if sent == "llm" and llm.is_available():
        return llm, "llm"
    if sent == "deterministic":
        return DeterministicToneClassifier(), "deterministic"
    # auto
    if ollama.is_available():
        return ollama, "ollama"
    if llm.is_available():
        return llm, "llm"
    return DeterministicToneClassifier(), "deterministic"


def build_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    """Fetch recent news, classify sentiment per theme, return the snapshot.

    Network-tolerant: a failed fetch/classify yields an empty theme, not a
    crash — the live loop must never block on a news API hiccup.
    """
    ncfg = news_config(config)
    from scripts.news_sentiment_pipeline import RSSNewsFetcher

    start, end = _window(config)
    fetcher = RSSNewsFetcher()

    themes_out: dict[str, Any] = {}
    total_items = 0
    errors: list[str] = []
    try:
        clf, backend = _pick_classifier(ncfg)
    except Exception as exc:  # noqa: BLE001
        clf = None
        backend = "unavailable"
        errors.append(f"classifier_init: {exc}")

    for theme in ncfg["themes"]:
        try:
            items = fetcher.fetch(theme, start, end)
        except Exception as exc:  # noqa: BLE001
            items = []
            errors.append(f"fetch:{theme}: {exc}")
        items = items[: ncfg["max_items_per_theme"]]
        scores: list[float] = []
        top: list[dict[str, Any]] = []
        for it in items:
            try:
                s = float(clf.classify(it)) if clf is not None else 0.0
            except Exception:  # noqa: BLE001
                s = 0.0
            s = max(-1.0, min(1.0, s))
            scores.append(s)
            top.append({"text": it.get("text", "")[:200], "sentiment": round(s, 3),
                        "time": str(it.get("time"))})
        n = len(scores)
        mean_s = sum(scores) / n if n else 0.0
        themes_out[theme] = {
            "n_items": n,
            "mean_sentiment": round(mean_s, 3),
            "items": top,
        }
        total_items += n

    # Aggregate macro-news sentiment (equal-weight across themes that actually
    # returned items, so a single silent feed doesn't drag the signal to 0).
    theme_means = [v["mean_sentiment"] for v in themes_out.values() if v["n_items"] > 0]
    agg = sum(theme_means) / len(theme_means) if theme_means else 0.0

    # Strong-hint detection: at least one theme cluster is both deep enough and
    # one-sided enough to be a real macro-news regime, not noise.
    strong_hint = None
    for theme, v in themes_out.items():
        if (
            v["n_items"] >= ncfg["strong_hint_min_items"]
            and abs(v["mean_sentiment"]) >= ncfg["strong_hint_threshold"]
        ):
            strong_hint = {
                "theme": theme,
                "direction": "bullish" if v["mean_sentiment"] > 0 else "bearish",
                "mean_sentiment": v["mean_sentiment"],
                "n_items": v["n_items"],
            }
            break

    hint = "neutral"
    if strong_hint:
        hint = strong_hint["direction"]

    return {
        "updated_at": utc_now_iso(),
        "source": ncfg["source"],
        "sentiment_backend": backend,
        "lookback_hours": ncfg["lookback_hours"],
        "themes": themes_out,
        "aggregate": {
            "mean_sentiment": round(agg, 3),
            "n_items": total_items,
            "n_themes_with_items": len(theme_means),
        },
        "hint": hint,
        "strong_hint": strong_hint,
        "sentiment_gate": ncfg["sentiment_gate"],
        "errors": errors,
    }


def run(config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Recompute the news-sentiment snapshot. No-op when disabled."""
    from core.utils import load_config

    cfg = config or load_config()
    ncfg = news_config(cfg)
    if not ncfg["enabled"]:
        return None
    snap = build_snapshot(cfg)
    write_json_state(SNAPSHOT_FILE, snap)
    return snap


def sentiment_hint_for(symbol: str | None, config: dict[str, Any]) -> dict[str, Any]:
    """Verifier-facing read: the current macro-news hint + whether gating is on.

    Returns {'hint': str, 'score': float, 'gate': bool, 'available': bool}.
    'available'=False (disabled or no snapshot) means the verifier check is
    treated as shadow-pass regardless of ``gate`` — we never block on missing
    news data.
    """
    ncfg = news_config(config)
    if not ncfg["enabled"]:
        return {"hint": "neutral", "score": 0.0, "gate": False, "available": False}
    snap = read_json_state(SNAPSHOT_FILE, default={}) or {}
    if not snap:
        return {"hint": "neutral", "score": 0.0, "gate": ncfg["sentiment_gate"], "available": False}
    agg = snap.get("aggregate") or {}
    return {
        "hint": str(snap.get("hint") or "neutral"),
        "score": float(agg.get("mean_sentiment", 0.0) or 0.0),
        "gate": bool(ncfg["sentiment_gate"]),
        "available": True,
    }