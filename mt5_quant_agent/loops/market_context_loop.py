"""Market Context Loop — what is the market trying to do?"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.evidence_engine import EvidenceEngine
from core.market_context import MarketContextEngine
from core.market_regime import MarketRegimeEngine
from core.setup_library import list_setups
from core.utils import fail_safe_missing, load_config, read_json_state, setup_logger, utc_now_iso, write_json_state


def _update_regime_history(features: dict, regimes: dict, logger) -> None:
    """Track per-symbol M15-trend + regime-bias across ticks so the broker can
    detect a confirmed direction FLIP (USER-AUTHORIZED 2026-06-30 regime-flip
    trade replacement). A flip is recorded only when BOTH the bias and the
    M15 trend switch bullish<->bearish vs the prior tick (neutral excluded so a
    flip is a real directional change, not noise). The latest flip per symbol
    persists so the broker can check recency (regime_flip_window_sec).

    Writes state/regime_history.json:
      {updated_at, prior: {SYM:{bias,m15_trend}}, flips: {SYM:{bias_from,
       bias_to, m15_from, m15_to, at}}}
    """
    bull_bear = {"bullish", "bearish"}
    prior_state = read_json_state("regime_history.json", default={}) or {}
    prior = prior_state.get("prior", {}) or {}
    flips = dict(prior_state.get("flips", {}) or {})  # retain last flip per symbol
    now = utc_now_iso()
    cur: dict = {}
    feats = features.get("symbols", {}) or {}
    regs = regimes.get("symbols", {}) or {}
    for sym, feat in feats.items():
        try:
            m15 = feat.get("m15_trend")
            bias = (regs.get(sym) or {}).get("bias")
            cur[sym] = {"bias": bias, "m15_trend": m15}
            p = prior.get(sym, {}) or {}
            pb, pm = p.get("bias"), p.get("m15_trend")
            if (
                pb in bull_bear and bias in bull_bear and pb != bias
                and pm in bull_bear and m15 in bull_bear and pm != m15
            ):
                flips[sym] = {
                    "bias_from": pb, "bias_to": bias,
                    "m15_from": pm, "m15_to": m15, "at": now,
                }
                logger.info("REGIME FLIP %s: bias %s->%s m15_trend %s->%s",
                            sym, pb, bias, pm, m15)
        except Exception as exc:
            logger.warning("regime history failed for %s: %s", sym, exc)
    write_json_state("regime_history.json",
                     {"updated_at": now, "prior": cur, "flips": flips})


def run() -> dict | None:
    config = load_config()
    logger = setup_logger("market_context_loop", "market_context_loop.log")
    logger.info("=== Market Context Loop starting ===")

    if fail_safe_missing("features.json", logger):
        return None

    features = read_json_state("features.json")
    candles = read_json_state("latest_candles.json", default={})

    ctx_engine = MarketContextEngine(logger)
    context = ctx_engine.analyze_all(features, candles)

    regime_engine = MarketRegimeEngine(logger)
    regimes = regime_engine.classify_all(features, context)
    for symbol, ctx in context.get("symbols", {}).items():
        try:
            ctx["market_regime"] = regimes.get("symbols", {}).get(symbol, {})
        except Exception as exc:
            logger.warning("regime attach failed for %s: %s", symbol, exc)

    evidence_engine = EvidenceEngine(config, logger)
    evidence = evidence_engine.compute_all(features, context)

    output = {
        "timestamp": context["timestamp"],
        "market_context": context,
        "market_regime": regimes,
        "evidence": evidence,
        "setup_library": list_setups(),
    }
    write_json_state("market_context.json", output)
    _update_regime_history(features, regimes, logger)
    logger.info("Saved market_context.json for %d symbols", len(context.get("symbols", {})))
    logger.info("=== Market Context Loop complete ===")
    return output


if __name__ == "__main__":
    run()