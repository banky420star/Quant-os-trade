"""Pipeline loop wrapper for the news-sentiment SHADOW risk filter.

Network-bound (RSS + LLM classification), so this is registered as an
ANALYTICAL loop and additionally self-throttles by ``news.refresh_interval_seconds``
(default 900s = 15min): it skips when the last snapshot is fresher than the
interval. When ``news.enabled`` is false (default) the whole loop is a no-op
that returns before any network call.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.news_sentiment import news_config, run as _ns_run, SNAPSHOT_FILE  # noqa: E402
from core.utils import read_json_state, setup_logger  # noqa: E402

# Throttle: skip the network fetch if the snapshot is fresher than this. Lives
# outside config so a misconfig can't disable the throttle and hammer feeds.
DEFAULT_REFRESH_SECONDS = 900


def _throttle_ok(config: dict[str, Any]) -> bool:
    import time
    from datetime import datetime, timezone

    interval = int((config.get("news") or {}).get("refresh_interval_seconds", DEFAULT_REFRESH_SECONDS))
    snap = read_json_state(SNAPSHOT_FILE, default={}) or {}
    updated = str(snap.get("updated_at") or "")
    if not updated:
        return True
    try:
        # updated_at is an ISO timestamp from utc_now_iso()
        ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age >= interval
    except Exception:
        return True


def run() -> dict[str, Any] | None:
    from core.utils import load_config

    config = load_config()
    logger = setup_logger("news_sentiment_loop", "news_sentiment_loop.log")
    ncfg = news_config(config)
    if not ncfg["enabled"]:
        return None
    if not _throttle_ok(config):
        logger.debug("news_sentiment: throttled (snapshot fresh)")
        return None
    try:
        snap = _ns_run(config)
        if snap:
            agg = snap.get("aggregate", {})
            logger.info(
                "News sentiment refreshed: hint=%s agg=%.3f items=%d backend=%s gate=%s",
                snap.get("hint"), float(agg.get("mean_sentiment", 0)),
                agg.get("n_items"), snap.get("sentiment_backend"), snap.get("sentiment_gate"),
            )
        return snap
    except Exception as exc:  # noqa: BLE001
        # Network/LLM failures must never break the trading pipeline.
        logger.warning("news_sentiment refresh failed: %s", exc)
        return None


if __name__ == "__main__":
    run()