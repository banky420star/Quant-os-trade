"""Policy scoring helpers for evaluation_loop (shadow mode)."""

from __future__ import annotations

from typing import Any


def _session_from_signal(signal: dict[str, Any]) -> str:
    ctx = signal.get("market_context") or {}
    if isinstance(ctx, dict):
        return str(ctx.get("session") or "unknown")
    return "unknown"


def recent_symbol_stats(
    trades: list[dict[str, Any]],
    symbol: str,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    """Win rate and net PnL for recent closes on one symbol."""
    rows = [t for t in trades if t.get("symbol") == symbol][-limit:]
    if not rows:
        return {"n": 0, "wins": 0, "losses": 0, "win_rate_pct": 50.0, "net_pnl": 0.0}
    wins = sum(1 for t in rows if t.get("result") == "win")
    losses = len(rows) - wins
    net = sum(float(t.get("pnl") or 0) for t in rows)
    wr = 100.0 * wins / len(rows) if rows else 50.0
    return {
        "n": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": wr,
        "net_pnl": net,
    }


def session_entry_bias(session: str) -> dict[str, Any]:
    """Session defaults — rollover is high caution."""
    s = (session or "unknown").lower()
    if s == "rollover":
        return {
            "entry_type": "limit",
            "limit_offset_atr": 0.15,
            "sl_atr_mult": 1.0,
            "tp1_r": 0.8,
            "tp2_r": 1.1,
            "break_even_trigger_r": 0.35,
            "trail_start_r": 0.55,
            "trail_atr_mult": 0.4,
            "session_weight": -12,
            "max_hold_minutes": 12,
            "cancel_if_not_filled_seconds": 90,
        }
    if s in ("overlap_london_ny", "london_open", "new_york"):
        return {
            "entry_type": "market",
            "limit_offset_atr": 0.08,
            "sl_atr_mult": 1.2,
            "tp1_r": 1.0,
            "tp2_r": 1.5,
            "break_even_trigger_r": 0.45,
            "trail_start_r": 0.7,
            "trail_atr_mult": 0.45,
            "session_weight": 8,
            "max_hold_minutes": 25,
            "cancel_if_not_filled_seconds": 120,
        }
    if s == "tokyo":
        return {
            "entry_type": "limit",
            "limit_offset_atr": 0.12,
            "sl_atr_mult": 1.1,
            "tp1_r": 0.9,
            "tp2_r": 1.2,
            "break_even_trigger_r": 0.4,
            "trail_start_r": 0.6,
            "trail_atr_mult": 0.42,
            "session_weight": 4,
            "max_hold_minutes": 18,
            "cancel_if_not_filled_seconds": 100,
        }
    return {
        "entry_type": "limit",
        "limit_offset_atr": 0.1,
        "sl_atr_mult": 1.15,
        "tp1_r": 0.95,
        "tp2_r": 1.25,
        "break_even_trigger_r": 0.4,
        "trail_start_r": 0.65,
        "trail_atr_mult": 0.43,
        "session_weight": 0,
        "max_hold_minutes": 20,
        "cancel_if_not_filled_seconds": 110,
    }


def compute_policy_score(
    signal: dict[str, Any],
    feat: dict[str, Any],
    *,
    spread_points: float | None = None,
    recent: dict[str, Any] | None = None,
    session_bias: dict[str, Any] | None = None,
) -> tuple[float, list[str]]:
    """Weighted score 0–100 with human-readable reason fragments."""
    reasons: list[str] = []
    score = 50.0

    eq = float(signal.get("entry_quality") or 50)
    score += (eq - 50) * 0.35
    reasons.append(f"entry_quality={eq:.0f}")

    conf = float(signal.get("confidence") or 50)
    score += (conf - 50) * 0.15

    dist = float(signal.get("distance_atr") or 0)
    if dist <= 0.35:
        score += 10
        reasons.append("close_anchor")
    elif dist > 1.5:
        score -= 15
        reasons.append("far_anchor")

    vol = float(feat.get("volume_ratio") or feat.get("relative_volume") or 1.0)
    if vol >= 1.2:
        score += 6
        reasons.append("volume_strong")
    elif vol < 0.7:
        score -= 8
        reasons.append("volume_weak")

    if spread_points is not None:
        if spread_points > 80:
            score -= 20
            reasons.append("spread_high")
        elif spread_points < 30:
            score += 4

    sb = session_bias or {}
    score += float(sb.get("session_weight") or 0)
    session = _session_from_signal(signal)
    if session:
        reasons.append(f"session={session}")

    recent = recent or {}
    n = int(recent.get("n", 0))
    if n >= 5:
        wr = float(recent.get("win_rate_pct", 50.0))
        # Win-rate weight raised 0.2 -> 0.5: a 0% symbol now costs -25, not -10,
        # so a cold streak actually drags the score below the execute threshold.
        score += (wr - 50) * 0.5
        net = float(recent.get("net_pnl") or 0)
        # Micro-lot PnL is tiny ($0.01/trade); lower the loss threshold so a
        # string of small realized losses still flags the symbol.
        if net < -0.15:
            score -= 10
            reasons.append("recent_symbol_losses")
        elif net > 0.15:
            score += 6
            reasons.append("recent_symbol_wins")
        # Hard cold-streak penalty: >=8 recent trades under 20% win rate.
        if n >= 8 and wr < 20.0:
            score -= 30
            reasons.append("recent_symbol_cold")

    if signal.get("within_reach") is False:
        score -= 25
        reasons.append("unreachable")

    return max(0.0, min(100.0, score)), reasons