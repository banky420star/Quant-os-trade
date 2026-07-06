"""Score execution-policy variants in shadow mode (no live trading changes)."""

from __future__ import annotations

import hashlib
from typing import Any

from core.policy_score import compute_policy_score, recent_symbol_stats, session_entry_bias

ENTRY_VARIANTS: list[dict[str, Any]] = [
    {"entry_type": "market", "limit_offset_atr": 0.0},
    {"entry_type": "limit", "limit_offset_atr": 0.05},
    {"entry_type": "limit", "limit_offset_atr": 0.10},
    {"entry_type": "limit", "limit_offset_atr": 0.20},
]

MANAGEMENT_COMBOS: list[dict[str, Any]] = [
    {"sl_atr_mult": 1.0, "tp1_r": 0.8, "be_trigger_r": 0.35, "trail_start_r": 0.55},
    {"sl_atr_mult": 1.15, "tp1_r": 0.95, "be_trigger_r": 0.40, "trail_start_r": 0.65},
    {"sl_atr_mult": 1.2, "tp1_r": 1.0, "be_trigger_r": 0.45, "trail_start_r": 0.70},
]

_PARAM_KEYS = ("sl_atr_mult", "tp1_r", "be_trigger_r", "trail_start_r")


def policy_detection_enabled(config: dict[str, Any]) -> bool:
    return bool((config.get("policy_detection") or {}).get("enabled", True))


def policy_detection_mode(config: dict[str, Any]) -> str:
    return str((config.get("policy_detection") or {}).get("mode") or "shadow")


def _detection_cfg(config: dict[str, Any]) -> dict[str, Any]:
    cfg = config.get("policy_detection") or {}
    return {
        "min_sample_n": int(cfg.get("min_sample_n", 3)),
        "trade_lookback": int(cfg.get("trade_lookback", 200)),
        "insufficient_sample_penalty": float(cfg.get("insufficient_sample_penalty", 12.0)),
    }


def cell_key(symbol: str, setup_type: str, session: str) -> str:
    return f"{symbol}|{setup_type or 'unknown'}|{session or 'unknown'}"


def parse_cell_key(key: str) -> tuple[str, str, str]:
    parts = str(key).split("|", 2)
    while len(parts) < 3:
        parts.append("unknown")
    return parts[0], parts[1], parts[2]


def _trade_session(trade: dict[str, Any]) -> str:
    mc = trade.get("market_context") or {}
    if isinstance(mc, dict) and mc.get("session"):
        return str(mc["session"])
    return str(trade.get("session") or "unknown")


def _trade_setup(trade: dict[str, Any]) -> str:
    return str(trade.get("setup_type") or trade.get("setup") or "unknown")


def _signal_session(signal: dict[str, Any]) -> str:
    mc = signal.get("market_context") or {}
    if isinstance(mc, dict) and mc.get("session"):
        return str(mc["session"])
    return "unknown"


def _management_from_trade(trade: dict[str, Any]) -> dict[str, float]:
    prof = trade.get("management_profile") or {}
    meta = trade.get("meta") or {}
    if not prof and isinstance(meta, dict):
        prof = meta.get("management_profile") or {}
    return {
        "sl_atr_mult": float(prof.get("sl_atr_mult") or 1.15),
        "tp1_r": float(prof.get("tp1_r") or 0.95),
        "be_trigger_r": float(prof.get("break_even_trigger_r") or prof.get("be_trigger_r") or 0.4),
        "trail_start_r": float(prof.get("trail_start_r") or 0.65),
    }


def _param_distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    """0 = identical params, higher = more divergent (normalized ~0–1)."""
    total = 0.0
    scales = {
        "sl_atr_mult": 0.5,
        "tp1_r": 0.5,
        "be_trigger_r": 0.25,
        "trail_start_r": 0.35,
    }
    for key in _PARAM_KEYS:
        scale = scales[key]
        total += abs(float(a.get(key) or 0) - float(b.get(key) or 0)) / scale
    return total / len(_PARAM_KEYS)


def make_policy_id(variant: dict[str, Any]) -> str:
    entry = str(variant.get("entry_type") or "limit")
    offset = float(variant.get("limit_offset_atr") or 0.0)
    parts = [
        entry[:3],
        f"lo{offset:.2f}".replace(".", ""),
        f"sl{float(variant.get('sl_atr_mult') or 0):.2f}".replace(".", ""),
        f"tp{float(variant.get('tp1_r') or 0):.2f}".replace(".", ""),
        f"be{float(variant.get('be_trigger_r') or 0):.2f}".replace(".", ""),
        f"tr{float(variant.get('trail_start_r') or 0):.2f}".replace(".", ""),
    ]
    raw = "_".join(parts)
    digest = hashlib.md5(raw.encode()).hexdigest()[:8]
    return f"pol_{digest}"


def build_policy_variants() -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    for entry in ENTRY_VARIANTS:
        for mgmt in MANAGEMENT_COMBOS:
            row = {
                "entry_type": entry["entry_type"],
                "limit_offset_atr": float(entry["limit_offset_atr"]),
                **mgmt,
            }
            row["policy_id"] = make_policy_id(row)
            variants.append(row)
    return variants


def _cell_trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {
            "n": 0,
            "wins": 0,
            "win_rate_pct": 50.0,
            "avg_r": 0.0,
            "net_pnl": 0.0,
        }
    wins = sum(1 for t in trades if t.get("result") == "win" or float(t.get("pnl") or 0) > 0)
    rs = []
    for t in trades:
        r = t.get("r_multiple")
        if r is not None:
            try:
                rs.append(float(r))
            except (TypeError, ValueError):
                pass
        elif t.get("pnl") is not None:
            rs.append(1.0 if float(t["pnl"]) > 0 else -1.0)
    avg_r = sum(rs) / len(rs) if rs else 0.0
    net = sum(float(t.get("pnl") or 0) for t in trades)
    n = len(trades)
    return {
        "n": n,
        "wins": wins,
        "win_rate_pct": 100.0 * wins / n,
        "avg_r": avg_r,
        "net_pnl": net,
    }


def _policy_fit_score(variant: dict[str, Any], trades: list[dict[str, Any]]) -> float:
    """Reward variants whose params resemble winning trade management profiles."""
    if not trades:
        return 0.0
    win_fit = 0.0
    loss_fit = 0.0
    win_n = 0
    loss_n = 0
    for trade in trades:
        mgmt = _management_from_trade(trade)
        dist = _param_distance(variant, mgmt)
        similarity = max(0.0, 1.0 - dist)
        is_win = trade.get("result") == "win" or float(trade.get("pnl") or 0) > 0
        if is_win:
            win_fit += similarity
            win_n += 1
        else:
            loss_fit += similarity
            loss_n += 1
    win_avg = win_fit / win_n if win_n else 0.0
    loss_avg = loss_fit / loss_n if loss_n else 0.0
    return (win_avg - loss_avg * 0.35) * 18.0


def _entry_fit_bonus(variant: dict[str, Any], session: str, trades: list[dict[str, Any]]) -> float:
    bias = session_entry_bias(session)
    preferred_entry = str(bias.get("entry_type") or "limit")
    bonus = 4.0 if variant.get("entry_type") == preferred_entry else -2.0
    if variant.get("entry_type") == "limit":
        pref_offset = float(bias.get("limit_offset_atr") or 0.1)
        diff = abs(float(variant.get("limit_offset_atr") or 0) - pref_offset)
        bonus += max(-6.0, 4.0 - diff * 40.0)
    if trades:
        limit_wins = sum(
            1 for t in trades
            if (t.get("management_profile") or {}).get("entry_type") == "limit"
            and (t.get("result") == "win" or float(t.get("pnl") or 0) > 0)
        )
        market_wins = sum(
            1 for t in trades
            if (t.get("management_profile") or {}).get("entry_type") == "market"
            and (t.get("result") == "win" or float(t.get("pnl") or 0) > 0)
        )
        if limit_wins > market_wins and variant.get("entry_type") == "limit":
            bonus += 3.0
        elif market_wins > limit_wins and variant.get("entry_type") == "market":
            bonus += 3.0
    return bonus


def score_variant(
    variant: dict[str, Any],
    *,
    symbol: str,
    setup_type: str,
    session: str,
    cell_trades: list[dict[str, Any]],
    feat: dict[str, Any] | None = None,
    signal: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[float, int]:
    """Return (score 0–100, sample_n) for one variant in a symbol/setup/session cell."""
    cfg = _detection_cfg(config or {})
    stats = _cell_trade_stats(cell_trades)
    sample_n = int(stats["n"])

    score = 50.0
    score += (float(stats["win_rate_pct"]) - 50.0) * 0.35
    score += float(stats["avg_r"]) * 12.0
    if float(stats["net_pnl"]) > 0:
        score += min(8.0, float(stats["net_pnl"]))
    elif float(stats["net_pnl"]) < -0.5:
        score -= 8.0

    score += _policy_fit_score(variant, cell_trades)
    score += _entry_fit_bonus(variant, session, cell_trades)
    score += float(session_entry_bias(session).get("session_weight") or 0) * 0.15

    if signal and feat:
        spread = feat.get("spread_points", feat.get("spread"))
        spread_f = float(spread) if spread is not None else None
        recent = recent_symbol_stats(cell_trades, symbol)
        ctx_score, _ = compute_policy_score(
            signal,
            feat,
            spread_points=spread_f,
            recent=recent,
            session_bias=session_entry_bias(session),
        )
        score += (ctx_score - 50.0) * 0.25

    if sample_n < cfg["min_sample_n"]:
        score -= cfg["insufficient_sample_penalty"] * (1.0 - sample_n / max(1, cfg["min_sample_n"]))

    return max(0.0, min(100.0, round(score, 2))), sample_n


def collect_cell_keys(
    trades: list[dict[str, Any]],
    evaluated: list[dict[str, Any]],
    *,
    config: dict[str, Any] | None = None,
) -> set[str]:
    """Keys to score: symbol|setup_type|session from recent trades + evaluated signals."""
    keys: set[str] = set()
    lookback = _detection_cfg(config or {})["trade_lookback"]
    for trade in trades[-lookback:]:
        sym = str(trade.get("symbol") or "")
        if not sym:
            continue
        keys.add(cell_key(sym, _trade_setup(trade), _trade_session(trade)))

    for signal in evaluated:
        sym = str(signal.get("symbol") or "")
        if not sym:
            continue
        setup = str(signal.get("setup_type") or "unknown")
        session = _signal_session(signal)
        keys.add(cell_key(sym, setup, session))

    return keys


def detect_policies(
    trades_doc: dict[str, Any],
    evaluated_doc: dict[str, Any],
    features_doc: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Score all policy variants per symbol/setup/session cell (shadow only)."""
    from core.utils import utc_now_iso

    trades = list(trades_doc.get("trades") or [])
    evaluated = list(evaluated_doc.get("evaluated") or [])
    feature_symbols = features_doc.get("symbols") or {}
    variants = build_policy_variants()
    keys = collect_cell_keys(trades, evaluated, config=config)

    trades_by_key: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        sym = str(trade.get("symbol") or "")
        if not sym:
            continue
        key = cell_key(sym, _trade_setup(trade), _trade_session(trade))
        trades_by_key.setdefault(key, []).append(trade)

    signals_by_key: dict[str, dict[str, Any]] = {}
    for signal in evaluated:
        sym = str(signal.get("symbol") or "")
        if not sym:
            continue
        key = cell_key(sym, str(signal.get("setup_type") or "unknown"), _signal_session(signal))
        signals_by_key[key] = signal

    scored: list[dict[str, Any]] = []
    best_by_key: dict[str, dict[str, Any]] = {}

    for key in sorted(keys):
        symbol, setup_type, session = parse_cell_key(key)
        cell_trades = trades_by_key.get(key, [])
        feat = feature_symbols.get(symbol) or {}
        signal = signals_by_key.get(key)
        cell_rows: list[dict[str, Any]] = []

        for variant in variants:
            score, sample_n = score_variant(
                variant,
                symbol=symbol,
                setup_type=setup_type,
                session=session,
                cell_trades=cell_trades,
                feat=feat,
                signal=signal,
                config=config,
            )
            row = {
                "policy_id": variant["policy_id"],
                "symbol": symbol,
                "setup_type": setup_type,
                "session": session,
                "entry_type": variant["entry_type"],
                "limit_offset_atr": variant["limit_offset_atr"],
                "sl_atr_mult": variant["sl_atr_mult"],
                "tp1_r": variant["tp1_r"],
                "be_trigger_r": variant["be_trigger_r"],
                "trail_start_r": variant["trail_start_r"],
                "score": score,
                "sample_n": sample_n,
            }
            cell_rows.append(row)
            scored.append(row)

        if cell_rows:
            cell_rows.sort(key=lambda r: (-float(r["score"]), -int(r["sample_n"])))
            best_by_key[key] = dict(cell_rows[0])

    scored.sort(key=lambda r: (-float(r["score"]), -int(r["sample_n"])))

    return {
        "timestamp": utc_now_iso(),
        "mode": policy_detection_mode(config),
        "variant_count": len(scored),
        "cell_count": len(keys),
        "variants": scored,
        "best_by_key": best_by_key,
        "source": "policy_detection_loop",
    }