"""Live-trade thesis reviewer loop.

Re-scores each OPEN position against the CURRENT market regime + trend and the
latest candidate-signal side, to detect "thesis decay" -- the original reason
for the trade no longer holding -- and flag (or, when explicitly enabled, close)
positions whose entry thesis has decayed.

OBSERVE-ONLY BY DEFAULT. It never places trades and never closes a position
unless config.thesis_reviewer.live_close_enabled is explicitly true. Even then
it respects min_hold_seconds and skips on market-closed/back-off. On a real
account, run it in observe mode first and review state/thesis_review.json
before flipping live_close_enabled.

Scoring (per open position), thesis_score in 0..100, base 50:
  regime bias alignment (market_context.market_regime.symbols[sym].bias)   +/-30
  m5 trend alignment (features.symbols[sym].m5_trend)                     +/-20
  m15 trend alignment (features.symbols[sym].m15_trend)                    +/-15
  current top candidate side agreement (candidate_signals.candidates)     +/-10
clamp to [0,100]. recommendation:
  score < close_threshold (default 35) AND (regime against OR both trends
  against) -> "close"
  score < watch_threshold (default 50)                     -> "watch"
  otherwise                                                -> "hold"
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import load_config, read_json_state, setup_logger, utc_now_iso, write_json_state

STATE_FILE = "thesis_review.json"

# trend/bias string -> direction: +1 bullish, -1 bearish, 0 neutral
_BULLISH = {"bullish", "up", "long", "buy", "bull"}
_BEARISH = {"bearish", "down", "short", "sell", "bear"}


def _dir(token: Any) -> int:
    if token is None:
        return 0
    t = str(token).strip().lower()
    if t in _BULLISH:
        return 1
    if t in _BEARISH:
        return -1
    return 0


def _side_dir(side: str) -> int:
    return 1 if str(side).strip().upper() == "BUY" else -1


def _align(trend_dir: int, side_dir: int) -> int:
    """+1 if trend agrees with side, -1 if against, 0 if neutral."""
    if trend_dir == 0 or side_dir == 0:
        return 0
    return 1 if trend_dir == side_dir else -1


def thesis_reviewer_settings(config: dict[str, Any]) -> dict[str, Any]:
    return dict(config.get("thesis_reviewer") or {})


def _score_position(
    pos: dict[str, Any],
    regime: dict[str, Any],
    features: dict[str, Any],
    top_candidates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    sym = pos.get("symbol") or pos.get("broker_symbol")
    side = pos.get("side", "")
    side_dir = _side_dir(side)

    reg_sym = (regime.get("symbols") or {}).get(sym, {})
    bias_dir = _dir(reg_sym.get("bias"))

    feat_sym = (features.get("symbols") or {}).get(sym, {})
    m5_dir = _dir(feat_sym.get("m5_trend"))
    m15_dir = _dir(feat_sym.get("m15_trend"))

    cand = top_candidates.get(sym)
    cand_align = 0
    cand_side = None
    cand_conf = None
    if cand:
        cand_side = cand.get("side")
        cand_conf = cand.get("confidence")
        cand_align = _align(_side_dir(cand_side) if cand_side else 0, side_dir)

    reg_align = _align(bias_dir, side_dir)
    m5_align = _align(m5_dir, side_dir)
    m15_align = _align(m15_dir, side_dir)

    score = 50 + 30 * reg_align + 20 * m5_align + 15 * m15_align + 10 * cand_align
    score = max(0, min(100, int(round(score))))

    # Strong-evidence decay: regime against AND at least one trend against, or
    # both trends against regardless of regime.
    regime_against = reg_align == -1
    trends_against = sum(1 for a in (m5_align, m15_align) if a == -1)
    strong_decay = (regime_against and trends_against >= 1) or trends_against >= 2

    return {
        "ticket": pos.get("ticket") or pos.get("position_id"),
        "symbol": sym,
        "side": side,
        "size": pos.get("size"),
        "entry": pos.get("entry"),
        "profit": pos.get("profit"),
        "score": score,
        "regime_bias": reg_sym.get("bias"),
        "regime_primary": reg_sym.get("primary"),
        "m5_trend": feat_sym.get("m5_trend"),
        "m15_trend": feat_sym.get("m15_trend"),
        "candidate_side": cand_side,
        "candidate_confidence": cand_conf,
        "regime_align": reg_align,
        "m5_align": m5_align,
        "m15_align": m15_align,
        "strong_decay": strong_decay,
    }


def _recommendation(scored: dict[str, Any], close_threshold: float, watch_threshold: float) -> str:
    if scored["score"] < close_threshold and scored["strong_decay"]:
        return "close"
    if scored["score"] < watch_threshold:
        return "watch"
    return "hold"


def run(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_config()
    logger = setup_logger("thesis_review_loop", "thesis_review_loop.log")
    tr = thesis_reviewer_settings(config)

    if not tr.get("enabled", True):
        return {"enabled": False, "skipped": True}

    close_threshold = float(tr.get("close_threshold", 35))
    watch_threshold = float(tr.get("watch_threshold", 50))
    live_close_enabled = bool(tr.get("live_close_enabled", False))
    min_hold_seconds = float(tr.get("min_hold_seconds", 130))
    close_reason = str(tr.get("close_reason", "thesis_decay"))

    positions_data = read_json_state("paper_positions.json", default={}) or {}
    positions = list(positions_data.get("positions", []))

    market_context = read_json_state("market_context.json", default={}) or {}
    regime = market_context.get("market_regime") or market_context.get("regime") or {}
    features = read_json_state("features.json", default={}) or {}

    candidates = []
    cand_src = read_json_state("candidate_signals.json", default={}) or {}
    candidates = list(cand_src.get("candidates", []))
    if not candidates:
        ev = read_json_state("evaluated_signals.json", default={}) or {}
        candidates = list(ev.get("evaluated", []))
    # Keep the highest-confidence candidate per symbol for side-agreement.
    top_candidates: dict[str, dict[str, Any]] = {}
    for c in candidates:
        sym = c.get("symbol")
        if not sym:
            continue
        cur = top_candidates.get(sym)
        if cur is None or (c.get("confidence") or 0) > (cur.get("confidence") or 0):
            top_candidates[sym] = c

    scored = []
    recommendations: list[dict[str, Any]] = []
    counts = {"hold": 0, "watch": 0, "close": 0}
    closes_attempted = 0
    closes_succeeded = 0
    close_errors: list[dict[str, Any]] = []

    for pos in positions:
        s = _score_position(pos, regime, features, top_candidates)
        rec = _recommendation(s, close_threshold, watch_threshold)
        s["recommendation"] = rec
        counts[rec] += 1
        scored.append(s)
        recommendations.append({
            "ticket": s["ticket"],
            "symbol": s["symbol"],
            "side": s["side"],
            "score": s["score"],
            "recommendation": rec,
            "strong_decay": s["strong_decay"],
            "profit": s["profit"],
        })

    # Live close path -- ONLY when explicitly enabled. Observe mode never touches MT5.
    if live_close_enabled and counts["close"] > 0:
        from core.mt5_connection_manager import MT5ConnectionManager
        from core.mt5_broker import MT5Broker
        from core.market_hours import in_backoff, is_market_closed_error

        connection = MT5ConnectionManager(config, logger)
        try:
            connection.connect()
            broker = MT5Broker(config, logger)
            now_ts = utc_now_iso()
            for s in scored:
                if s["recommendation"] != "close":
                    continue
                opened = s.get("opened_at") or _opened_at_for(s["ticket"])
                if _held_seconds(opened, now_ts) < min_hold_seconds:
                    s["close_action"] = "skipped_min_hold"
                    continue
                if in_backoff(s["symbol"], config):
                    s["close_action"] = "skipped_market_backoff"
                    continue
                closes_attempted += 1
                try:
                    res = broker.close_position(
                        int(s["ticket"]), s["symbol"], s["side"], float(s["size"] or 0.01),
                        reason=close_reason,
                    )
                    if res.get("success"):
                        closes_succeeded += 1
                        s["close_action"] = "closed"
                        logger.info("Thesis decay close: #%s %s %s score=%s",
                                    s["ticket"], s["symbol"], s["side"], s["score"])
                    else:
                        err = res.get("error") or "unknown"
                        # market-closed -> record back-off so we don't spam
                        if is_market_closed_error(err):
                            try:
                                from core.market_hours import record_market_closed
                                record_market_closed(s["symbol"], config)
                            except Exception:
                                pass
                        close_errors.append({"ticket": s["ticket"], "error": err})
                        s["close_action"] = f"error:{err}"
                        logger.warning("Thesis decay close failed: #%s %s -> %s",
                                       s["ticket"], s["symbol"], err)
                except Exception as exc:  # noqa: BLE001
                    close_errors.append({"ticket": s["ticket"], "error": str(exc)})
                    s["close_action"] = f"exception:{exc}"
        finally:
            try:
                connection.disconnect()
            except Exception:
                pass

    state = {
        "timestamp": utc_now_iso(),
        "enabled": True,
        "live_close_enabled": live_close_enabled,
        "thresholds": {
            "close": close_threshold,
            "watch": watch_threshold,
            "min_hold_seconds": min_hold_seconds,
        },
        "open_positions": len(scored),
        "counts": counts,
        "closes_attempted": closes_attempted,
        "closes_succeeded": closes_succeeded,
        "close_errors": close_errors,
        "positions": scored,
        "recommendations": recommendations,
    }
    write_json_state(STATE_FILE, state)
    logger.info(
        "Thesis review: %d positions -> hold=%d watch=%d close=%d (live_close=%s, closes_ok=%d/%d)",
        len(scored), counts["hold"], counts["watch"], counts["close"],
        live_close_enabled, closes_succeeded, closes_attempted,
    )
    return {
        "thesis_review": "OK",
        "open_positions": len(scored),
        "counts": counts,
        "closes_succeeded": closes_succeeded,
        "closes_attempted": closes_attempted,
    }


def _opened_at_for(ticket: Any) -> str | None:
    """Look up the position's opened_at from position_open_times.json if available."""
    try:
        pot = read_json_state("position_open_times.json", default={}) or {}
        return pot.get(str(ticket))
    except Exception:
        return None


def _held_seconds(opened_at: str | None, now_iso: str) -> float:
    if not opened_at or not now_iso:
        return float("inf")  # unknown age -> do not block on min_hold
    try:
        from datetime import datetime
        a = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        b = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        return max(0.0, (b - a).total_seconds())
    except Exception:
        return float("inf")


if __name__ == "__main__":
    run()
