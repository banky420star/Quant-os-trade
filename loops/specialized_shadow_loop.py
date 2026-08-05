"""Pipeline loop wrapper for the specialized-setups SHADOW fire-ledger analyzer.

Reads ``state/specialized_shadow_ledger.jsonl`` (populated by
``core.specialized_setups._shadow_log`` when ``signals.specialized_setups.shadow``
is true) and turns the raw fire log into a per-symbol / per-setup / per-session
fire-frequency report at ``state/specialized_shadow_report.json``.

This is a no-orders, read-only ANALYTICAL loop (mirrors news_sentiment_loop):
it never initializes MT5, never calls a broker, never touches the kill switch.
Registered in ``core/pipeline.py`` as an ANALYTICAL loop so it runs every
``ANALYTICS_EVERY_N`` (5) pipeline cycles instead of every cycle.

When ``signals.specialized_setups.shadow`` is false the loop is a no-op — there
is no ledger to analyze.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.specialized_setups import specialized_config  # noqa: E402
from core.specialized_shadow_analyzer import run as _analyze_shadow  # noqa: E402
from core.utils import load_config, setup_logger  # noqa: E402


def run() -> dict[str, Any] | None:
    """Analyze the specialized-setup shadow ledger into a fire report.

    Returns the report doc on success, None when shadow mode is off or the
    analysis is skipped. Never raises into the pipeline.
    """
    config = load_config()
    logger = setup_logger("specialized_shadow_loop", "specialized_shadow_loop.log")

    if not specialized_config(config).get("shadow"):
        logger.debug("specialized shadow ledger disabled (shadow=false) — skip")
        return None

    try:
        report = _analyze_shadow()
        if report:
            logger.info(
                "Specialized shadow report: %d fires across %d cells "
                "(%d thin, %d symbols)",
                report.get("total_fires", 0),
                report.get("distinct_cells", 0),
                report.get("thin_cells", 0),
                len(report.get("per_symbol", {}) or {}),
            )
        return report
    except Exception as exc:  # noqa: BLE001 — analysis must never break the pipeline
        logger.warning("specialized shadow analysis failed: %s", exc)
        return None


if __name__ == "__main__":
    run()
