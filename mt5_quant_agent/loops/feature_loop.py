"""Agent 2: Feature Loop — convert candles to quant features."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.feature_engine import FeatureEngine
from core.history_manager import HistoryManager
from core.utils import (
    fail_safe_missing,
    load_config,
    read_json_state,
    setup_logger,
    write_json_state,
)


def _validate_candles_payload(candles: dict) -> None:
    """Ensure latest_candles.json has the structure feature computation expects."""
    for key in ("timestamp", "symbols"):
        if key not in candles:
            raise ValueError(f"latest_candles.json missing field: {key}")
    if not candles["symbols"]:
        raise ValueError("latest_candles.json has no symbols")


def run() -> dict | None:
    """Compute technical features from latest candles."""
    config = load_config()
    logger = setup_logger("feature_loop", "feature_loop.log")
    logger.info("Starting feature loop")

    if fail_safe_missing("latest_candles.json", logger):
        return None

    try:
        candles = read_json_state("latest_candles.json")
        _validate_candles_payload(candles)

        history_mgr = None
        if config.get("history", {}).get("enabled", True) and config.get("features", {}).get(
            "use_history_lookback", True
        ):
            history_mgr = HistoryManager(config, collector=None, logger=logger)
            logger.info("History lookback enabled (parquet augmentation)")

        engine = FeatureEngine(config, history_mgr, logger)
        features = engine.compute_all(candles)

        symbol_count = len(features.get("symbols", {}))
        if symbol_count == 0:
            logger.error("No symbols produced features — check candle data")
            return None

        for symbol, feat in features["symbols"].items():
            missing = FeatureEngine.validate_symbol_features(feat)
            if missing:
                logger.warning("%s feature missing schema fields: %s", symbol, ", ".join(missing))

        write_json_state("features.json", features)
        logger.info(
            "Saved features.json for %d symbols (source=%s)",
            symbol_count,
            features.get("source", "unknown"),
        )
        return features

    except Exception as exc:
        logger.error("Feature loop failed: %s", exc)
        logger.error(traceback.format_exc())
        raise


if __name__ == "__main__":
    run()