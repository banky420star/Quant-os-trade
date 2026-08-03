"""Specialized ICT/SMC killzone setups — session-window entries sourced from
TradingView community indicators (2026-07-31).

Sources reviewed (TradingView + 2026 playbook):
  * Gold SMC Dashboard [CX Liquidity Hunter] — OB/FVG/Killzones/Liquidity Sweeps
  * Smart Money Gold Map [XAUUSD Kill Zones + Liquidity Sweeps]
  * XAU/USD Killzones — The Daily Playbook (2026 Edition) (Liquidity Hunters)

HONEST framing (read before enabling): per VERDICT.md there is **no deployable
edge at retail 30bps** across 6 edge families. Adding these setups does NOT
manufacture edge — ICT/SMC killzone concepts are a popular social framework,
not a proven edge at retail cost. The value here is **granularity**: new
``setup_type`` labels create new culturing cells
(symbol|setup|regime|align|session), so the data-driven veto + Thompson bandit +
setup-aggregate veto can distinguish **per-symbol** which specialized setups
work and which lose. That is exactly the user's "distinguish what wears per
symbol" direction.

Wiring: these are normal classifier candidates, **opt-in** via
``signals.specialized_setups.enabled`` (default false). When enabled they flow
through every existing verifier gate (kill_switch, exposure, data_driven_veto,
setup_aggregate_veto, news_sentiment, ...). If a specialized setup loses on a
symbol, the culturing ledger blocks it — same hygiene as the legacy setups.
Bot stays on demo; no live trading, no orders.

Adding a new specialized setup each iteration is a one-liner: append a
``SetupSpec`` to ``SPECIALIZED_SETUPS`` with a ``detect`` callable.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from core.session_scorer import utc_hour
from core.setup_triggers import trigger_params
from core.utils import append_archive_record, utc_now_iso

# Shadow fire-ledger (iteration 8). When shadow mode is on, every specialized
# setup that fires is appended here as one JSONL line under
# state/specialized_shadow_ledger.jsonl — regardless of whether `enabled` is on.
# This accumulates per-symbol fire-frequency + context evidence WITHOUT placing
# any orders and WITHOUT flipping `enabled` (respects the standing no-orders /
# no-live-trading / kill-switch-untouched constraints). Mirrors the existing
# shadow Thompson bandit + shadow news-sentiment pattern. Outcome-labeling
# (turning fires into "what works per symbol") is a follow-up offline pass that
# joins these fires to forward price history.
SHADOW_LEDGER_NAME = "specialized_shadow_ledger"

# Cached replay-evidence veto (iteration 12). {mtime, per_symbol}. The veto
# file is written by core/specialized_replay_veto.py from the replay report.
# Module-level cache with mtime check so the hot path does not re-read JSON per
# bar; refreshes when the file is rewritten.
_REPLAY_VETO_CACHE: dict[str, Any] = {"mtime": 0.0, "per_symbol": {}}


def _load_replay_veto() -> dict[str, list[str]]:
    """Read state/specialized_replay_veto.json per_symbol blocklist (cached).

    Returns {symbol: [blocked_setup, ...]}. Fault-isolated: any read/parse error
    -> empty veto (no blocking). The caller (specialized_config) only invokes
    this when apply_replay_veto is true.
    """
    import os
    from core.utils import STATE_DIR
    try:
        p = STATE_DIR / "specialized_replay_veto.json"
        if not p.exists():
            return {}
        mtime = p.stat().st_mtime
        if mtime == _REPLAY_VETO_CACHE["mtime"]:
            return _REPLAY_VETO_CACHE["per_symbol"]
        from core.utils import read_json_state
        doc = read_json_state("specialized_replay_veto.json", default={}) or {}
        per_symbol = doc.get("per_symbol") or {}
        if not isinstance(per_symbol, dict):
            per_symbol = {}
        clean = {
            str(sym): [str(s) for s in (setups or []) if isinstance(s, str)]
            for sym, setups in per_symbol.items()
        }
        _REPLAY_VETO_CACHE["mtime"] = mtime
        _REPLAY_VETO_CACHE["per_symbol"] = clean
        return clean
    except Exception:  # noqa: BLE001 — veto must never break detection
        return {}


def specialized_config(config: dict[str, Any]) -> dict[str, Any]:
    """Resolved config for the specialized-setups layer (all opt-in)."""
    sig = config.get("signals", {}) if isinstance(config.get("signals"), dict) else {}
    sa = sig.get("specialized_setups", {}) if isinstance(sig.get("specialized_setups"), dict) else {}
    return {
        "enabled": bool(sa.get("enabled", False)),
        # Per-symbol allowlist (empty = all configured symbols). Lets an
        # operator trial a setup on gold only before widening.
        "symbols": list(sa.get("symbols", []) or []),
        # Per-setup enable map (empty = all setups in SPECIALIZED_SETUPS). Lets
        # the loop progressively turn on setups as evidence accumulates.
        "setups": list(sa.get("setups", []) or []),
        # Per-symbol-per-setup granular map (iteration 6). Maps a symbol to the
        # setups allowed on THAT symbol only, e.g.
        #   per_symbol: {AUDUSDm: [sydney_open_orb], USDCHFm: [london_close_reversal]}
        # When a symbol is keyed here, only the listed setups may fire on it
        # (intersected with the global symbols/setups gates). Empty = no extra
        # restriction. This is the "very symbol specific" lever: each symbol
        # accumulates culturing evidence only on the setups trialed on it.
        "per_symbol": {
            str(k): list(v or [])
            for k, v in (sa.get("per_symbol", {}) or {}).items()
            if isinstance(v, (list, tuple))
        },
        # Shadow fire-ledger (iteration 8). When true, every fire is appended to
        # state/specialized_shadow_ledger.jsonl — independent of `enabled`. When
        # `enabled` is false, fires are logged but NOT emitted for trading (no
        # orders). When `enabled` is true, fires are logged AND traded. Default
        # off. Lets the system accumulate per-symbol fire evidence safely before
        # (or alongside) live demo trading.
        "shadow": bool(sa.get("shadow", False)),
        # 2026-08-01 iteration 12 — replay-evidence per-symbol veto. When true,
        # detect_specialized skips (symbol, setup) pairs that the historical-replay
        # labeler flagged as reliable losers (n>=8, expectancy<0, CI95 upper<=0).
        # The veto map is read from state/specialized_replay_veto.json (written by
        # core/specialized_replay_veto.py). Default OFF — operator opt-in. Even
        # when on, this only filters WHICH specialized setups fire; it places no
        # orders (gated by `enabled`). This is the "become very symbol specific"
        # lever: proven per-symbol losers are blocked per-symbol.
        "apply_replay_veto": bool(sa.get("apply_replay_veto", False)),
        "replay_veto": _load_replay_veto() if bool(sa.get("apply_replay_veto", False)) else {},
    }


def _shadow_log(fires: list[dict[str, Any]], feat: dict[str, Any],
                ctx: dict[str, Any]) -> None:
    """Append one JSONL record per fired specialized setup to the shadow ledger.

    Fault-isolated: never raises into the pipeline. Omits ``transaction_id`` so
    append_archive_record does not dedup (one record per fire per bar).
    """
    if not fires:
        return
    ts = utc_now_iso()
    symbol = feat.get("symbol") or ctx.get("symbol")
    feat_slice = {
        "price": feat.get("price"),
        "m5_trend": feat.get("m5_trend"),
        "breakout": feat.get("breakout"),
        "rejection": feat.get("rejection"),
        "bb_position": feat.get("bb_position"),
    }
    try:
        hour = int(utc_hour())
    except Exception:  # noqa: BLE001
        hour = -1
    for c in fires:
        rec = {
            "fired_at": ts,
            "utc_hour": hour,
            "symbol": symbol,
            "setup_type": c.get("setup_type"),
            "side": c.get("side"),
            "confidence": c.get("setup_confidence"),
            "reason": c.get("reason"),
            "feat": feat_slice,
        }
        try:
            append_archive_record(SHADOW_LEDGER_NAME, rec)
        except Exception:  # noqa: BLE001 — shadow logging must never break trading
            logging.getLogger("specialized_setups").warning(
                "specialized shadow ledger append failed", exc_info=True
            )


def _base(setup_type: str, side: str, confidence: float, reason: str) -> dict[str, Any]:
    """Match the SetupClassifier._base candidate shape exactly."""
    return {
        "setup_type": setup_type,
        "side": side,
        "setup_confidence": round(confidence, 2),
        "reason": reason,
    }


def _in_window(cfg_params: dict[str, Any]) -> bool:
    """True if the current UTC hour is inside [start, end)."""
    start = int(cfg_params.get("utc_hour_start", -1))
    end = int(cfg_params.get("utc_hour_end", -1))
    if start < 0 or end < 0:
        return True  # no window configured -> always eligible
    h = utc_hour()
    return start <= h < end


def _rejection_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                         reason_prefix: str) -> dict | None:
    """Shared rejection-candle detector for the ICT/SMC killzone setups.

    Keys off ``feat.rejection`` (bullish_rejection = sweep lows + reject up ->
    BUY; bearish_rejection = sweep highs + reject down -> SELL) inside a UTC
    window with liquidity >= min. Conf = avg(liquidity, structure). Mirrors
    ``_breakout_in_window`` so adding a new rejection-based killzone setup is a
    one-liner wrapper. Each setup's UTC window + label -> a distinct culturing
    cell, so the ledger distinguishes them independently per symbol.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    min_liq = float(p.get("min_liquidity", 0.60))
    liq = float(ev.get("liquidity", 0))
    if liq < min_liq:
        return None
    rej = feat.get("rejection")
    struct = float(ev.get("structure", 0))
    if rej == "bullish_rejection":
        return _base(setup_type, "BUY", (liq + struct) / 2,
                     f"{reason_prefix} — sweep of lows + bullish rejection (CHoCH)")
    if rej == "bearish_rejection":
        return _base(setup_type, "SELL", (liq + struct) / 2,
                     f"{reason_prefix} — sweep of highs + bearish rejection (CHoCH)")
    return None


def _bb_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                  reason_prefix: str) -> dict | None:
    """Shared Bollinger-Band mean-reversion detector, session-gated (iteration 7).

    Keys off ``feat.bb_position`` (>= bb_upper -> overstretched up -> SELL;
    <= bb_lower -> overstretched down -> BUY) inside a UTC window. Conf =
    avg(structure, liquidity). This is a THIRD entry model alongside
    ``_rejection_in_window`` (rejection) and ``_breakout_in_window`` (breakout),
    so a session-gated BB reversion lands in a distinct culturing cell from both
    the legacy anytime mean_reversion and the rejection/breakout killzone setups
    — the ledger can distinguish per symbol whether BB reversion works in a given
    session. Adding a new BB-reversion setup is a one-liner wrapper.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    bb_upper = float(p.get("bb_upper", 0.85))
    bb_lower = float(p.get("bb_lower", 0.15))
    bb = float(feat.get("bb_position", 0.5))
    struct = float(ev.get("structure", 0))
    liq = float(ev.get("liquidity", 0))
    if bb >= bb_upper:
        return _base(setup_type, "SELL", (struct + liq) / 2,
                     f"{reason_prefix} — overstretched at upper band")
    if bb <= bb_lower:
        return _base(setup_type, "BUY", (struct + liq) / 2,
                     f"{reason_prefix} — overstretched at lower band")
    return None


def _silver_bullet(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """ICT Silver Bullet — 14:00-15:00 UTC session-liquidity sweep + CHoCH.

    Proxy: a rejection candle in the Silver Bullet window with liquidity >= min.
    bullish_rejection (sweep lows, reject up) -> BUY; bearish_rejection -> SELL.
    CHoCH is approximated by the rejection feature (the bot has no explicit
    market-structure detector); the culturing ledger will tell us per-symbol
    whether this proxy is predictive.
    """
    return _rejection_in_window("silver_bullet", feat, ev, cfg,
                                "Silver Bullet — 14:00-15:00 UTC")


def _london_judas(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London Open Judas Swing — 07:00-10:00 UTC manipulation reversal.

    The Judas Swing is a fake move at the London open that sweeps the Asian
    session range then reverses. Proxy: a rejection candle in the London
    killzone with liquidity >= min — same proxy as Silver Bullet but a different
    session window, so it lands in a DIFFERENT culturing cell and the ledger can
    distinguish the two independently per symbol.
    """
    return _rejection_in_window("london_judas", feat, ev, cfg,
                                "London Judas — 07:00-10:00 UTC")


# --- iteration 2: symbol-specific breakout setups (breakout-based) ---

def _breakout_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                        reason_prefix: str) -> dict | None:
    """Shared breakout-state detector for the iteration-2 setups.

    Both oil_orb and asia_range_breakout key off ``feat.breakout`` (a different
    entry model than the iteration-1 rejection-based ICT setups) and differ only
    in their UTC window + setup label -> distinct culturing cells per symbol.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    states = tuple(p.get("breakout_states", ("breakout", "breakdown")))
    bo = feat.get("breakout")
    if bo not in states:
        return None
    struct = float(ev.get("structure", 0))
    vol = float(ev.get("volume", 0))
    # 3-evidence confidence for the momentum-driven breakouts (oil ORB + US
    # index ORB + prev-day breakout + EU ORB + Tokyo ORB); 2-evidence for the
    # session-range fade (Asian range breakout).
    if setup_type in ("oil_orb", "us_open_orb", "prev_day_breakout", "eu_open_orb", "tokyo_open_orb", "sydney_open_orb", "london_morning_breakout"):
        mom = float(ev.get("momentum", 0))
        conf = (struct + vol + mom) / 3
    else:
        conf = (struct + vol) / 2
    side = "BUY" if bo == "breakout" else "SELL"
    return _base(setup_type, side, conf, f"{reason_prefix} — {bo} of session range")


def _oil_orb(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """Crude oil opening-range breakout — 14:00-19:00 UTC NY execution window.

    Source: Kane's Oil ORB (TradingView) + NexusFi CL playbook. 9:00 AM ET ORB
    = 14:00 UTC; primary execution 10:30 AM-2:00 PM ET. Oil-specific — trial via
    signals.specialized_setups.symbols: ["USOILm"].
    """
    return _breakout_in_window("oil_orb", feat, ev, cfg, "Oil ORB — 14:00-19:00 UTC")


def _asia_range_breakout(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """Asian range breakout at London open — 07:00-10:00 UTC.

    Source: Asian Range Sweep scanners (LiquidityScan, TL Asia Session Sweep
    Pro). Asia builds range 00:00-04:00 UTC; London open breaks it. FX/crypto —
    trial via symbols: ["EURUSDm","GBPUSDm","BTCUSDm", ...].
    """
    return _breakout_in_window("asia_range_breakout", feat, ev, cfg,
                               "Asian range breakout — 07:00-10:00 UTC London open")


# --- iteration 3: US equity-index specialized setups ---

def _us_open_orb(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """US index RTH opening-range breakout — 14:00-16:00 UTC (9:30 AM ET).

    Source: TradingView 9:30 Breakout (kaiserfx_), ORB - Futures and Stocks
    (tolosatrader), Fazen Capital + Vortex Capital 60-day ORB research. NAS100
    favors 5-15 min OR (explosive opens), US500 favors 15-30 min. Trial via
    symbols: ["US500m","US30m","NAS100m"].
    """
    return _breakout_in_window("us_open_orb", feat, ev, cfg,
                               "US RTH open ORB — 14:00-16:00 UTC (9:30 AM ET)")


def _prev_day_breakout(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """Previous-day high/low breakout during US RTH — 14:00-21:00 UTC.

    Source: TradingView "US 30 Daily Breakout Strategy" (yavanmahur) — long on
    close above prev day high, short below prev day low, one per day, NY reset.
    Index-specific — trial via symbols: ["US30m","US500m","NAS100m"].
    """
    return _breakout_in_window("prev_day_breakout", feat, ev, cfg,
                               "Prev day H/L breakout — 14:00-21:00 UTC US RTH")


# --- iteration 4: EU + Tokyo open gaps ---

def _eu_open_orb(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """EU index opening-range breakout — 08:00-10:00 UTC (London-Frankfurt open).

    Source: TradingView Europe Open Levels PRO (markus_schneider7), Xetra
    Auctions Breakout (ovvo_113), Fazen DAX 08:00-10:00 GMT. FXVPS 14-year DAX
    ORB study found a REAL but THIN edge (PF 1.25, Sharpe 1.46, ~0.1R/trade,
    slippage-sensitive). NOTE: FR40m is CAC not DAX — edge may not transfer;
    culturing measures it per symbol. Trial via symbols: ["UK100m","FR40m"].
    """
    return _breakout_in_window("eu_open_orb", feat, ev, cfg,
                               "EU open ORB — 08:00-10:00 UTC London-Frankfurt")


def _tokyo_open_orb(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """Tokyo session opening-range breakout — 00:00-02:00 UTC (first hour).

    Source: TradingView USDJPY FVG+Session (Stockhock), Liquidity Sweep Breakout
    (humayunmha), FibAlgo USDJPY session framework. Tokyo first hour is highest
    JPY volatility. BOJ intervention awareness (reduced size near 145/150/155).
    Trial via symbols: ["USDJPYm","JP225m"].
    """
    return _breakout_in_window("tokyo_open_orb", feat, ev, cfg,
                               "Tokyo open ORB — 00:00-02:00 UTC first hour")


# --- iteration 5: London close reversal + Sydney open ORB (close the
# USDCHFm / AUDUSDm coverage gap) ---

def _london_close_reversal(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London Close killzone reversal — 15:00-17:00 UTC.

    Source: ICT London Close killzone + TradingView London Close Reversal
    scanners (London Close Killzone, ICT Killzones). The London close (16:00- 17:00 UTC) produces liquidity sweeps and reversals as European books roll off;
    a rejection candle in this window is the proxy. Rejection-based (via
    ``_rejection_in_window``), distinct UTC window + label -> distinct culturing
    cell. FX majors + gold — fills the USDCHFm gap (CHF is most liquid in the
    London session). Trial via symbols: ["USDCHFm","EURUSDm","GBPUSDm","XAUUSDm"].
    """
    return _rejection_in_window("london_close_reversal", feat, ev, cfg,
                                "London Close reversal — 15:00-17:00 UTC")


def _sydney_open_orb(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """Sydney session opening-range breakout — 21:00-23:00 UTC (Asia week-open).

    Source: TradingView Sydney/Tokyo session ORB scanners + AUDUSD session
    frameworks (FX Synergy Session Times, Sydney Open indicators). Sydney is the
    first major FX centre to open each trading day (~21:00 UTC / 10 PM London);
    the AUD leg is most volatile in this window. Breakout-based (via
    ``_breakout_in_window``, 3-evidence conf). Fills the AUDUSDm gap. Trial via
    symbols: ["AUDUSDm"]. NOTE: thin liquidity pre-Tokyo -> wider spreads on
    retail Exness; the culturing ledger will surface whether cost eats the edge
    per symbol.
    """
    return _breakout_in_window("sydney_open_orb", feat, ev, cfg,
                               "Sydney open ORB — 21:00-23:00 UTC Asia week-open")


# --- iteration 6: fill the remaining uncovered UTC windows
# (10:00-14:00 London morning, 17:00-19:00 NY lunch) + per-symbol allowlist ---

def _london_morning_breakout(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London morning continuation breakout — 10:00-14:00 UTC.

    Source: TradingView London Session Breakout strategies (London Breakout
    Indicator, Forex Session Breakout). After the 07:00-10:00 London-open noise
    + Judas manipulation settles, the 10:00-14:00 window trends into the NY
    open. Breakout-based (via ``_breakout_in_window``, 3-evidence conf). Fills
    the 10:00-14:00 UTC dead zone (no setup covered it before). FX majors + gold
    + EU indices. Trial via symbols: ["EURUSDm","GBPUSDm","USDCHFm","XAUUSDm"].
    """
    return _breakout_in_window("london_morning_breakout", feat, ev, cfg,
                               "London morning breakout — 10:00-14:00 UTC")


def _ny_lunch_reversal(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """NY lunch lull reversal — 17:00-19:00 UTC (12:00-14:00 ET).

    Source: ICT NY AM/PM session model + TradingView NY Lunch Reversal / NY Mid
    scanners. The 12:00-14:00 ET lunch lull is low volatility; extremes made in
    the NY morning often sweep + revert. Rejection-based (via
    ``_rejection_in_window``). Fills the 17:00-19:00 UTC dead zone. FX majors +
    US indices. Trial via symbols: ["EURUSDm","GBPUSDm","US30m","US500m"].
    """
    return _rejection_in_window("ny_lunch_reversal", feat, ev, cfg,
                                "NY lunch reversal — 17:00-19:00 UTC")


# --- iteration 7: session-gated Bollinger-Band mean reversion (third entry
# model, uses feat.bb_position — exhausts the bot's available feature proxies) ---

def _london_bb_reversion(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session BB mean reversion — 07:00-10:00 UTC.

    Source: TradingView Bollinger Band mean-reversion scalpers + London session
    BB strategies (BB Squeeze London, London BB Reversion). Fade an overstretched
    BB extreme inside the London killzone. BB-based (via ``_bb_in_window``) — a
    different entry model from london_judas (rejection) and asia_range_breakout
    (breakout) in the same window, so it lands in its own culturing cell. For FX
    majors + gold. Trial via symbols: ["EURUSDm","GBPUSDm","USDCHFm","XAUUSDm"].
    """
    return _bb_in_window("london_bb_reversion", feat, ev, cfg,
                         "London BB reversion — 07:00-10:00 UTC")


def _ny_bb_reversion(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session BB mean reversion — 14:00-17:00 UTC.

    Source: TradingView NY BB reversion + Bollinger Band scalping strategies
    (NY Session BB, BB RSI NY). Fade an overstretched BB extreme during US RTH.
    BB-based (via ``_bb_in_window``). Distinct entry model + window -> distinct
    culturing cell. For FX majors + US indices. Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m"].
    """
    return _bb_in_window("ny_bb_reversion", feat, ev, cfg,
                         "NY BB reversion — 14:00-17:00 UTC")


# --- iteration 12: stochastic-cross momentum setups (FOURTH entry model, uses
# feat.stoch_cross — previously unused by any specialized setup, so this is a
# genuinely new entry model, not a relabel of rejection/breakout/BB). ---

def _stoch_cross_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                           reason_prefix: str) -> dict | None:
    """Shared stochastic-cross momentum detector, session-gated (iteration 12).

    Keys off ``feat.stoch_cross`` (bullish_cross = %K crosses above %D ->
    momentum-up -> BUY; bearish_cross = %K crosses below %D -> SELL) inside a UTC
    window with momentum >= min. Conf = avg(momentum, structure). This is a
    FOURTH entry model alongside ``_rejection_in_window`` (rejection),
    ``_breakout_in_window`` (breakout), and ``_bb_in_window`` (BB) — it uses a
    feature none of those read, so a session-gated stoch cross lands in its own
    culturing cell and the ledger can distinguish per symbol whether momentum-
    cross continuation works in a given session. Adding a new stoch-cross setup
    is a one-liner wrapper.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    min_mom = float(p.get("min_momentum", 0.50))
    mom = float(ev.get("momentum", 0))
    if mom < min_mom:
        return None
    cross = feat.get("stoch_cross")
    struct = float(ev.get("structure", 0))
    if cross == "bullish_cross":
        return _base(setup_type, "BUY", (mom + struct) / 2,
                     f"{reason_prefix} — bullish %K/%D cross (momentum continuation)")
    if cross == "bearish_cross":
        return _base(setup_type, "SELL", (mom + struct) / 2,
                     f"{reason_prefix} — bearish %K/%D cross (momentum continuation)")
    return None


def _macd_cross_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                          reason_prefix: str) -> dict | None:
    """Shared MACD signal-line-cross momentum detector, session-gated (iteration 22).

    Keys off ``feat.macd_cross`` (bullish_cross = MACD line crosses above its
    signal line -> momentum building up -> BUY; bearish_cross = MACD crosses
    below signal -> SELL) inside a UTC window with momentum >= min. Conf =
    avg(momentum, structure). This is a THIRTEENTH entry model: MACD is a
    SMOOTHED-MOMENTUM oscillator (EMA12-EMA26, a fast/slow EMA difference) —
    mathematically distinct from stoch_cross (a price-RANGE oscillator cross,
    iter 12), from the EMA20-slope m5_trend (single-EMA slope, not a fast/slow
    difference), and from RSI (momentum-ratio, iter 21). So a session-gated MACD
    cross lands in its own culturing cell and the ledger can distinguish per
    symbol whether a smoothed-momentum cross works in a given session (vs the
    price-range stoch cross). Adding a new MACD-cross setup is a one-liner
    wrapper. Standard TradingView MACD(12,26,9).
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    min_mom = float(p.get("min_momentum", 0.50))
    mom = float(ev.get("momentum", 0))
    if mom < min_mom:
        return None
    cross = feat.get("macd_cross")
    struct = float(ev.get("structure", 0))
    if cross == "bullish_cross":
        return _base(setup_type, "BUY", (mom + struct) / 2,
                     f"{reason_prefix} — bullish MACD/signal cross (momentum continuation)")
    if cross == "bearish_cross":
        return _base(setup_type, "SELL", (mom + struct) / 2,
                     f"{reason_prefix} — bearish MACD/signal cross (momentum continuation)")
    return None


def _london_stoch_cross(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session stochastic-cross momentum continuation — 07:00-10:00 UTC.

    Source: TradingView Stochastic crossover strategies (Stochastic Oscillator
    Strategies, Forex Stoch Cross). A %K/%D cross inside the London killzone is a
    momentum-continuation trigger (distinct from the rejection/breakout/BB setups
    in the same window -> distinct culturing cell). FX majors + gold. Trial via
    symbols: ["EURUSDm","GBPUSDm","USDCHFm","XAUUSDm"].
    """
    return _stoch_cross_in_window("london_stoch_cross", feat, ev, cfg,
                                  "London stoch cross — 07:00-10:00 UTC")


def _ny_stoch_cross(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session stochastic-cross momentum continuation — 14:00-17:00 UTC.

    Source: TradingView NY stoch-cross / RSI-Stoch combo strategies (NY Session
    Stochastic, Stoch RSI NY). A %K/%D cross during US RTH. Stoch-based (via
    ``_stoch_cross_in_window``). Distinct entry model + window -> distinct
    culturing cell. FX majors + US indices. Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m"].
    """
    return _stoch_cross_in_window("ny_stoch_cross", feat, ev, cfg,
                                   "NY stoch cross — 14:00-17:00 UTC")


# --- iteration 13: volatility-expansion momentum setups (FIFTH entry model,
# uses feat.volatility_regime — previously unused as a primary key by any
# specialized setup; keys off regime flip to "high" + trend direction). ---

def _vol_expansion_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                             reason_prefix: str) -> dict | None:
    """Shared volatility-expansion momentum detector, session-gated (iteration 13).

    Keys off ``feat.volatility_regime == "high"`` (ATR/price ratio above the
    high-regime threshold, OR 20-bar pct-change std elevated) combined with the M5
    trend direction — a momentum-continuation entry on a volatility expansion.
    This is a FIFTH entry model alongside rejection / breakout / BB / stoch-cross:
    it uses ``volatility_regime`` (unused as a primary key by any prior specialized
    setup) + ``m5_trend`` for direction, so it lands in its own culturing cell and
    the ledger can distinguish per symbol whether vol-expansion continuation works
    in a given session. Conf = avg(volatility, momentum). min_volatility gate.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    # min_volatility 0.50 (not 0.60): EvidenceEngine._volatility_evidence maps the
    # "high" regime to 0.55 (normal=0.65, low=0.35), so a 0.60 gate made the
    # regime==high requirement and the volatility gate mutually exclusive — the
    # setup could never fire. 0.50 lets high-regime (0.55) through; normal-regime
    # (0.65) is already blocked by the volatility_regime=="high" check above; low
    # (0.35) fails both. The gate still filters dead-flat low-vol bars.
    min_vol = float(p.get("min_volatility", 0.50))
    vol = float(ev.get("volatility", 0))
    if vol < min_vol:
        return None
    if feat.get("volatility_regime") != "high":
        return None
    trend = feat.get("m5_trend")
    mom = float(ev.get("momentum", 0))
    if trend == "bullish":
        return _base(setup_type, "BUY", (vol + mom) / 2,
                     f"{reason_prefix} — vol expansion + bullish M5 trend (momentum continuation)")
    if trend == "bearish":
        return _base(setup_type, "SELL", (vol + mom) / 2,
                     f"{reason_prefix} — vol expansion + bearish M5 trend (momentum continuation)")
    return None


def _volume_spike_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                            reason_prefix: str) -> dict | None:
    """Shared volume-spike confirmation detector, session-gated (iteration 14).

    Keys off ``feat.volume_ratio >= min_volume_ratio`` (current bar volume vs its
    rolling average) combined with the M5 trend direction — a momentum-continuation
    entry on a volume spike. This is a SIXTH entry model alongside rejection /
    breakout / BB / stoch-cross / vol-expansion: it uses ``volume_ratio`` as a
    PRIMARY KEY (previously unused by any specialized setup — volume only entered
    the EvidenceEngine score, never a trigger). So it lands in its own culturing
    cell and the ledger can distinguish per symbol whether volume-spike
    continuation works in a given session. Conf = avg(volume, momentum).

    HONEST: on Exness CFDs ``volume`` is broker tick-volume (an activity proxy,
    not true traded volume) — it correlates with volatility/participation but is
    not real flow. That is exactly why this needs the replay to measure it per
    symbol rather than assume an edge.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    min_vr = float(p.get("min_volume_ratio", 1.5))
    vr = float(feat.get("volume_ratio") or 0)
    if vr < min_vr:
        return None
    trend = feat.get("m5_trend")
    vol_ev = float(ev.get("volume", 0))
    mom = float(ev.get("momentum", 0))
    if trend == "bullish":
        return _base(setup_type, "BUY", (vol_ev + mom) / 2,
                    f"{reason_prefix} — volume spike {vr:.1f}x avg + bullish M5 trend")
    if trend == "bearish":
        return _base(setup_type, "SELL", (vol_ev + mom) / 2,
                    f"{reason_prefix} — volume spike {vr:.1f}x avg + bearish M5 trend")
    return None


def _mtf_align_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                         reason_prefix: str) -> dict | None:
    """Shared multi-timeframe-alignment continuation detector, session-gated
    (iteration 15).

    Keys off ``feat.timeframe_alignment == True`` (M5 EMA20-slope trend ==
    M15 EMA20-slope trend) combined with the shared M5/M15 trend direction — a
    momentum-continuation entry on multi-timeframe confluence. This is a SEVENTH
    entry model alongside rejection / breakout / BB / stoch-cross / vol-expansion
    / volume-spike: it uses ``timeframe_alignment`` (the m5_trend==m15_trend
    boolean) as a PRIMARY KEY — previously unused by any specialized setup (m15_trend
    only fed the regime/context, never a setup trigger). So it lands in its own
    culturing cell and the ledger can distinguish per symbol whether MTF-alignment
    continuation works in a given session. Conf = avg(trend, momentum).
    min_trend gate filters weak/flat aligned trends.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    if not feat.get("timeframe_alignment"):
        return None
    trend = feat.get("m5_trend")
    min_tr = float(p.get("min_trend", 0.50))
    tr_ev = float(ev.get("trend", 0))
    if tr_ev < min_tr:
        return None
    mom = float(ev.get("momentum", 0))
    if trend == "bullish":
        return _base(setup_type, "BUY", (tr_ev + mom) / 2,
                    f"{reason_prefix} — M5+M15 aligned bullish (multi-timeframe continuation)")
    if trend == "bearish":
        return _base(setup_type, "SELL", (tr_ev + mom) / 2,
                    f"{reason_prefix} — M5+M15 aligned bearish (multi-timeframe continuation)")
    return None


def _stoch_reversion_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                                reason_prefix: str) -> dict | None:
    """Shared stochastic oversold/overbought mean-reversion detector, session-gated
    (iteration 16).

    Keys off ``feat.stoch_k`` (the raw %K level — NOT the cross event) at an
    extreme: stoch_k <= oversold (default 20) -> BUY reversion up; stoch_k >=
    overbought (default 80) -> SELL reversion down. This is an EIGHTH entry model
    alongside rejection / breakout / BB / stoch-cross / vol-expansion /
    volume-spike / MTF-align: it uses ``stoch_k`` (the raw oscillator level) as a
    PRIMARY KEY — previously unused (stoch_cross keyed on the cross EVENT, not
    the level). It is a MEAN-REVERSION entry from a stoch extreme, distinct from
    stoch_cross's momentum CONTINUATION, so it lands in its own culturing cell
    and the ledger can distinguish per symbol whether stoch-extreme reversion
    works in a given session. Conf = avg(structure, liquidity) (reversion-shaped,
    same as BB-reversion).
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    k = float(feat.get("stoch_k") if feat.get("stoch_k") is not None else 50)
    oversold = float(p.get("oversold", 20))
    overbought = float(p.get("overbought", 80))
    struct = float(ev.get("structure", 0))
    liq = float(ev.get("liquidity", 0))
    if k <= oversold:
        return _base(setup_type, "BUY", (struct + liq) / 2,
                    f"{reason_prefix} — stoch %K {k:.0f} oversold (mean reversion up)")
    if k >= overbought:
        return _base(setup_type, "SELL", (struct + liq) / 2,
                    f"{reason_prefix} — stoch %K {k:.0f} overbought (mean reversion down)")
    return None


def _htf_breakout_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                            reason_prefix: str) -> dict | None:
    """Shared higher-timeframe-trend-filtered breakout detector, session-gated
    (iteration 18).

    Keys off ``feat.m15_trend`` DIRECTION (the M15 EMA20-slope trend — bullish /
    bearish) as a higher-timeframe bias filter, combined with a ``feat.breakout``
    state ALIGNED to that M15 direction: M15 bullish + breakout (above
    resistance) -> BUY; M15 bearish + breakdown (below support) -> SELL. This is
    a NINTH entry model alongside rejection / breakout / BB / stoch-cross /
    vol-expansion / volume-spike / MTF-align / stoch-reversion: it uses
    ``m15_trend`` DIRECTION as a PRIMARY KEY — previously unused (timeframe_alignment
    uses the m5==m15 boolean MATCH, not the m15 direction alone; the plain
    breakout setups don't filter by M15 trend at all). So it lands in its own
    culturing cell and the ledger can distinguish per symbol whether
    HTF-trend-filtered breakouts beat unfiltered breakouts. Conf = avg(trend,
    structure, volume). Requires the breakout state to actually fire (it does
    after the iteration-17 S/R off-by-one fix; before that fix this was dead).
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    m15 = feat.get("m15_trend")
    bo = feat.get("breakout")
    tr_ev = float(ev.get("trend", 0))
    struct = float(ev.get("structure", 0))
    vol = float(ev.get("volume", 0))
    if m15 == "bullish" and bo == "breakout":
        return _base(setup_type, "BUY", (tr_ev + struct + vol) / 3,
                    f"{reason_prefix} — M15 bullish + breakout above resistance (HTF-filtered)")
    if m15 == "bearish" and bo == "breakdown":
        return _base(setup_type, "SELL", (tr_ev + struct + vol) / 3,
                    f"{reason_prefix} — M15 bearish + breakdown below support (HTF-filtered)")
    return None


def _squeeze_breakout_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                                reason_prefix: str) -> dict | None:
    """Shared Bollinger-bandwidth squeeze-release breakout detector, session-gated
    (iteration 19).

    Keys off ``feat.bb_squeeze_pct`` — the percentile rank of the CURRENT BB
    bandwidth within the prior 50 bars (0 = tightest compression, 1 = widest) —
    as a PRIMARY KEY. This is a TENTH entry model: ``bb_squeeze_pct`` is a NEW
    feature exposed this iteration (``FeatureEngine._bb_squeeze`` + the replay
    vectorized pass, parity-identical) — no prior setup used a rolling-compression
    baseline. ``volatility_regime == "low"`` is an ABSOLUTE atr_ratio threshold
    and is far too rare to fire (~0.06% of bars per iter-17 sampling); the
    squeeze percentile is RELATIVE / per-symbol-adaptive and fires often enough
    to measure. Source: TradingView TTM Squeeze / Bollinger Band Squeeze
    strategies — enter on the breakout that releases a compressed-volatility
    coil. Requires ``feat.bb_squeeze_pct <= squeeze_threshold`` (default 0.20 =
    current bandwidth in the tightest 20% of the last 50 bars) AND a
    ``breakout``/``breakdown`` state: breakout -> BUY, breakdown -> SELL (the
    direction the coil releases). Conf = avg(structure, volume, momentum).
    Requires the breakout state to fire (it does after the iteration-17 S/R fix).
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    sq = float(feat.get("bb_squeeze_pct") if feat.get("bb_squeeze_pct") is not None else 0.5)
    max_sq = float(p.get("max_squeeze_pct", 0.20))
    if sq > max_sq:
        return None  # not compressed enough -> no squeeze
    bo = feat.get("breakout")
    struct = float(ev.get("structure", 0))
    vol = float(ev.get("volume", 0))
    mom = float(ev.get("momentum", 0))
    if bo == "breakout":
        return _base(setup_type, "BUY", (struct + vol + mom) / 3,
                    f"{reason_prefix} — BB squeeze {sq:.0%} pct + breakout (squeeze release up)")
    if bo == "breakdown":
        return _base(setup_type, "SELL", (struct + vol + mom) / 3,
                    f"{reason_prefix} — BB squeeze {sq:.0%} pct + breakdown (squeeze release down)")
    return None


def _volume_breakout_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                               reason_prefix: str) -> dict | None:
    """Shared volume-confirmed breakout detector, session-gated (iteration 20).

    Keys off ``feat.volume_ratio >= min_volume_ratio`` AND a ``breakout``/
    ``breakdown`` state — a structural break CONFIRMED by above-average volume.
    This is an ELEVENTH entry model: the genuinely-unused COMPOUND is
    ``volume_ratio`` + ``breakout`` state. ``volume_spike`` (iter 14) uses
    ``volume_ratio`` + ``m5_trend`` (direction from TREND); the plain breakout
    setups use the breakout state with NO volume gate; this uses volume_ratio +
    breakout STATE (direction from the STRUCTURAL BREAK). So it lands in its own
    culturing cell and the ledger distinguishes per symbol whether volume
    confirmation improves structural breakouts vs unfiltered breakouts. Conf =
    avg(structure, volume, momentum). Source: TradingView Volume Breakout /
    Volume-Confirmed Breakout strategies.

    HONEST: on Exness CFDs ``volume`` is broker tick-volume (an activity proxy,
    not true traded volume) — the replay must measure it per symbol rather than
    assume edge. ``volume_ratio`` and ``breakout`` are both already threaded
    through the replay feat_dict (parity-identical to live), so no feature-engine
    work is needed — and unlike the iter-19 squeeze (squeeze∩breakout∩3h window,
    too thin), the breakout state is common enough to reach n>=8 per cell.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    min_vr = float(p.get("min_volume_ratio", 1.5))
    vr = float(feat.get("volume_ratio") or 0)
    if vr < min_vr:
        return None
    bo = feat.get("breakout")
    struct = float(ev.get("structure", 0))
    vol = float(ev.get("volume", 0))
    mom = float(ev.get("momentum", 0))
    if bo == "breakout":
        return _base(setup_type, "BUY", (struct + vol + mom) / 3,
                    f"{reason_prefix} — volume {vr:.1f}x avg + breakout above resistance")
    if bo == "breakdown":
        return _base(setup_type, "SELL", (struct + vol + mom) / 3,
                    f"{reason_prefix} — volume {vr:.1f}x avg + breakdown below support")
    return None


def _rsi_reversion_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                             reason_prefix: str) -> dict | None:
    """Shared RSI oversold/overbought mean-reversion detector, session-gated
    (iteration 21).

    Keys off ``feat.rsi`` (Wilder's RSI(14), a NEW feature exposed this
    iteration) RAW LEVEL as a primary key. This is a TWELFTH entry model: RSI is
    mathematically DISTINCT from stoch_k (RSI is a smoothed momentum-ratio
    oscillator; stoch_k is a price-range position oscillator) — so
    london_rsi_reversion / ny_rsi_reversion land in their own culturing cells,
    distinct from london_stoch_reversion / ny_stoch_reversion (iter 16). That
    matters because stoch_reversion was a BROAD per-symbol LOSER (iter 16 —
    "stoch stays overbought in a trend" trap); RSI reversion is a clean test of
    whether a DIFFERENT oscillator avoids that trap on a per-symbol basis.
    Source: TradingView RSI strategies (RSI Overbought/Oversold, RSI 30/70
    reversal). MEAN-REVERSION entry: rsi <= oversold (30) -> BUY (fade up),
    rsi >= overbought (70) -> SELL (fade down). Conf = avg(structure, liquidity)
    (reversion-shaped, same as BB/stoch reversion). Classic 30/70 thresholds
    (tunable per config; stoch_reversion used 20/80 — RSI uses the standard 30/70).
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    rsi = float(feat.get("rsi") if feat.get("rsi") is not None else 50.0)
    oversold = float(p.get("oversold", 30))
    overbought = float(p.get("overbought", 70))
    struct = float(ev.get("structure", 0))
    liq = float(ev.get("liquidity", 0))
    if rsi <= oversold:
        return _base(setup_type, "BUY", (struct + liq) / 2,
                    f"{reason_prefix} — RSI {rsi:.0f} <= {oversold:.0f} oversold (fade up to mean)")
    if rsi >= overbought:
        return _base(setup_type, "SELL", (struct + liq) / 2,
                    f"{reason_prefix} — RSI {rsi:.0f} >= {overbought:.0f} overbought (fade down to mean)")
    return None


def _cci_reversion_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                             reason_prefix: str) -> dict | None:
    """Shared CCI oversold/overbought mean-reversion detector, session-gated
    (iteration 23).

    Keys off ``feat.cci`` (Commodity Channel Index(20), a NEW feature exposed
    this iteration) RAW LEVEL as a primary key. This is a FOURTEENTH entry
    model: CCI is a PRICE-DEVIATION-FROM-MA oscillator (how far price sits from
    its own SMA, in units of mean deviation) — mathematically DISTINCT from rsi
    (gain/loss momentum ratio, iter 21), stoch_k (price-range position, iter 16),
    and MACD (EMA difference, iter 22). So london_cci_reversion /
    ny_cci_reversion land in their own culturing cells, distinct from the
    RSI/stoch/BB reversion cells. That matters because stoch_reversion was a
    broad loser (iter 16) and RSI reversion was more selective (iter 21) — CCI
    reversion is a clean per-symbol test of whether a price-deviation
    oscillator's extreme-fade works where the momentum-ratio (RSI) and
    price-range (stoch) fades didn't. Source: TradingView CCI strategies
    (Commodity Channel Index, CCI 100/-100 reversal). MEAN-REVERSION entry:
    cci <= oversold (-100) -> BUY (fade up), cci >= overbought (+100) -> SELL
    (fade down). Conf = avg(structure, liquidity) (reversion-shaped, same as
    RSI/stoch/BB reversion). Classic +/-100 thresholds (tunable; distinct scale
    from RSI 30/70 and stoch 20/80 — a different oscillator's thresholds).
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    cci = float(feat.get("cci") if feat.get("cci") is not None else 0.0)
    oversold = float(p.get("oversold", -100))
    overbought = float(p.get("overbought", 100))
    struct = float(ev.get("structure", 0))
    liq = float(ev.get("liquidity", 0))
    if cci <= oversold:
        return _base(setup_type, "BUY", (struct + liq) / 2,
                     f"{reason_prefix} — CCI {cci:.0f} <= {oversold:.0f} oversold (fade up to mean)")
    if cci >= overbought:
        return _base(setup_type, "SELL", (struct + liq) / 2,
                     f"{reason_prefix} — CCI {cci:.0f} >= {overbought:.0f} overbought (fade down to mean)")
    return None


def _mfi_reversion_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                             reason_prefix: str) -> dict | None:
    """Shared MFI oversold/overbought mean-reversion detector, session-gated
    (iteration 24).

    Keys off ``feat.mfi`` (Money Flow Index(14), a NEW feature exposed this
    iteration) RAW LEVEL as a primary key. This is a FIFTEENTH entry model: MFI
    is a VOLUME-WEIGHTED oscillator (money-flow ratio = sum(TP*volume on up
    bars) / sum(TP*volume on down bars), MFI = 100-100/(1+ratio)) — the ONLY
    oscillator here that folds in volume. Mathematically DISTINCT from rsi
    (gain/loss momentum ratio, iter 21, NO volume), cci (price-deviation from
    MA, iter 23, NO volume), stoch_k (price-range position, iter 16, NO
    volume), and MACD (EMA difference, iter 22, NO volume). So
    london_mfi_reversion / ny_mfi_reversion land in their own culturing cells,
    distinct from the RSI/CCI/stoch/BB reversion cells. That matters because the
    pure-price oscillator fades (RSI iter 21, CCI iter 23, stoch iter 16) split
    per-symbol without a clear volume dimension — MFI reversion is a clean test
    of whether a VOLUME-WEIGHTED fade distinguishes volume-informative symbols
    (crypto / oil / indices, where tick volume carries information) from
    low-volume-informativeness FX majors. Source: TradingView MFI strategies
    (Money Flow Index, MFI 20/80 reversal, volume-weighted RSI). MEAN-REVERSION
    entry: mfi <= oversold (20) -> BUY (fade up), mfi >= overbought (80) -> SELL
    (fade down). Conf = avg(structure, liquidity) (reversion-shaped, same as
    RSI/CCI/stoch/BB reversion). Classic 20/80 thresholds (same scale as stoch,
    distinct from RSI 30/70 and CCI +/-100 — a different oscillator's
    thresholds).
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    mfi = float(feat.get("mfi") if feat.get("mfi") is not None else 50.0)
    oversold = float(p.get("oversold", 20))
    overbought = float(p.get("overbought", 80))
    struct = float(ev.get("structure", 0))
    liq = float(ev.get("liquidity", 0))
    if mfi <= oversold:
        return _base(setup_type, "BUY", (struct + liq) / 2,
                     f"{reason_prefix} — MFI {mfi:.0f} <= {oversold:.0f} oversold (fade up to mean)")
    if mfi >= overbought:
        return _base(setup_type, "SELL", (struct + liq) / 2,
                     f"{reason_prefix} — MFI {mfi:.0f} >= {overbought:.0f} overbought (fade down to mean)")
    return None


def _adx_trend_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                         reason_prefix: str) -> dict | None:
    """Shared ADX trend-STRENGTH continuation detector, session-gated
    (iteration 25).

    Keys off ``feat.adx`` (Wilder's ADX(14) trend-strength line, a NEW feature
    exposed this iteration) as the primary strength gate AND ``feat.di_plus`` /
    ``feat.di_minus`` for direction. This is a SIXTEENTH entry model and a
    genuinely-new DIMENSION: ADX is a TREND-STRENGTH oscillator (non-directional
    — it reads how strong a trend is, not which way). NO prior setup measures
    strength — the 15 prior models use DIRECTION (breakout/cross/m5_trend/
    m15_trend), REVERSION (oscillator extremes: bb/stoch/rsi/cci/mfi), or VOLUME
    (volume_spike/volume_breakout). ADX is mathematically distinct from all of
    them (DI smoothing of directional moves normalized to a strength index).
    So london_adx_trend / ny_adx_trend land in their own culturing cells,
    distinct from the directional trend models (mtf_align iter15, htf_breakout
    iter18, vol_expansion iter13). That matters because those directional trend
    models split per-symbol without a clear edge — ADX is a clean test of
    whether a STRENGTH filter (only trade strong trends, ADX >= 25 classic)
    works where directional-only filters didn't. Source: TradingView ADX / DMI
    strategies (Average Directional Index, ADX 25 trend filter, DI crossover).
    CONTINUATION entry: adx >= min_adx (25) strong trend AND di_plus > di_minus
    -> BUY (bullish continuation), di_minus > di_plus -> SELL (bearish
    continuation). Conf = avg(trend, momentum) (continuation-shaped, same as
    stoch_cross/macd_cross). Classic 25 strong-trend threshold (tunable).
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    adx = float(feat.get("adx") if feat.get("adx") is not None else 0.0)
    di_plus = float(feat.get("di_plus") if feat.get("di_plus") is not None else 0.0)
    di_minus = float(feat.get("di_minus") if feat.get("di_minus") is not None else 0.0)
    min_adx = float(p.get("min_adx", 25.0))
    trend_ev = float(ev.get("trend", 0))
    mom = float(ev.get("momentum", 0))
    if adx < min_adx:
        return None
    if di_plus > di_minus:
        return _base(setup_type, "BUY", (trend_ev + mom) / 2,
                     f"{reason_prefix} — ADX {adx:.0f} >= {min_adx:.0f} strong trend, +DI>{di_minus:.0f} (bullish continuation)")
    if di_minus > di_plus:
        return _base(setup_type, "SELL", (trend_ev + mom) / 2,
                     f"{reason_prefix} — ADX {adx:.0f} >= {min_adx:.0f} strong trend, -DI>{di_plus:.0f} (bearish continuation)")
    return None


def _obv_cross_in_window(setup_type: str, feat: dict, ev: dict, cfg: dict[str, Any],
                         reason_prefix: str) -> dict | None:
    """Shared OBV-EMA-cross volume-accumulation detector, session-gated
    (iteration 26).

    Keys off ``feat.obv_cross`` (a NEW feature exposed this iteration:
    On-Balance Volume — the cumulative signed-volume line — crossing its own
    EMA(20)). bullish_cross = OBV crosses above its EMA -> net accumulation
    (buyers adding across bars) -> BUY; bearish_cross = OBV crosses below its
    EMA -> net distribution -> SELL. This is a SEVENTEENTH entry model and a
    genuinely-new VOLUME-ACCUMULATION dimension. NO prior setup measures
    cumulative volume: volume_spike (iter14) and volume_breakout (iter20) key
    off volume_ratio (a SINGLE-BAR spike), mfi (iter24) is a WINDOWED pos/neg
    money-flow RATIO (bounded oscillator). OBV is the running TOTAL of signed
    volume — its direction measures who is accumulating across MANY bars, not
    one bar. Mathematically distinct from MFI (MFI folds in TP and normalizes
    to a ratio per window; OBV is unbounded cumulative, read via its EMA cross).

    OBV is path-dependent (cumulative from the first bar), so live (full
    history) and the replay cap (max_bars tail) have different absolute OBV
    levels — but the CROSS EVENT is independent of the starting level after
    EMA warmup (a constant offset shifts OBV and its EMA equally), so
    live/research SIGNAL parity holds (verified 8/8 across symbols). Conf =
    avg(volume, momentum) (volume-continuation-shaped). Source: TradingView On
    Balance Volume + OBV EMA strategies (OBV EMA Cross, Volume Accumulation).
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    cross = feat.get("obv_cross")
    vol_ev = float(ev.get("volume", 0))
    mom = float(ev.get("momentum", 0))
    if cross == "bullish_cross":
        return _base(setup_type, "BUY", (vol_ev + mom) / 2,
                     f"{reason_prefix} — bullish OBV/EMA cross (volume accumulation)")
    if cross == "bearish_cross":
        return _base(setup_type, "SELL", (vol_ev + mom) / 2,
                     f"{reason_prefix} — bearish OBV/EMA cross (volume distribution)")
    return None


def _atr_pct_breakout_in_window(setup_type: str, feat: dict, ev: dict,
                                cfg: dict[str, Any], reason_prefix: str) -> dict | None:
    """Shared ATR-percentile-breakout relative-vol-expansion detector,
    session-gated (iteration 27).

    Keys off ``feat.atr_pct`` (a NEW feature exposed this iteration: the
    percentile rank 0..1 of the current ATR(14) within its own rolling 100-bar
    ATR history) as the primary gate AND ``feat.m5_trend`` for direction. This
    is an EIGHTEENTH entry model and a genuinely-new VOLATILITY-RANK dimension.
    NO prior setup measures relative vol: vol_expansion (iter 13) gates on the
    ABSOLUTE ``volatility_regime == "high"`` (atr_ratio thresholded), which a
    symbol with perpetually high absolute vol sits in forever. atr_pct instead
    asks where current vol sits within its OWN recent history (a non-parametric
    rolling rank) — so it only fires when vol is expanding RELATIVE to the
    symbol's own recent range, regardless of absolute level. Mathematically
    distinct (a rolling rank, not a thresholded ratio). So london/ny
    atr_pct_breakout land in their own culturing cells, distinct from
    vol_expansion (absolute gate). That matters because vol_expansion split
    per-symbol without a clear edge — atr_pct is a clean test of whether a
    RELATIVE-vol gate works where the absolute gate didn't. Source: TradingView
    ATR Percentile / Volatility Rank strategies (ATR Rank, Volatility
    Percentile breakout). CONTINUATION entry: atr_pct >= min_atr_pct (0.70
    classic "high-relative-vol") AND m5_trend bullish -> BUY, bearish -> SELL
    (relative-vol expansion momentum continuation). Conf = avg(volatility,
    momentum) (continuation-shaped, same as vol_expansion).
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    atr_pct = float(feat.get("atr_pct") if feat.get("atr_pct") is not None else 0.5)
    min_atr_pct = float(p.get("min_atr_pct", 0.70))
    if atr_pct < min_atr_pct:
        return None
    trend = feat.get("m5_trend")
    vol_ev = float(ev.get("volatility", 0))
    mom = float(ev.get("momentum", 0))
    if trend == "bullish":
        return _base(setup_type, "BUY", (vol_ev + mom) / 2,
                     f"{reason_prefix} — ATR-pct {atr_pct:.0%} >= {min_atr_pct:.0%} + bullish M5 trend (relative-vol expansion)")
    if trend == "bearish":
        return _base(setup_type, "SELL", (vol_ev + mom) / 2,
                     f"{reason_prefix} — ATR-pct {atr_pct:.0%} >= {min_atr_pct:.0%} + bearish M5 trend (relative-vol expansion)")
    return None


def _london_vol_expansion(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session volatility-expansion momentum continuation — 07:00-10:00 UTC.

    Source: TradingView ATR-expansion / volatility-breakout indicators (ATR
    Expansion Breakout, Volatility Breakout by Razvan, Squeeze Breakout). When
    ATR/price expands into the high regime inside the London killzone, trade the
    M5-trend direction. Vol-regime-based (via ``_vol_expansion_in_window``) — a
    different entry model from london_judas (rejection), asia_range_breakout
    (breakout), london_bb_reversion (BB), and london_stoch_cross (stoch) in the
    same window -> distinct culturing cell. FX majors + gold + EU indices. Trial
    via symbols: ["EURUSDm","GBPUSDm","XAUUSDm","UK100m","FR40m"].
    """
    return _vol_expansion_in_window("london_vol_expansion", feat, ev, cfg,
                                     "London vol expansion — 07:00-10:00 UTC")


def _ny_vol_expansion(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session volatility-expansion momentum continuation — 14:00-17:00 UTC.

    Source: TradingView NY volatility-breakout / ATR-expansion strategies (NY
    Volatility Breakout, ATR NY Session). ATR/price expands into the high regime
    during US RTH -> trade the M5-trend direction. Vol-regime-based (via
    ``_vol_expansion_in_window``). Distinct entry model + window -> distinct
    culturing cell. FX majors + US indices + oil. Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm"].
    """
    return _vol_expansion_in_window("ny_vol_expansion", feat, ev, cfg,
                                     "NY vol expansion — 14:00-17:00 UTC")


def _london_volume_spike(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session volume-spike momentum continuation — 07:00-10:00 UTC.

    Source: TradingView volume-spike / volume-confirmation indicators (Volume
    Spike, Volume Zone Pro, Better Volume). When bar volume spikes >= 1.5x its
    rolling average inside the London killzone, trade the M5-trend direction
    (volume confirms the move). Volume-ratio-based (via ``_volume_spike_in_window``)
    — a different entry model from london_judas (rejection), asia_range_breakout
    (breakout), london_bb_reversion (BB), london_stoch_cross (stoch), and
    london_vol_expansion (vol regime) in the same window -> distinct culturing
    cell. FX majors + gold + EU indices. Trial via symbols:
    ["EURUSDm","GBPUSDm","XAUUSDm","UK100m","FR40m"].
    """
    return _volume_spike_in_window("london_volume_spike", feat, ev, cfg,
                                   "London volume spike — 07:00-10:00 UTC")


def _ny_volume_spike(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session volume-spike momentum continuation — 14:00-17:00 UTC.

    Source: TradingView NY volume-spike / volume-confirmation strategies (NY
    Volume Spike, Volume Profile Entry). Bar volume spikes >= 1.5x avg during
    US RTH -> trade the M5-trend direction (volume confirms participation).
    Volume-ratio-based (via ``_volume_spike_in_window``). Distinct entry model +
    window -> distinct culturing cell. FX majors + US indices + oil. Trial via
    symbols: ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm"].
    """
    return _volume_spike_in_window("ny_volume_spike", feat, ev, cfg,
                                   "NY volume spike — 14:00-17:00 UTC")


def _london_mtf_align(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session multi-timeframe-alignment momentum continuation — 07:00-10:00 UTC.

    Source: TradingView multi-timeframe trend-confirmation indicators (MTF
    Trend, Triple Screen, EMA20 M5+M15 alignment dashboards). When the M5 and M15
    EMA20-slope trends agree inside the London killzone, trade the aligned
    direction (higher-timeframe confluence). Alignment-based (via
    ``_mtf_align_in_window``) — a different entry model from london_judas
    (rejection), asia_range_breakout (breakout), london_bb_reversion (BB),
    london_stoch_cross (stoch), london_vol_expansion (vol regime), and
    london_volume_spike (volume) in the same window -> distinct culturing cell.
    FX majors + gold + EU indices. Trial via symbols:
    ["EURUSDm","GBPUSDm","XAUUSDm","UK100m","FR40m"].
    """
    return _mtf_align_in_window("london_mtf_align", feat, ev, cfg,
                                "London MTF align — 07:00-10:00 UTC")


def _ny_mtf_align(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session multi-timeframe-alignment momentum continuation — 14:00-17:00 UTC.

    Source: TradingView NY multi-timeframe trend-confirmation strategies (MTF
    Trend NY, Dual EMA M5/M15). M5 and M15 EMA20-slope trends agree during US RTH
    -> trade the aligned direction (HTF confluence). Alignment-based (via
    ``_mtf_align_in_window``). Distinct entry model + window -> distinct culturing
    cell. FX majors + US indices + oil. Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm"].
    """
    return _mtf_align_in_window("ny_mtf_align", feat, ev, cfg,
                                "NY MTF align — 14:00-17:00 UTC")


def _london_stoch_reversion(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session stochastic oversold/overbought mean reversion — 07:00-10:00 UTC.

    Source: TradingView stochastic-reversion indicators (Stochastic Revers,
    Stochastic Divergence, %K extreme reversion). When stoch %K reaches an
    extreme (<=20 oversold / >=80 overbought) inside the London killzone, fade
    back toward the mean. Stoch-level-based (via ``_stoch_reversion_in_window``)
    — a different entry model from london_judas (rejection), london_bb_reversion
    (BB), and london_stoch_cross (stoch CROSS continuation) in the same window:
    this is a MEAN-REVERSION entry from a stoch extreme, not a continuation
    entry on a cross -> distinct culturing cell. FX majors + gold + EU indices.
    Trial via symbols: ["EURUSDm","GBPUSDm","XAUUSDm","UK100m","FR40m"].
    """
    return _stoch_reversion_in_window("london_stoch_reversion", feat, ev, cfg,
                                       "London stoch reversion — 07:00-10:00 UTC")


def _ny_stoch_reversion(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session stochastic oversold/overbought mean reversion — 14:00-17:00 UTC.

    Source: TradingView NY stochastic-reversion strategies (NY Stochastic
    Reversal, Stochastic Overbought/Oversold fade). Stoch %K reaches an extreme
    during US RTH -> fade back toward the mean. Stoch-level-based (via
    ``_stoch_reversion_in_window``). Distinct entry model + window -> distinct
    culturing cell. FX majors + US indices + oil. Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm"].
    """
    return _stoch_reversion_in_window("ny_stoch_reversion", feat, ev, cfg,
                                       "NY stoch reversion — 14:00-17:00 UTC")


def _london_htf_breakout(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session higher-timeframe-trend-filtered breakout — 07:00-10:00 UTC.

    Source: TradingView "trade breakouts in the direction of the higher
    timeframe trend" strategies (HTF Trend Filter, M15 Trend-Filtered
    Breakout). Take a structural breakout (close > prior 50-bar resistance)
    ONLY when the M15 EMA20-slope trend agrees in direction — i.e. an
    M15-bullish + breakout-up BUY, M15-bearish + breakdown-down SELL.
    HTF-trend-direction-filtered (via ``_htf_breakout_in_window``) — a NINTH
    entry model: it keys off ``feat.m15_trend`` DIRECTION as a primary trigger,
    which was previously unused (timeframe_alignment uses the m5==m15 boolean
    MATCH, not the m15 direction alone; the plain breakout/compression_breakout
    setups don't filter by M15 trend at all). The culturing ledger can thus
    distinguish per symbol whether HTF-trend filtering improves breakouts vs the
    unfiltered breakout setups. FX majors + gold + EU indices. Trial via
    symbols: ["EURUSDm","GBPUSDm","XAUUSDm","UK100m","FR40m"].
    """
    return _htf_breakout_in_window("london_htf_breakout", feat, ev, cfg,
                                   "London HTF-trend-filtered breakout — 07:00-10:00 UTC")


def _ny_htf_breakout(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session higher-timeframe-trend-filtered breakout — 14:00-17:00 UTC.

    Source: TradingView NY HTF-trend-filtered breakout strategies (NYSE RTH
    Trend-Filtered Breakout). Structural breakout during US RTH only when the
    M15 trend direction agrees. HTF-trend-direction-filtered (via
    ``_htf_breakout_in_window``) — ninth entry model + distinct window ->
    distinct culturing cell. FX majors + US indices + oil. Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm"].
    """
    return _htf_breakout_in_window("ny_htf_breakout", feat, ev, cfg,
                                   "NY HTF-trend-filtered breakout — 14:00-17:00 UTC")


def _london_squeeze_breakout(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session Bollinger-bandwidth squeeze-release breakout — 07:00-10:00 UTC.

    Source: TradingView TTM Squeeze + Bollinger Band Squeeze strategies (Keltner
    / BB squeeze coil, then trade the release). Enter when BB bandwidth is in
    its tightest 20% of the last 50 bars (compression coil) AND price breaks
    structure — the breakout direction the coil releases. Squeeze-percentile-
    based (via ``_squeeze_breakout_in_window``) — a TENTH entry model: it keys
    off ``feat.bb_squeeze_pct``, a NEW rolling-compression feature exposed this
    iteration (no prior setup used a rolling squeeze baseline; vol_expansion
    uses the absolute volatility_regime=="high", the opposite end). Distinct
    culturing cell -> the ledger distinguishes per symbol whether
    compression-filtered breakouts beat unfiltered breakouts. FX majors + gold
    + EU indices. Trial via symbols: ["EURUSDm","GBPUSDm","XAUUSDm","UK100m","FR40m"].
    """
    return _squeeze_breakout_in_window("london_squeeze_breakout", feat, ev, cfg,
                                       "London BB-squeeze breakout — 07:00-10:00 UTC")


def _ny_squeeze_breakout(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session Bollinger-bandwidth squeeze-release breakout — 14:00-17:00 UTC.

    Source: TradingView NY squeeze-release strategies (NYSE RTH TTM Squeeze
    breakout). BB bandwidth in its tightest 20% of the last 50 bars during US
    RTH + a structural breakout -> trade the release. Squeeze-percentile-based
    (via ``_squeeze_breakout_in_window``) — tenth entry model + distinct window
    -> distinct culturing cell. FX majors + US indices + oil. Trial via
    symbols: ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm"].
    """
    return _squeeze_breakout_in_window("ny_squeeze_breakout", feat, ev, cfg,
                                       "NY BB-squeeze breakout — 14:00-17:00 UTC")


def _london_volume_breakout(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session volume-confirmed breakout — 07:00-10:00 UTC.

    Source: TradingView Volume Breakout / Volume-Confirmed Breakout strategies.
    A structural breakout (close > prior 50-bar resistance) confirmed by
    above-average tick-volume (volume_ratio >= 1.5) inside the London killzone.
    Volume-confirmed (via ``_volume_breakout_in_window``) — an ELEVENTH entry
    model: the genuinely-unused COMPOUND is volume_ratio + breakout STATE
    (volume_spike uses volume_ratio + m5_trend; plain breakout setups have no
    volume gate). Distinct culturing cell -> the ledger distinguishes per symbol
    whether volume confirmation improves breakouts. FX majors + gold + EU
    indices. Trial via symbols: ["EURUSDm","GBPUSDm","XAUUSDm","UK100m","FR40m"].
    """
    return _volume_breakout_in_window("london_volume_breakout", feat, ev, cfg,
                                      "London volume-confirmed breakout — 07:00-10:00 UTC")


def _ny_volume_breakout(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session volume-confirmed breakout — 14:00-17:00 UTC.

    Source: TradingView NY volume-breakout strategies (NYSE RTH Volume
    Breakout). Structural breakout during US RTH confirmed by above-average
    tick-volume. Volume-confirmed (via ``_volume_breakout_in_window``) —
    eleventh entry model + distinct window -> distinct culturing cell. FX majors
    + US indices + oil. Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm"].
    """
    return _volume_breakout_in_window("ny_volume_breakout", feat, ev, cfg,
                                      "NY volume-confirmed breakout — 14:00-17:00 UTC")


def _london_rsi_reversion(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session RSI oversold/overbought mean reversion — 07:00-10:00 UTC.

    Source: TradingView RSI Overbought/Oversold + RSI 30/70 reversal strategies.
    When Wilder's RSI(14) reaches an extreme (<=30 oversold / >=70 overbought)
    inside the London killzone, fade back toward the mean. RSI-level-based (via
    ``_rsi_reversion_in_window``) — a TWELFTH entry model: RSI is a distinct
    oscillator from stoch_k (smoothed momentum-ratio vs price-range position),
    so this is a different entry model from london_stoch_reversion in the same
    window -> distinct culturing cell. stoch_reversion (iter 16) was a broad
    per-symbol loser; this tests whether RSI avoids that trap per symbol. FX
    majors + gold + EU indices. Trial via symbols:
    ["EURUSDm","GBPUSDm","XAUUSDm","UK100m","FR40m"].
    """
    return _rsi_reversion_in_window("london_rsi_reversion", feat, ev, cfg,
                                    "London RSI reversion — 07:00-10:00 UTC")


def _ny_rsi_reversion(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session RSI oversold/overbought mean reversion — 14:00-17:00 UTC.

    Source: TradingView NY RSI reversal strategies (NYSE RTH RSI
    Overbought/Oversold fade). RSI(14) reaches an extreme during US RTH -> fade
    back to the mean. RSI-level-based (via ``_rsi_reversion_in_window``) —
    twelfth entry model + distinct window -> distinct culturing cell. FX majors
    + US indices + oil. Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm"].
    """
    return _rsi_reversion_in_window("ny_rsi_reversion", feat, ev, cfg,
                                    "NY RSI reversion — 14:00-17:00 UTC")


def _london_macd_cross(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session MACD signal-line-cross momentum continuation — 07:00-10:00 UTC.

    Source: TradingView MACD Strategy / MACD Crossover strategies (Moving
    Average Convergence Divergence). A MACD/signal-line cross inside the London
    killzone is a smoothed-momentum continuation trigger. MACD-based (via
    ``_macd_cross_in_window``) — a THIRTEENTH entry model: distinct oscillator
    from london_stoch_cross (price-range vs smoothed-momentum) in the same
    window -> distinct culturing cell. FX majors + gold + EU indices. Trial via
    symbols: ["EURUSDm","GBPUSDm","XAUUSDm","UK100m","FR40m"].
    """
    return _macd_cross_in_window("london_macd_cross", feat, ev, cfg,
                                 "London MACD cross — 07:00-10:00 UTC")


def _ny_macd_cross(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session MACD signal-line-cross momentum continuation — 14:00-17:00 UTC.

    Source: TradingView NY MACD strategies (MACD NY Session, MACD Crossover
    RTH). A MACD/signal-line cross during US RTH. MACD-based (via
    ``_macd_cross_in_window``) — thirteenth entry model + distinct window ->
    distinct culturing cell. FX majors + US indices + oil. Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm"].
    """
    return _macd_cross_in_window("ny_macd_cross", feat, ev, cfg,
                                 "NY MACD cross — 14:00-17:00 UTC")


def _london_cci_reversion(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session CCI oversold/overbought mean reversion — 07:00-10:00 UTC.

    Source: TradingView CCI strategies (Commodity Channel Index, CCI 100/-100
    reversal). When CCI(20) reaches an extreme (<= -100 oversold / >= +100
    overbought) inside the London killzone, fade back toward the mean.
    CCI-level-based (via ``_cci_reversion_in_window``) — a FOURTEENTH entry
    model: CCI is a price-deviation-from-MA oscillator, distinct from
    london_rsi_reversion (gain/loss ratio) and london_stoch_reversion
    (price-range position) in the same window -> distinct culturing cell. Tests
    whether a price-deviation fade works per symbol where RSI/stoch fades
    didn't. FX majors + gold + EU indices. Trial via symbols:
    ["EURUSDm","GBPUSDm","XAUUSDm","UK100m","FR40m"].
    """
    return _cci_reversion_in_window("london_cci_reversion", feat, ev, cfg,
                                    "London CCI reversion — 07:00-10:00 UTC")


def _ny_cci_reversion(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session CCI oversold/overbought mean reversion — 14:00-17:00 UTC.

    Source: TradingView NY CCI strategies (CCI NY Session, CCI 100/-100 RTH
    reversal). CCI(20) reaches an extreme during US RTH -> fade back to the
    mean. CCI-level-based (via ``_cci_reversion_in_window``) — fourteenth entry
    model + distinct window -> distinct culturing cell. FX majors + US indices
    + oil. Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm"].
    """
    return _cci_reversion_in_window("ny_cci_reversion", feat, ev, cfg,
                                    "NY CCI reversion — 14:00-17:00 UTC")


def _london_mfi_reversion(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session MFI oversold/overbought mean reversion — 07:00-10:00 UTC.

    Source: TradingView MFI strategies (Money Flow Index, MFI 20/80 reversal,
    volume-weighted RSI). When MFI(14) reaches an extreme (<= 20 oversold /
    >= 80 overbought) inside the London killzone, fade back toward the mean.
    MFI-level-based (via ``_mfi_reversion_in_window``) — a FIFTEENTH entry
    model: MFI is volume-weighted (the only oscillator here that folds in
    volume), distinct from london_rsi_reversion (pure-price gain/loss ratio),
    london_cci_reversion (price-deviation from MA), and london_stoch_reversion
    (price-range position) in the same window -> distinct culturing cell. Tests
    whether a volume-weighted fade works per symbol on volume-informative
    markets (crypto / oil / EU indices) where the pure-price fades split without
    a clear edge. Trial via symbols:
    ["EURUSDm","GBPUSDm","XAUUSDm","UK100m","FR40m","BTCUSDm"].
    """
    return _mfi_reversion_in_window("london_mfi_reversion", feat, ev, cfg,
                                    "London MFI reversion — 07:00-10:00 UTC")


def _ny_mfi_reversion(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session MFI oversold/overbought mean reversion — 14:00-17:00 UTC.

    Source: TradingView NY MFI strategies (MFI NY Session, MFI 20/80 RTH
    reversal, volume-weighted RSI). MFI(14) reaches an extreme during US RTH ->
    fade back to the mean. MFI-level-based (via ``_mfi_reversion_in_window``) —
    fifteenth entry model + distinct window -> distinct culturing cell. Tests
    whether a volume-weighted fade works per symbol on volume-informative US
    markets (US indices + oil + crypto) where the pure-price fades didn't.
    Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm","BTCUSDm"].
    """
    return _mfi_reversion_in_window("ny_mfi_reversion", feat, ev, cfg,
                                    "NY MFI reversion — 14:00-17:00 UTC")


def _london_adx_trend(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session ADX trend-strength continuation — 07:00-10:00 UTC.

    Source: TradingView ADX / DMI strategies (Average Directional Index, ADX 25
    trend filter, DI crossover). When ADX(14) >= 25 (a strong trend, regardless
    of direction) inside the London killzone, trade the DI direction (+DI > -DI
    bullish continuation / -DI > +DI bearish continuation). ADX-strength-based
    (via ``_adx_trend_in_window``) — a SIXTEENTH entry model and a new DIMENSION:
    trend STRENGTH, distinct from london_mtf_align (m5==m15 boolean direction
    match), london_htf_breakout (m15 direction + breakout state), and
    london_vol_expansion (vol regime + m5 trend) in the same window -> distinct
    culturing cell. Tests whether a strength filter (only trade strong trends)
    works per symbol where the directional-only trend models split without an
    edge. FX majors + gold + EU indices. Trial via symbols:
    ["EURUSDm","GBPUSDm","XAUUSDm","UK100m","FR40m"].
    """
    return _adx_trend_in_window("london_adx_trend", feat, ev, cfg,
                                "London ADX trend — 07:00-10:00 UTC")


def _ny_adx_trend(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session ADX trend-strength continuation — 14:00-17:00 UTC.

    Source: TradingView NY ADX / DMI strategies (ADX NY Session, ADX 25 RTH
    trend filter, DI crossover). ADX(14) >= 25 during US RTH -> trade the DI
    direction (strong-trend continuation). ADX-strength-based (via
    ``_adx_trend_in_window``) — sixteenth entry model + distinct window ->
    distinct culturing cell. Tests whether a strength filter works per symbol
    on US markets (US indices + oil) where the directional-only trend models
    didn't. Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm"].
    """
    return _adx_trend_in_window("ny_adx_trend", feat, ev, cfg,
                                "NY ADX trend — 14:00-17:00 UTC")


def _london_obv_cross(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session OBV-EMA-cross volume-accumulation continuation — 07:00-10:00 UTC.

    Source: TradingView On Balance Volume + OBV EMA strategies (OBV EMA Cross,
    Volume Accumulation London). OBV (cumulative signed volume) crossing its
    EMA(20) inside the London killzone = net accumulation/distribution across
    many bars -> momentum continuation. OBV-volume-accumulation-based (via
    ``_obv_cross_in_window``) — a SEVENTEENTH entry model and a genuinely-new
    VOLUME-ACCUMULATION dimension (cumulative, vs volume_spike/volume_breakout
    single-bar and mfi windowed-ratio). Distinct entry model + window ->
    distinct culturing cell. Tests whether cumulative-volume direction works
    per symbol in the London session. FX majors + gold + EU indices. Trial
    via symbols: ["EURUSDm","GBPUSDm","USDCHFm","XAUUSDm","FR40m","UK100m"].
    """
    return _obv_cross_in_window("london_obv_cross", feat, ev, cfg,
                                "London OBV cross — 07:00-10:00 UTC")


def _ny_obv_cross(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session OBV-EMA-cross volume-accumulation continuation — 14:00-17:00 UTC.

    Source: TradingView NY OBV / volume-accumulation strategies (OBV NY
    Session, Volume Accumulation NY). OBV crossing its EMA(20) during US RTH
    = net accumulation/distribution -> momentum continuation. OBV-volume-
    accumulation-based (via ``_obv_cross_in_window``). Seventeenth entry model
    + distinct window -> distinct culturing cell. Tests whether cumulative-
    volume direction works per symbol on US markets (US indices + oil + gold).
    Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm","XAUUSDm"].
    """
    return _obv_cross_in_window("ny_obv_cross", feat, ev, cfg,
                                "NY OBV cross — 14:00-17:00 UTC")


def _london_atr_pct_breakout(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session ATR-percentile-breakout relative-vol expansion — 07:00-10:00 UTC.

    Source: TradingView ATR Percentile / Volatility Rank strategies (ATR Rank,
    Volatility Percentile breakout). When ATR(14)'s percentile rank within its
    own 100-bar history >= 0.70 inside the London killzone (relative-vol
    expanding, regardless of absolute level), trade the M5-trend direction.
    Relative-vol-rank-based (via ``_atr_pct_breakout_in_window``) — an
    EIGHTEENTH entry model and a genuinely-new VOLATILITY-RANK dimension
    (rolling percentile rank, vs vol_expansion's absolute atr_ratio gate).
    Distinct entry model + window -> distinct culturing cell. Tests whether a
    relative-vol gate works per symbol where the absolute-vol gate didn't.
    FX majors + gold + EU indices. Trial via symbols:
    ["EURUSDm","GBPUSDm","USDCHFm","XAUUSDm","FR40m","UK100m"].
    """
    return _atr_pct_breakout_in_window("london_atr_pct_breakout", feat, ev, cfg,
                                       "London ATR-pct breakout — 07:00-10:00 UTC")


def _ny_atr_pct_breakout(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session ATR-percentile-breakout relative-vol expansion — 14:00-17:00 UTC.

    Source: TradingView NY ATR-rank / volatility-percentile strategies (ATR
    Rank NY, Volatility Percentile NY). ATR(14)'s percentile rank within its
    own 100-bar history >= 0.70 during US RTH (relative-vol expanding) -> trade
    the M5-trend direction. Relative-vol-rank-based (via
    ``_atr_pct_breakout_in_window``). Eighteenth entry model + distinct window
    -> distinct culturing cell. Tests whether a relative-vol gate works per
    symbol on US markets (US indices + oil + gold). Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm","XAUUSDm"].
    """
    return _atr_pct_breakout_in_window("ny_atr_pct_breakout", feat, ev, cfg,
                                       "NY ATR-pct breakout — 14:00-17:00 UTC")


def _cmf_continuation_in_window(setup_type: str, feat: dict, ev: dict,
                                cfg: dict[str, Any], reason_prefix: str) -> dict | None:
    """Shared Chaikin-Money-Flow-confirmed continuation detector,
    session-gated (iteration 28).

    Keys off ``feat.cmf`` (a NEW feature exposed this iteration: Chaikin Money
    Flow(20), the sum of (money-flow-multiplier * volume) over 20 bars divided
    by sum(volume), bounded [-1, +1]) as the primary gate AND ``feat.m5_trend``
    for direction. This is a NINETEENTH entry model and a genuinely-new VOLUME
    dimension — the FOURTH volume feature, each measuring something distinct:
    volume_ratio (iter 14/20) = single-bar volume LEVEL vs average; mfi
    (iter 24) = price-CHANGE-weighted money-flow ratio (reversion); obv
    (iter 26) = CUMULATIVE signed-DIRECTION volume line (cross). CMF instead
    keys off the intrabar close LOCATION weighted by volume
    ((2*close-high-low)/(high-low) * volume), summed and normalized —
    accumulation/distribution PRESSURE as a bounded oscillator. A close near
    the bar high on high volume = buying pressure (+); near the low on high
    volume = distribution (-). Captures intrabar flow that close-to-close
    (MFI/OBV) and level-only (volume_ratio) both miss. So london/ny
    cmf_continuation land in their own culturing cells, distinct from the
    three other volume setups. That matters because all three volume models
    came back neutral-to-negative on indices — CMF is a clean test of whether
    intrabar money-flow pressure works per symbol where they didn't.
    Source: TradingView Chaikin Money Flow strategies (CMF confirmation, CMF
    trend). CONTINUATION entry: cmf >= +min_cmf (0.10 classic accumulation
    threshold) AND m5_trend bullish -> BUY; cmf <= -min_cmf (distribution) AND
    m5_trend bearish -> SELL. Conf = avg(volume, momentum) (volume-confirmed
    continuation).
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    cmf = float(feat.get("cmf") if feat.get("cmf") is not None else 0.0)
    min_cmf = float(p.get("min_cmf", 0.10))
    trend = feat.get("m5_trend")
    vol_ev = float(ev.get("volume", 0))
    mom = float(ev.get("momentum", 0))
    if trend == "bullish" and cmf >= min_cmf:
        return _base(setup_type, "BUY", (vol_ev + mom) / 2,
                     f"{reason_prefix} — CMF {cmf:+.2f} >= +{min_cmf:.2f} (accumulation) + bullish M5 trend")
    if trend == "bearish" and cmf <= -min_cmf:
        return _base(setup_type, "SELL", (vol_ev + mom) / 2,
                     f"{reason_prefix} — CMF {cmf:+.2f} <= -{min_cmf:.2f} (distribution) + bearish M5 trend")
    return None


def _london_cmf_continuation(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session CMF-confirmed continuation — 07:00-10:00 UTC.

    Source: TradingView Chaikin Money Flow strategies (CMF confirmation, CMF
    trend). When CMF(20) >= +0.10 (accumulation — closes near bar highs on
    rising volume) inside the London killzone AND M5 trend is bullish, buy the
    continuation; CMF <= -0.10 (distribution) + bearish -> sell. Volume-pressure-
    based (via ``_cmf_continuation_in_window``) — a NINETEENTH entry model and
    a genuinely-new VOLUME dimension (intrabar close-location * volume, the 4th
    volume feature: vs volume_ratio level, mfi price-change ratio, obv
    cumulative line). Distinct entry model + window -> distinct culturing cell.
    Tests whether intrabar money-flow pressure works per symbol where the other
    three volume models came back neutral-to-negative. FX majors + gold + EU
    indices. Trial via symbols:
    ["EURUSDm","GBPUSDm","USDCHFm","XAUUSDm","FR40m","UK100m"].
    """
    return _cmf_continuation_in_window("london_cmf_continuation", feat, ev, cfg,
                                       "London CMF continuation — 07:00-10:00 UTC")


def _ny_cmf_continuation(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session CMF-confirmed continuation — 14:00-17:00 UTC.

    Source: TradingView NY Chaikin Money Flow strategies (CMF NY confirmation,
    CMF trend NY). CMF(20) >= +0.10 (accumulation) during US RTH AND M5 trend
    bullish -> buy; CMF <= -0.10 (distribution) + bearish -> sell. Volume-
    pressure-based (via ``_cmf_continuation_in_window``). Nineteenth entry model
    + distinct window -> distinct culturing cell. Tests whether intrabar
    money-flow pressure works per symbol on US markets (US indices + oil +
    gold). Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm","XAUUSDm"].
    """
    return _cmf_continuation_in_window("ny_cmf_continuation", feat, ev, cfg,
                                       "NY CMF continuation — 14:00-17:00 UTC")


def _triple_confirm_in_window(setup_type: str, feat: dict, ev: dict,
                              cfg: dict[str, Any], reason_prefix: str) -> dict | None:
    """Shared triple-confirmation momentum-continuation detector,
    session-gated (iteration 29).

    A 3-WAY COMPOUND entry model — the first setup whose primary key is the
    CONJUNCTION of three independent dimensions, not any single feature. Fires
    only when ALL THREE of the strongest per-symbol-positive dimensions from
    iter 25/27/28 hold simultaneously:

      1. ``feat.adx >= min_adx`` (25) — Wilder trend STRENGTH (iter25 dim): the
         only strength dimension here, filters out range/chop where continuation
         fails.
      2. ``feat.atr_pct >= min_atr_pct`` (0.70) — ATR(14) percentile rank within
         its 100-bar history (iter27 dim): the only relative-volatility-RANK
         dimension, fires only when vol is EXPANDING relative to the symbol's
         own recent history (not a perpetually-high-absolute-vol artifact).
      3. ``feat.cmf`` agrees with trend direction — cmf >= +min_cmf (0.10
         accumulation) when bullish, <= -min_cmf (distribution) when bearish
         (iter28 dim): intrabar close-location * volume PRESSURE, the 4th
         volume dimension, confirms buyers/sellers are participating in the
         direction the trend is pushing.

    AND ``feat.m5_trend`` directional (bullish -> BUY, bearish -> SELL).

    Each dim individually came back necessary-not-sufficient (per-cell CI lo>0
    selection-biased across 630 cells, ~9.5% naive-pass). The CONJUNCTION is a
    much rarer, more selective event — the hypothesis: strong-trend +
    relative-vol-expanding + accumulation-pressure agreeing at once is a
    higher-conviction continuation that survives cost where any single dim
    alone does not. This is the cleanest test of whether the 3-dim per-cell
    agreement (strongest on EUR/GBP London+NY per iter28) translates into a
    higher-quality setup. Source: TradingView multi-confirmation / confluence
    strategies (ADX+vol+money-flow confluence, triple-screen confirmation).
    CONTINUATION entry. Conf = avg(trend, momentum, volume) (three-confirmation
    continuation — higher bar than the avg-of-two used by the single-dim
    setups).
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    adx = float(feat.get("adx") if feat.get("adx") is not None else 0.0)
    atr_pct = float(feat.get("atr_pct") if feat.get("atr_pct") is not None else 0.5)
    cmf = float(feat.get("cmf") if feat.get("cmf") is not None else 0.0)
    min_adx = float(p.get("min_adx", 25))
    min_atr_pct = float(p.get("min_atr_pct", 0.70))
    min_cmf = float(p.get("min_cmf", 0.10))
    if adx < min_adx or atr_pct < min_atr_pct:
        return None
    trend = feat.get("m5_trend")
    tr_ev = float(ev.get("trend", 0))
    mom = float(ev.get("momentum", 0))
    vol_ev = float(ev.get("volume", 0))
    if trend == "bullish" and cmf >= min_cmf:
        return _base(setup_type, "BUY", (tr_ev + mom + vol_ev) / 3,
                     f"{reason_prefix} — ADX {adx:.0f}>={min_adx:.0f} + ATR-pct {atr_pct:.0%}>={min_atr_pct:.0%} + CMF {cmf:+.2f} (accumulation) + bullish M5 trend (triple confirmation)")
    if trend == "bearish" and cmf <= -min_cmf:
        return _base(setup_type, "SELL", (tr_ev + mom + vol_ev) / 3,
                     f"{reason_prefix} — ADX {adx:.0f}>={min_adx:.0f} + ATR-pct {atr_pct:.0%}>={min_atr_pct:.0%} + CMF {cmf:+.2f} (distribution) + bearish M5 trend (triple confirmation)")
    return None


def _london_triple_confirm(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session triple-confirmation momentum continuation — 07:00-10:00 UTC.

    Source: TradingView multi-confirmation / confluence strategies (ADX+vol+
    money-flow confluence, triple-screen confirmation). Fires only when ADX(14)
    >= 25 (strong trend) AND ATR(14) percentile rank >= 0.70 (relative-vol
    expanding) AND CMF(20) >= +0.10 (accumulation) / <= -0.10 (distribution)
    agreeing with the M5 trend direction — all three at once — inside the
    London killzone. A 3-WAY COMPOUND entry model (the 20th here) — the
    conjunction of the three strongest per-symbol-positive dimensions from
    iter25/27/28 is the primary key, distinct from any single-dim setup. The
    tightest 3-dim per-cell agreement was EUR/GBP London+NY. Tests whether the
    rare triple-confluence produces a higher-conviction continuation that
    survives cost where each dim alone does not. FX majors + gold + EU
    indices. Trial via symbols: ["EURUSDm","GBPUSDm","USDCHFm","XAUUSDm",
    "FR40m","UK100m"].
    """
    return _triple_confirm_in_window("london_triple_confirm", feat, ev, cfg,
                                     "London triple-confirm — 07:00-10:00 UTC")


def _ny_triple_confirm(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session triple-confirmation momentum continuation — 14:00-17:00 UTC.

    Source: TradingView NY multi-confirmation / confluence strategies (ADX+vol+
    money-flow NY confluence, triple-screen NY). ADX >= 25 + ATR-pct >= 0.70 +
    CMF agrees with trend + M5 directional — all at once — during US RTH. A
    3-WAY COMPOUND entry model (20th) + distinct window -> distinct culturing
    cell. Tests whether triple confluence works per symbol on US markets (US
    indices + oil + gold). Trial via symbols: ["EURUSDm","GBPUSDm","US30m",
    "US500m","NAS100m","USOILm","XAUUSDm"].
    """
    return _triple_confirm_in_window("ny_triple_confirm", feat, ev, cfg,
                                     "NY triple-confirm — 14:00-17:00 UTC")


def _adx_cmf_in_window(setup_type: str, feat: dict, ev: dict,
                       cfg: dict[str, Any], reason_prefix: str) -> dict | None:
    """Shared ADX+CMF 2-way-compound continuation detector,
    session-gated (iteration 30).

    A 2-WAY COMPOUND — the second compound entry model. Fires when TWO of the
    three triple-confirm dimensions hold (dropping the atr_pct relative-vol-rank
    gate from iter29):

      1. ``feat.adx >= min_adx`` (25) — Wilder trend STRENGTH (iter25 dim).
      2. ``feat.cmf`` agrees with trend direction — cmf >= +min_cmf (0.10
         accumulation) when bullish, <= -min_cmf (distribution) when bearish
         (iter28 dim): intrabar close-location * volume PRESSURE.

    AND ``feat.m5_trend`` directional (bullish -> BUY, bearish -> SELL).

    PURPOSE — isolate which dims carry the EUR/GBP edge. iter29's triple
    (adx+atr_pct+cmf) produced the strongest cells in the loop on EUR/GBP NY
    (+0.465R / +0.387R, 74%/69% wr). This 2-way adx×cmf compound DROPS the
    atr_pct gate. Comparing its EUR/GBP cells to the triple tells us whether
    atr_pct adds independent signal: if adx×cmf alone reproduces the edge,
    atr_pct is redundant (correlated with adx in expanding markets); if it
    degrades, atr_pct carries independent information. adx (strength family) ×
    cmf (volume family) is the most independent pairing of the three. Source:
    TradingView ADX+Money-Flow confluence strategies (ADX+CMF confirmation).
    CONTINUATION entry. Conf = avg(trend, momentum, volume) (two-confirmation
    continuation).
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    adx = float(feat.get("adx") if feat.get("adx") is not None else 0.0)
    cmf = float(feat.get("cmf") if feat.get("cmf") is not None else 0.0)
    min_adx = float(p.get("min_adx", 25))
    min_cmf = float(p.get("min_cmf", 0.10))
    if adx < min_adx:
        return None
    trend = feat.get("m5_trend")
    tr_ev = float(ev.get("trend", 0))
    mom = float(ev.get("momentum", 0))
    vol_ev = float(ev.get("volume", 0))
    if trend == "bullish" and cmf >= min_cmf:
        return _base(setup_type, "BUY", (tr_ev + mom + vol_ev) / 3,
                     f"{reason_prefix} — ADX {adx:.0f}>={min_adx:.0f} + CMF {cmf:+.2f} (accumulation) + bullish M5 trend (2-way confluence)")
    if trend == "bearish" and cmf <= -min_cmf:
        return _base(setup_type, "SELL", (tr_ev + mom + vol_ev) / 3,
                     f"{reason_prefix} — ADX {adx:.0f}>={min_adx:.0f} + CMF {cmf:+.2f} (distribution) + bearish M5 trend (2-way confluence)")
    return None


def _london_adx_cmf(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session ADX+CMF 2-way-compound continuation — 07:00-10:00 UTC.

    Source: TradingView ADX+Money-Flow confluence strategies (ADX+CMF
    confirmation). ADX(14) >= 25 (strong trend) AND CMF(20) >= +0.10
    (accumulation) / <= -0.10 (distribution) agreeing with the M5 trend — both
    at once — inside the London killzone. A 2-WAY COMPOUND entry model (the
    21st here) that DROPS the atr_pct gate from iter29's triple — isolates
    whether trend-strength + intrabar money-flow pressure alone reproduce the
    EUR/GBP edge, or whether the relative-vol-rank dim carries independent
    signal. FX majors + gold + EU indices. Trial via symbols:
    ["EURUSDm","GBPUSDm","USDCHFm","XAUUSDm","FR40m","UK100m"].
    """
    return _adx_cmf_in_window("london_adx_cmf", feat, ev, cfg,
                              "London ADX+CMF — 07:00-10:00 UTC")


def _ny_adx_cmf(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session ADX+CMF 2-way-compound continuation — 14:00-17:00 UTC.

    Source: TradingView NY ADX+Money-Flow confluence strategies. ADX >= 25 +
    CMF agrees with trend + M5 directional — during US RTH. A 2-WAY COMPOUND
    entry model (21st) + distinct window -> distinct culturing cell. Tests
    whether adx×cmf alone reproduces the EUR/GBP NY edge from iter29's triple
    on US markets (US indices + oil + gold). Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm","XAUUSDm"].
    """
    return _adx_cmf_in_window("ny_adx_cmf", feat, ev, cfg,
                              "NY ADX+CMF — 14:00-17:00 UTC")


def _adx_atr_pct_in_window(setup_type: str, feat: dict, ev: dict,
                           cfg: dict[str, Any], reason_prefix: str) -> dict | None:
    """Shared ADX+ATR-pct 2-way-compound continuation detector,
    session-gated (iteration 31).

    A 2-WAY COMPOUND — the third compound entry model. Fires when TWO of the
    three triple-confirm dimensions hold, this time DROPPING the cmf money-flow
    gate from iter29:

      1. ``feat.adx >= min_adx`` (25) — Wilder trend STRENGTH (iter25 dim).
      2. ``feat.atr_pct >= min_atr_pct`` (0.70) — ATR(14) rolling 100-bar
         percentile rank (iter27 dim): vol EXPANDING relative to the symbol's
         own recent history.

    AND ``feat.m5_trend`` directional (bullish -> BUY, bearish -> SELL).
    cmf is intentionally NOT required — the dimension-attribution complement
    to iter30's adx×cmf.

    PURPOSE — complete the compound-attribution triangle. iter29 triple
    (adx+atr_pct+cmf) = strongest EUR/GBP NY cells. iter30 adx×cmf (drop
    atr_pct) ALONE reproduced the EUR/GBP edge broadly -> atr_pct looked like
    a non-redundant SHARPENER (triple 74% wr / +0.46R vs adx×cmf 65% / +0.32R
    at half sample). This adx×atr_pct compound (drop cmf) tests the remaining
    pairing: does strength + relative-vol-rank WITHOUT intrabar money-flow
    pressure carry the edge? If adx×atr_pct degrades vs adx×cmf on EUR/GBP,
    cmf is localized as the primary carrier and atr_pct the multiplier —
    closing the attribution. Source: TradingView ADX+vol-expansion confluence
    strategies (ADX+ATR breakout confirmation, no money-flow). CONTINUATION
    entry. Conf = avg(trend, momentum, volume) (two-confirmation continuation).
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    adx = float(feat.get("adx") if feat.get("adx") is not None else 0.0)
    atr_pct = float(feat.get("atr_pct") if feat.get("atr_pct") is not None else 0.5)
    min_adx = float(p.get("min_adx", 25))
    min_atr_pct = float(p.get("min_atr_pct", 0.70))
    if adx < min_adx or atr_pct < min_atr_pct:
        return None
    trend = feat.get("m5_trend")
    tr_ev = float(ev.get("trend", 0))
    mom = float(ev.get("momentum", 0))
    vol_ev = float(ev.get("volume", 0))
    if trend == "bullish":
        return _base(setup_type, "BUY", (tr_ev + mom + vol_ev) / 3,
                     f"{reason_prefix} — ADX {adx:.0f}>={min_adx:.0f} + ATR-pct {atr_pct:.0%}>={min_atr_pct:.0%} + bullish M5 trend (2-way confluence, no money-flow gate)")
    if trend == "bearish":
        return _base(setup_type, "SELL", (tr_ev + mom + vol_ev) / 3,
                     f"{reason_prefix} — ADX {adx:.0f}>={min_adx:.0f} + ATR-pct {atr_pct:.0%}>={min_atr_pct:.0%} + bearish M5 trend (2-way confluence, no money-flow gate)")
    return None


def _london_adx_atr_pct(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session ADX+ATR-pct 2-way-compound continuation — 07:00-10:00 UTC.

    Source: TradingView ADX+vol-expansion confluence strategies (ADX+ATR
    breakout confirmation). ADX(14) >= 25 (strong trend) AND ATR(14) percentile
    rank >= 0.70 (relative-vol expanding) AND M5 trend directional — both at
    once — inside the London killzone, with NO cmf money-flow gate. A 2-WAY
    COMPOUND entry model (the 22nd here) that DROPS cmf from iter29's triple —
    the dimension-attribution complement to iter30's adx×cmf (which dropped
    atr_pct). Tests whether trend-strength + relative-vol-rank WITHOUT
    intrabar money-flow pressure carry the EUR/GBP edge, or whether cmf is the
    primary carrier. FX majors + gold + EU indices. Trial via symbols:
    ["EURUSDm","GBPUSDm","USDCHFm","XAUUSDm","FR40m","UK100m"].
    """
    return _adx_atr_pct_in_window("london_adx_atr_pct", feat, ev, cfg,
                                  "London ADX+ATR-pct — 07:00-10:00 UTC")


def _ny_adx_atr_pct(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session ADX+ATR-pct 2-way-compound continuation — 14:00-17:00 UTC.

    Source: TradingView NY ADX+vol-expansion confluence strategies. ADX >= 25 +
    ATR-pct >= 0.70 + M5 directional — during US RTH, with NO cmf gate. A 2-WAY
    COMPOUND entry model (22nd) + distinct window -> distinct culturing cell.
    Tests whether adx×atr_pct alone reproduces the EUR/GBP NY edge from iter29's
    triple on US markets (US indices + oil + gold), localizing whether cmf or
    atr_pct is the independent carrier. Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm","XAUUSDm"].
    """
    return _adx_atr_pct_in_window("ny_adx_atr_pct", feat, ev, cfg,
                                  "NY ADX+ATR-pct — 14:00-17:00 UTC")


def _atr_pct_cmf_in_window(setup_type: str, feat: dict, ev: dict,
                           cfg: dict[str, Any], reason_prefix: str) -> dict | None:
    """Shared ATR-pct+CMF 2-way-compound continuation detector,
    session-gated (iteration 32).

    A 2-WAY COMPOUND — the fourth and FINAL compound entry model. Fires when
    TWO of the three triple-confirm dimensions hold, this time DROPPING the adx
    trend-STRENGTH gate from iter29:

      1. ``feat.atr_pct >= min_atr_pct`` (0.70) — ATR(14) rolling 100-bar
         percentile rank (iter27 dim): vol EXPANDING relative to the symbol's
         own recent history.
      2. ``feat.cmf`` agrees with trend direction — cmf >= +min_cmf (0.10
         accumulation) when bullish, <= -min_cmf (distribution) when bearish
         (iter28 dim): intrabar close-location * volume PRESSURE.

    AND ``feat.m5_trend`` directional (bullish -> BUY, bearish -> SELL).
    adx is intentionally NOT required — the dimension-attribution complement
    to iter30's adx×cmf (dropped atr_pct) and iter31's adx×atr_pct (dropped
    cmf). This completes the full 2-way attribution matrix: all three pairs
    (adx×cmf, adx×atr_pct, atr_pct×cmf) plus the triple.

    PURPOSE — symmetric closure. iter30/31 showed adx is the BASE all
    effective compounds require (adx×cmf and adx×atr_pct both reproduce the
    EUR/GBP edge; dropping adx entirely is expected to degrade because
    strength filters the range/chop where continuation fails). This atr_pct×cmf
    pair tests that expectation directly: does vol-rank + money-flow WITHOUT
    trend-strength carry anything? If it degrades sharply vs the adx-bearing
    compounds, adx is confirmed as the necessary base and the attribution
    matrix is closed. Source: TradingView vol+money-flow confluence strategies
    (ATR+CMF continuation, no strength filter). CONTINUATION entry. Conf =
    avg(trend, momentum, volume) (two-confirmation continuation).
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    atr_pct = float(feat.get("atr_pct") if feat.get("atr_pct") is not None else 0.5)
    cmf = float(feat.get("cmf") if feat.get("cmf") is not None else 0.0)
    min_atr_pct = float(p.get("min_atr_pct", 0.70))
    min_cmf = float(p.get("min_cmf", 0.10))
    if atr_pct < min_atr_pct:
        return None
    trend = feat.get("m5_trend")
    tr_ev = float(ev.get("trend", 0))
    mom = float(ev.get("momentum", 0))
    vol_ev = float(ev.get("volume", 0))
    if trend == "bullish" and cmf >= min_cmf:
        return _base(setup_type, "BUY", (tr_ev + mom + vol_ev) / 3,
                     f"{reason_prefix} — ATR-pct {atr_pct:.0%}>={min_atr_pct:.0%} + CMF {cmf:+.2f} (accumulation) + bullish M5 trend (2-way confluence, no strength gate)")
    if trend == "bearish" and cmf <= -min_cmf:
        return _base(setup_type, "SELL", (tr_ev + mom + vol_ev) / 3,
                     f"{reason_prefix} — ATR-pct {atr_pct:.0%}>={min_atr_pct:.0%} + CMF {cmf:+.2f} (distribution) + bearish M5 trend (2-way confluence, no strength gate)")
    return None


def _london_atr_pct_cmf(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session ATR-pct+CMF 2-way-compound continuation — 07:00-10:00 UTC.

    Source: TradingView vol+money-flow confluence strategies (ATR+CMF
    continuation). ATR(14) percentile rank >= 0.70 (relative-vol expanding) AND
    CMF(20) agrees with trend (accumulation+bullish / distribution+bearish) AND
    M5 directional — both at once — inside the London killzone, with NO adx
    strength gate. A 2-WAY COMPOUND entry model (the 23rd here, the FINAL
    compound pair) that DROPS adx from iter29's triple — completes the full
    2-way attribution matrix (adx×cmf, adx×atr_pct, atr_pct×cmf + triple).
    Tests whether vol-rank + money-flow WITHOUT trend-strength carry the
    EUR/GBP edge (expected to degrade — adx is the base). FX majors + gold + EU
    indices. Trial via symbols: ["EURUSDm","GBPUSDm","USDCHFm","XAUUSDm",
    "FR40m","UK100m"].
    """
    return _atr_pct_cmf_in_window("london_atr_pct_cmf", feat, ev, cfg,
                                  "London ATR-pct+CMF — 07:00-10:00 UTC")


def _ny_atr_pct_cmf(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session ATR-pct+CMF 2-way-compound continuation — 14:00-17:00 UTC.

    Source: TradingView NY vol+money-flow confluence strategies. ATR-pct >= 0.70
    + CMF agrees with trend + M5 directional — during US RTH, with NO adx gate.
    A 2-WAY COMPOUND entry model (23rd, final pair) + distinct window -> distinct
    culturing cell. Tests whether atr_pct×cmf alone (no strength) reproduces the
    EUR/GBP NY edge from iter29's triple on US markets, closing the attribution
    matrix. Trial via symbols: ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m",
    "USOILm","XAUUSDm"].
    """
    return _atr_pct_cmf_in_window("ny_atr_pct_cmf", feat, ev, cfg,
                                  "NY ATR-pct+CMF — 14:00-17:00 UTC")


# --- iteration 34: session-gated VWAP-band mean reversion (24th entry model,
# uses feat.vwap_position — a VOLUME-WEIGHTED price-anchor dimension not used by
# any prior setup; the 5th volume dimension here, distinct from volume_ratio
# level / mfi price-change ratio / obv cumulative line / cmf intrabar pressure,
# and distinct from bb_position which is a price-only SMA band). ---

def _vwap_reversion_in_window(setup_type: str, feat: dict, ev: dict,
                              cfg: dict[str, Any], reason_prefix: str) -> dict | None:
    """Shared VWAP-band mean-reversion detector, session-gated (iteration 34).

    Keys off ``feat.vwap_position`` (the close's location within the rolling
    VWAP +/- 1.5sigma bands, in [0,1]). >= vwap_upper (0.92) -> price extended
    above the volume-weighted upper band -> overstretched up -> SELL fade to
    VWAP; <= vwap_lower (0.08) -> overstretched down -> BUY fade. Conf =
    avg(structure, liquidity) — the same reversion confidence shape as
    ``_bb_in_window`` (iteration 7), so the culturing ledger can isolate whether
    a VOLUME-WEIGHTED anchor fades extremes better than the price-only BB anchor
    on the same window. This is a 24TH entry model: VWAP is a genuinely-new
    PRICE-ANCHOR dimension (volume-weighted level + bands) not used by any of the
    23 prior entry models — bb_position uses a plain-SMA Bollinger band, the four
    volume features measure flow/level (volume_ratio/mfi/obv/cmf), none anchors a
    price level by volume. A session-gated VWAP reversion lands in a distinct
    culturing cell from BB reversion on the same window, so the ledger can
    distinguish per symbol whether VWAP reversion works where BB reversion
    doesn't. Adding a new VWAP-reversion setup is a one-liner wrapper.

    HONESTY: per-cell CI lo>0 on this setup is selection-biased across the full
    specialized-replay search (iter33 proved the EUR/GBP cluster's per-cell CI+
    was a selection artifact that inverts negative on full-history OOS). DSR/
    SPA + OOS walk-forward across the (now larger K) search remains the bar; the
    per-cell numbers below are necessary-not-sufficient, NOT a deployable edge.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    vwap_upper = float(p.get("vwap_upper", 0.92))
    vwap_lower = float(p.get("vwap_lower", 0.08))
    vp = float(feat.get("vwap_position", 0.5))
    struct = float(ev.get("structure", 0))
    liq = float(ev.get("liquidity", 0))
    if vp >= vwap_upper:
        return _base(setup_type, "SELL", (struct + liq) / 2,
                     f"{reason_prefix} — overstretched above VWAP upper band")
    if vp <= vwap_lower:
        return _base(setup_type, "BUY", (struct + liq) / 2,
                     f"{reason_prefix} — overstretched below VWAP lower band")
    return None


def _london_vwap_reversion(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session VWAP-band mean reversion — 07:00-10:00 UTC.

    Source: TradingView VWAP reversion / VWAP-band fade strategies (Anchored
    VWAP, VWAP Bands mean reversion). Fade an extension beyond the rolling
    VWAP+/-1.5sigma band inside the London killzone. VWAP-based (via
    ``_vwap_reversion_in_window``) — a different entry model from london_bb_reversion
    (BB, price-only anchor) and london_judas (rejection) in the same window, so
    it lands in its own culturing cell. The 24th entry model and the 5th volume
    dimension (volume-weighted price anchor). For FX majors + gold + EU indices.
    Trial via symbols: ["EURUSDm","GBPUSDm","USDCHFm","XAUUSDm","FR40m","UK100m"].
    """
    return _vwap_reversion_in_window("london_vwap_reversion", feat, ev, cfg,
                                     "London VWAP reversion — 07:00-10:00 UTC")


def _ny_vwap_reversion(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session VWAP-band mean reversion — 14:00-17:00 UTC.

    Source: TradingView NY VWAP fade / VWAP-band reversion strategies. Fade an
    extension beyond the rolling VWAP band during US RTH. VWAP-based (via
    ``_vwap_reversion_in_window``). Distinct entry model + window -> distinct
    culturing cell from ny_bb_reversion (price-only BB) on the same window. The
    24th entry model + 5th volume dimension. For FX majors + US indices + oil +
    gold. Trial via symbols: ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m",
    "USOILm","XAUUSDm"].
    """
    return _vwap_reversion_in_window("ny_vwap_reversion", feat, ev, cfg,
                                     "NY VWAP reversion — 14:00-17:00 UTC")


# --- iteration 35: session-gated Supertrend-flip trend continuation (25th entry
# model, uses feat.supertrend_flip — an ATR-band trend-STATE flip dimension not
# used by any prior setup; distinct from m5_trend EMA DIRECTION and adx STRENGTH). ---

def _supertrend_flip_in_window(setup_type: str, feat: dict, ev: dict,
                               cfg: dict[str, Any], reason_prefix: str) -> dict | None:
    """Shared Supertrend-flip trend-continuation detector, session-gated (iter 35).

    Keys off ``feat.supertrend_flip`` (bullish_flip = Supertrend direction
    DOWN->UP, the ATR-band trailing stop flipped bullish -> BUY continuation;
    bearish_flip = UP->DOWN -> SELL). Inside a UTC window with momentum >= min.
    Conf = avg(trend, momentum) — a trend-CONTINUATION confidence (the flip IS
    the trend signal), the same shape as the stoch_cross / macd_cross momentum
    continuations so the culturing ledger can isolate whether an ATR-band
    trend-state flip carries edge where EMA-slope direction (m5_trend, used by
    trend_continuation / all the compound setups) and ADX strength (adx_trend)
    do not.

    This is a 25TH entry model: Supertrend is a genuinely-new TREND-STATE
    dimension — an ATR-band overlay (hl2 +/- 3*ATR(10)) that ratchets to track
    structure and flips on a clean ATR-adjusted close-cross, the canonical
    TradingView trend-continuation trigger. No prior setup keys off a band-flip
    trend state: m5_trend (EMA(20) slope sign) is trend DIRECTION; adx (iter 25)
    is trend STRENGTH; atr_pct (iter 27) is relative-vol RANK; this is the
    trend STATE that results from an ATR-band cross. A session-gated Supertrend
    flip lands in a distinct culturing cell from every prior setup, so the
    ledger can distinguish per symbol whether the Supertrend flip works where
    EMA/ADX trend do not. Adding a new Supertrend-flip setup is a one-liner
    wrapper.

    HONESTY: per-cell CI lo>0 on this setup is selection-biased across the full
    specialized-replay search (iter33 proved the EUR/GBP cluster's per-cell CI+
    was a selection artifact that inverts negative on full-history OOS; iter34's
    VWAP showed 1/28 CI+ in the chance zone). DSR/SPA + OOS walk-forward across
    the (now larger K) search remains the bar; the per-cell numbers below are
    necessary-not-sufficient, NOT a deployable edge.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    flip = feat.get("supertrend_flip")
    tr_ev = float(ev.get("trend", 0))
    mom = float(ev.get("momentum", 0))
    if flip == "bullish_flip":
        return _base(setup_type, "BUY", (tr_ev + mom) / 2,
                     f"{reason_prefix} — Supertrend bullish flip (ATR-band trend state UP)")
    if flip == "bearish_flip":
        return _base(setup_type, "SELL", (tr_ev + mom) / 2,
                     f"{reason_prefix} — Supertrend bearish flip (ATR-band trend state DOWN)")
    return None


def _london_supertrend_flip(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session Supertrend-flip trend continuation — 07:00-10:00 UTC.

    Source: TradingView Supertrend strategies (Supertrend + EMA, Supertrend
    scalping, Supertrend trend-follow). On a bullish Supertrend flip (ATR-band
    trailing stop DOWN->UP) inside the London killzone -> BUY continuation;
    bearish flip -> SELL. Supertrend-based (via ``_supertrend_flip_in_window``)
    — a different entry model from london_stoch_cross (stoch-cross momentum) and
    london_adx_trend (strength) in the same window, so it lands in its own
    culturing cell. The 25th entry model and a new trend-STATE dimension (ATR-
    band flip, distinct from EMA direction and ADX strength). For FX majors +
    gold + EU indices. Trial via symbols: ["EURUSDm","GBPUSDm","USDCHFm",
    "XAUUSDm","FR40m","UK100m"].
    """
    return _supertrend_flip_in_window("london_supertrend_flip", feat, ev, cfg,
                                      "London Supertrend flip — 07:00-10:00 UTC")


def _ny_supertrend_flip(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session Supertrend-flip trend continuation — 14:00-17:00 UTC.

    Source: TradingView NY Supertrend / Supertrend trend-follow strategies. On a
    Supertrend flip during US RTH -> trade the flip direction (continuation).
    Supertrend-based (via ``_supertrend_flip_in_window``). Distinct entry model +
    window -> distinct culturing cell. The 25th entry model + new trend-STATE
    dimension. For FX majors + US indices + oil + gold. Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm","XAUUSDm"].
    """
    return _supertrend_flip_in_window("ny_supertrend_flip", feat, ev, cfg,
                                      "NY Supertrend flip — 14:00-17:00 UTC")


def _ichimoku_tk_cross_in_window(setup_type: str, feat: dict, ev: dict,
                                 cfg: dict[str, Any], reason_prefix: str) -> dict | None:
    """Shared Ichimoku Tenkan/Kijun-cross continuation detector, session-gated
    (iter 36).

    Keys off ``feat.ichimoku_tk_cross`` (bullish_cross = Tenkan-sen crosses
    ABOVE Kijun-sen, the short-term equilibrium crossing above the medium-term
    baseline; bearish_cross = cross below) CONFIRMED by ``feat.ichimoku_cloud_
    position`` (price above the Senkou Span A/B cloud = bullish bias, below =
    bearish bias). A bullish TK cross with price ABOVE the cloud is the iconic
    Ichimoku strong-buy continuation; cross + price inside the cloud is weak and
    skipped (no cloud confirmation). Conf = avg(trend, momentum) — the same
    trend-CONTINUATION shape as supertrend_flip / macd_cross so the culturing
    ledger can isolate whether the rolling-midpoint-equilibrium cross carries
    edge where EMA-slope direction (m5_trend) and ATR-band state (supertrend)
    do not.

    This is the 26TH entry model: Ichimoku is a genuinely-new EQUILIBRIUM
    dimension — a rolling high-low MIDPOINT (max(high,n)+min(low,n))/2, the
    short/medium-term fair value with no smoothing lag and no volume weighting.
    No prior setup keys off a midpoint equilibrium: m5_trend (EMA(20) slope) is
    smoothed-price direction; adx (iter 25) is strength; supertrend (iter 35)
    is an ATR-band state; vwap (iter 34) is a volume-weighted anchor; the
    oscillators (rsi/stoch/cci/mfi) use close-vs-range. The Tenkan/Kijun
    midpoint cross confirmed by cloud side is the canonical TradingView
    Ichimoku continuation trigger. A session-gated TK cross lands in a distinct
    culturing cell from every prior setup, so the ledger can distinguish per
    symbol whether the Ichimoku equilibrium cross works where EMA/ADX/Supertrend
    do not. Adding a new Ichimoku-TK-cross setup is a one-liner wrapper.

    HONESTY: per-cell CI lo>0 on this setup is selection-biased across the full
    specialized-replay search (iter33 proved the EUR/GBP cluster's per-cell CI+
    was a selection artifact that inverts negative on full-history OOS; iter34's
    VWAP showed 1/28 CI+ and iter35's Supertrend 2/28 CI+ — both chance-zone
    noise). DSR/SPA + OOS walk-forward across the (now larger K) search remains
    the bar; the per-cell numbers below are necessary-not-sufficient, NOT a
    deployable edge.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    tk = feat.get("ichimoku_tk_cross")
    cloud = feat.get("ichimoku_cloud_position")
    tr_ev = float(ev.get("trend", 0))
    mom = float(ev.get("momentum", 0))
    if tk == "bullish_cross" and cloud == "above":
        return _base(setup_type, "BUY", (tr_ev + mom) / 2,
                     f"{reason_prefix} — Ichimoku bullish TK cross (Tenkan over Kijun) confirmed above cloud")
    if tk == "bearish_cross" and cloud == "below":
        return _base(setup_type, "SELL", (tr_ev + mom) / 2,
                     f"{reason_prefix} — Ichimoku bearish TK cross (Tenkan under Kijun) confirmed below cloud")
    return None


def _london_ichimoku_tk_cross(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session Ichimoku Tenkan/Kijun-cross continuation — 07:00-10:00 UTC.

    Source: TradingView Ichimoku Cloud strategies (Ichimoku TK cross + cloud
    filter, Ichimoku trend continuation). On a bullish Tenkan-over-Kijun cross
    with price above the Senkou cloud inside the London killzone -> BUY
    continuation; bearish cross + below cloud -> SELL. Ichimoku-based (via
    ``_ichimoku_tk_cross_in_window``) — a different entry model from london_adx_
    trend (strength) and london_supertrend_flip (ATR-band state) in the same
    window, so it lands in its own culturing cell. The 26th entry model and a
    new EQUILIBRIUM dimension (rolling high-low midpoint, distinct from EMA
    direction, ADX strength, ATR-band state, and volume-weighted VWAP). For FX
    majors + gold + EU indices. Trial via symbols: ["EURUSDm","GBPUSDm",
    "USDCHFm","XAUUSDm","FR40m","UK100m"].
    """
    return _ichimoku_tk_cross_in_window("london_ichimoku_tk_cross", feat, ev, cfg,
                                         "London Ichimoku TK cross — 07:00-10:00 UTC")


def _ny_ichimoku_tk_cross(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session Ichimoku Tenkan/Kijun-cross continuation — 14:00-17:00 UTC.

    Source: TradingView NY Ichimoku / Ichimoku trend-continuation strategies. On
    a TK cross with cloud confirmation during US RTH -> trade the cross
    direction (continuation). Ichimoku-based (via ``_ichimoku_tk_cross_in_
    window``). Distinct entry model + window -> distinct culturing cell. The
    26th entry model + new EQUILIBRIUM dimension. For FX majors + US indices +
    oil + gold. Trial via symbols: ["EURUSDm","GBPUSDm","US30m","US500m",
    "NAS100m","USOILm","XAUUSDm"].
    """
    return _ichimoku_tk_cross_in_window("ny_ichimoku_tk_cross", feat, ev, cfg,
                                         "NY Ichimoku TK cross — 14:00-17:00 UTC")


def _fvg_in_window(setup_type: str, feat: dict, ev: dict,
                   cfg: dict[str, Any], reason_prefix: str) -> dict | None:
    """Shared Fair Value Gap imbalance-continuation detector, session-gated
    (iter 37).

    Keys off ``feat.fvg`` (bullish_fvg = a 3-bar ICT/SMC structural imbalance
    where bar[i-2].high < bar[i].low, a gap-up inefficiency to the upside that
    price tends to return to fill before continuing; bearish_fvg = bar[i-2].low
    > bar[i].high, gap-down). Inside a UTC window. The gap is gated by
    fvg_size_atr >= min_size_atr (0.25*ATR) so only a MEANINGFUL inefficiency
    fires, not a sub-noise wiggle. Conf = avg(momentum, volume) — an
    imbalance/momentum confidence (the FVG IS the imbalance signal), the same
    shape as the volume_breakout / vol_expansion momentum continuations so the
    culturing ledger can isolate whether a 3-bar structural gap carries edge
    where the prior-bar S/R breakouts and ATR/volume expansions do not.

    This is the 27TH entry model: FVG is a genuinely-new PRICE-IMBALANCE
    dimension — a gap between NON-ADJACENT bars (bar i-2 vs bar i, with bar i-1
    as the impulse bar), the canonical ICT/SMC inefficiency that price tends to
    fill. No prior setup keys off a non-adjacent-bar gap: the breakouts
    (breakout/compression/squeeze/volume/htf/atr_pct) all use PRIOR-BAR S/R
    levels (a 1-bar structure); m5_trend (EMA slope), adx (strength),
    supertrend (ATR-band state), ichimoku (iter 36, midpoint-equilibrium cross),
    and vwap (volume-weighted anchor) are all adjacent/continuous structures.
    A session-gated FVG lands in a distinct culturing cell from every prior
    setup, so the ledger can distinguish per symbol whether the 3-bar imbalance
    works where 1-bar breakouts do not. Adding a new FVG setup is a one-liner
    wrapper.

    HONESTY: per-cell CI lo>0 is selection-biased across the full specialized-
    replay search (iter33-36 all confirmed; iter36 Ichimoku had 0/28 CI+, the
    cleanest negative). DSR/SPA + OOS walk-forward across the (now larger K)
    search remains the bar; the per-cell numbers below are necessary-not-
    sufficient, NOT a deployable edge.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    fvg = feat.get("fvg")
    mom = float(ev.get("momentum", 0))
    vol = float(ev.get("volume", 0))
    if fvg == "bullish_fvg":
        return _base(setup_type, "BUY", (mom + vol) / 2,
                     f"{reason_prefix} — bullish Fair Value Gap (3-bar upside imbalance, price tends to fill then continue up)")
    if fvg == "bearish_fvg":
        return _base(setup_type, "SELL", (mom + vol) / 2,
                     f"{reason_prefix} — bearish Fair Value Gap (3-bar downside imbalance, price tends to fill then continue down)")
    return None


def _london_fvg(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session Fair Value Gap imbalance continuation — 07:00-10:00 UTC.

    Source: TradingView ICT/SMC Fair Value Gap strategies (FVG fill + continue,
    ICT killzone FVG). On a bullish FVG (3-bar upside structural imbalance,
    gap size >= 0.25*ATR) inside the London killzone -> BUY continuation; bearish
    FVG -> SELL. FVG-based (via ``_fvg_in_window``) — a different entry model
    from london_breakout (prior-bar S/R) and london_volume_breakout
    (volume-confirmed prior-bar S/R) in the same window, so it lands in its own
    culturing cell. The 27th entry model and a new PRICE-IMBALANCE dimension
    (non-adjacent 3-bar gap, distinct from all 1-bar-S/R and continuous-structure
    setups). For FX majors + gold + EU indices. Trial via symbols: ["EURUSDm",
    "GBPUSDm","USDCHFm","XAUUSDm","FR40m","UK100m"].
    """
    return _fvg_in_window("london_fvg", feat, ev, cfg,
                          "London FVG — 07:00-10:00 UTC")


def _ny_fvg(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session Fair Value Gap imbalance continuation — 14:00-17:00 UTC.

    Source: TradingView NY ICT/SMC Fair Value Gap strategies. On an FVG during
    US RTH -> trade the imbalance direction (continuation, expecting a fill-
    then-continue). FVG-based (via ``_fvg_in_window``). Distinct entry model +
    window -> distinct culturing cell. The 27th entry model + new PRICE-IMBALANCE
    dimension. For FX majors + US indices + oil + gold. Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm","XAUUSDm"].
    """
    return _fvg_in_window("ny_fvg", feat, ev, cfg,
                          "NY FVG — 14:00-17:00 UTC")


def _engulfing_in_window(setup_type: str, feat: dict, ev: dict,
                        cfg: dict[str, Any], reason_prefix: str) -> dict | None:
    """Shared 2-bar Engulfing candlestick-reversal detector, session-gated
    (iter 38).

    Keys off ``feat.engulfing`` (bullish_engulfing = a 2-bar candle-BODY
    structure where a bullish bar engulfs the prior bearish bar's body, a
    classic reversal; bearish_engulfing = bullish-then-bearish mirror). Inside
    a UTC window. Conf = avg(structure, momentum) — a reversal confidence (the
    engulfing IS the reversal signal), so the culturing ledger can isolate
    whether a 2-bar body-engulf carries edge where the 1-bar wick rejection
    (liquidity_sweep / false_breakout) and the 3-bar FVG gap (iter 37) do not.

    This is the 28TH entry model: Engulfing is a genuinely-new CANDLE-STRUCTURE
    dimension — a 2-bar BODY-vs-body engulf (the current bar's body wraps the
    prior bar's body). No prior setup keys off a 2-bar body engulf:
    rejection/liquidity_sweep/false_breakout use the 1-bar WICK-vs-body
    rejection (a single-bar wick signal); fvg (iter 37) uses a 3-bar gap between
    non-adjacent bars; the oscillators/crosses use smoothed levels. The 2-bar
    engulfing body-wrap is the canonical TradingView candlestick reversal
    pattern, a distinct entry model. A session-gated engulfing lands in a
    distinct culturing cell from every prior setup, so the ledger can
    distinguish per symbol whether the 2-bar body-engulf reversal works where
    1-bar wick rejection and 3-bar FVG do not. Adding an engulfing setup is a
    one-liner wrapper.

    HONESTY: per-cell CI lo>0 is selection-biased across the full specialized-
    replay search (iter33-37 all confirmed; iter37 FVG had 3/28 CI+ overlapping
    the iter33-adjudicated-negative EUR/GBP L+NY cluster). DSR/SPA + OOS
    walk-forward across the (now larger K) search remains the bar; the per-cell
    numbers below are necessary-not-sufficient, NOT a deployable edge.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    eng = feat.get("engulfing")
    struct = float(ev.get("structure", 0))
    mom = float(ev.get("momentum", 0))
    if eng == "bullish_engulfing":
        return _base(setup_type, "BUY", (struct + mom) / 2,
                     f"{reason_prefix} — bullish Engulfing (2-bar body engulf, bullish bar wraps prior bearish body)")
    if eng == "bearish_engulfing":
        return _base(setup_type, "SELL", (struct + mom) / 2,
                     f"{reason_prefix} — bearish Engulfing (2-bar body engulf, bearish bar wraps prior bullish body)")
    return None


def _london_engulfing(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session 2-bar Engulfing candlestick reversal — 07:00-10:00 UTC.

    Source: TradingView candlestick-pattern strategies (Engulfing reversal,
    session candle patterns). On a bullish Engulfing (2-bar body engulf) inside
    the London killzone -> BUY reversal; bearish Engulfing -> SELL.
    Engulfing-based (via ``_engulfing_in_window``) — a different entry model
    from london_liquidity_sweep (1-bar wick rejection) and london_fvg (3-bar
    gap) in the same window, so it lands in its own culturing cell. The 28th
    entry model and a new CANDLE-STRUCTURE dimension (2-bar body-vs-body engulf,
    distinct from 1-bar wick rejection + 3-bar FVG gap). For FX majors + gold +
    EU indices. Trial via symbols: ["EURUSDm","GBPUSDm","USDCHFm","XAUUSDm",
    "FR40m","UK100m"].
    """
    return _engulfing_in_window("london_engulfing", feat, ev, cfg,
                                "London Engulfing — 07:00-10:00 UTC")


def _ny_engulfing(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session 2-bar Engulfing candlestick reversal — 14:00-17:00 UTC.

    Source: TradingView NY candlestick-pattern strategies. On an Engulfing
    during US RTH -> trade the reversal direction (bullish engulfing -> BUY,
    bearish -> SELL). Engulfing-based (via ``_engulfing_in_window``). Distinct
    entry model + window -> distinct culturing cell. The 28th entry model + new
    CANDLE-STRUCTURE dimension. For FX majors + US indices + oil + gold. Trial
    via symbols: ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm",
    "XAUUSDm"].
    """
    return _engulfing_in_window("ny_engulfing", feat, ev, cfg,
                                "NY Engulfing — 14:00-17:00 UTC")


def _inside_bar_in_window(setup_type: str, feat: dict, ev: dict,
                          cfg: dict[str, Any], reason_prefix: str) -> dict | None:
    """Shared 3-bar Inside-Bar breakout detector, session-gated (iter 39).

    Keys off ``feat.inside_bar`` (bullish_inside_breakout = a mother bar, an
    inside bar whose range nests within the mother range, then a breakout bar
    that closes ABOVE the mother high — a volatility-contraction-then-expansion
    continuation; bearish_inside_breakout = closes BELOW the mother low). Inside
    a UTC window. Conf = avg(structure, momentum) — the breakout direction is
    the signal, so the culturing ledger can isolate whether a 2-3-bar
    range-nesting breakout carries edge where the 2-bar body engulf (iter 38),
    3-bar FVG gap (iter 37), and 1-bar wick rejection (iter 1) do not.

    This is the 29TH entry model: Inside-Bar breakout is a genuinely-new
    CANDLE-STRUCTURE dimension — a 2-3-bar RANGE-NESTING relationship (the
    inside bar's full range is contained within the mother bar's range, then
    expansion breaks the mother range). No prior setup keys off raw 2-bar
    range containment: rejection/liquidity_sweep/false_breakout use the 1-bar
    WICK-vs-body rejection; engulfing (iter 38) uses a 2-bar BODY-vs-body wrap;
    fvg (iter 37) uses a 3-bar price GAP between non-adjacent bars; legacy
    breakout/compression_breakout key off a 1-bar S/R state / BB-squeeze width
    (continuous metrics), not raw 2-bar range nesting. The inside-bar is the
    canonical TradingView volatility-contraction-then-expansion pattern, a
    distinct entry model. A session-gated inside-bar lands in a distinct
    culturing cell from every prior setup, so the ledger can distinguish per
    symbol whether the range-nesting breakout works where the body engulf, FVG
    gap, and wick rejection do not. Adding an inside-bar setup is a one-liner
    wrapper.

    HONESTY: per-cell CI lo>0 is selection-biased across the full specialized-
    replay search (iter33-38 all confirmed; iter38 Engulfing was the cleanest
    negative 0/28 CI+). DSR/SPA + OOS walk-forward across the (now larger K)
    search remains the bar; the per-cell numbers below are necessary-not-
    sufficient, NOT a deployable edge.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    ib = feat.get("inside_bar")
    struct = float(ev.get("structure", 0))
    mom = float(ev.get("momentum", 0))
    if ib == "bullish_inside_breakout":
        return _base(setup_type, "BUY", (struct + mom) / 2,
                     f"{reason_prefix} — bullish Inside-Bar breakout (3-bar range-nesting, breakout bar closes above mother high)")
    if ib == "bearish_inside_breakout":
        return _base(setup_type, "SELL", (struct + mom) / 2,
                     f"{reason_prefix} — bearish Inside-Bar breakout (3-bar range-nesting, breakout bar closes below mother low)")
    return None


def _london_inside_bar(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session 3-bar Inside-Bar breakout — 07:00-10:00 UTC.

    Source: TradingView inside-bar breakout strategies (volatility contraction
    then expansion, session killzones). On a bullish inside-bar breakout (range
    nests within mother bar, then closes above mother high) inside the London
    killzone -> BUY continuation; bearish -> SELL. Inside-bar-based (via
    ``_inside_bar_in_window``) — a different entry model from london_engulfing
    (2-bar body engulf) and london_fvg (3-bar gap) in the same window, so it
    lands in its own culturing cell. The 29th entry model and a new CANDLE-
    STRUCTURE dimension (2-3-bar range-nesting, distinct from 1-bar wick
    rejection + 2-bar body engulf + 3-bar FVG gap). For FX majors + gold + EU
    indices. Trial via symbols: ["EURUSDm","GBPUSDm","USDCHFm","XAUUSDm",
    "FR40m","UK100m"].
    """
    return _inside_bar_in_window("london_inside_bar", feat, ev, cfg,
                                 "London Inside-Bar — 07:00-10:00 UTC")


def _ny_inside_bar(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session 3-bar Inside-Bar breakout — 14:00-17:00 UTC.

    Source: TradingView NY inside-bar breakout strategies. On an inside-bar
    breakout during US RTH -> trade the breakout direction (bullish -> BUY,
    bearish -> SELL). Inside-bar-based (via ``_inside_bar_in_window``). Distinct
    entry model + window -> distinct culturing cell. The 29th entry model + new
    CANDLE-STRUCTURE dimension. For FX majors + US indices + oil + gold. Trial
    via symbols: ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm",
    "XAUUSDm"].
    """
    return _inside_bar_in_window("ny_inside_bar", feat, ev, cfg,
                                 "NY Inside-Bar — 14:00-17:00 UTC")


def _close_streak_in_window(setup_type: str, feat: dict, ev: dict,
                           cfg: dict[str, Any], reason_prefix: str) -> dict | None:
    """Shared consecutive-close-streak momentum-continuation detector,
    session-gated (iter 40).

    Keys off ``feat.close_streak`` — the signed count of consecutive
    same-direction closes ending at the current bar (a RUN-LENGTH statistic:
    +N = N consecutive up-closes, -N = N consecutive down-closes). Inside a UTC
    window. Fires when the streak reaches a threshold (default 3) in either
    direction, as a momentum-continuation signal: streak >= +threshold -> BUY
    (trade with the up-run), streak <= -threshold -> SELL (trade with the
    down-run). Conf = avg(momentum, volume) — the streak is a momentum signal
    and volume confirms commitment, so the culturing ledger can isolate
    whether a consecutive-close run-length carries edge where the EMA-trend
    state (m5_trend), ADX strength (adx_trend), and candle-structure patterns
    (engulfing/fvg/inside_bar) do not.

    This is the 30TH entry model: Close-streak is a genuinely-new STATISTICAL
    dimension — a discrete run-length / consecutive-count of same-direction
    closes. No prior setup keys off a run-length: m5_trend is an EMA-ribbon
    STATE; adx_trend is ADX STRENGTH; mtf_align is m5-vs-m15 AGREEMENT; the
    oscillators use smoothed LEVELS/crosses; the candle-structure setups use
    1-3-bar PATTERNS. None count a run of consecutive same-direction closes.
    The run-length is the canonical TradingView "consecutive candle streak"
    / "N-bar momentum" signal, a distinct entry model. A session-gated
    close-streak lands in a distinct culturing cell from every prior setup,
    so the ledger can distinguish per symbol whether the run-length
    continuation works where the EMA-state, ADX-strength, and candle-pattern
    entries do not. Adding a close-streak setup is a one-liner wrapper.

    HONESTY: per-cell CI lo>0 is selection-biased across the full specialized-
    replay search (iter33-39 all confirmed; iter38+iter39 both 0/28 CI+
    cleanest). DSR/SPA + OOS walk-forward across the (now larger K) search
    remains the bar; the per-cell numbers below are necessary-not-sufficient,
    NOT a deployable edge.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    streak = feat.get("close_streak", 0)
    try:
        streak = int(streak)
    except (TypeError, ValueError):
        return None
    threshold = p.get("min_streak", 3)
    try:
        threshold = int(threshold)
    except (TypeError, ValueError):
        threshold = 3
    mom = float(ev.get("momentum", 0))
    vol = float(ev.get("volume", 0))
    if streak >= threshold:
        return _base(setup_type, "BUY", (mom + vol) / 2,
                     f"{reason_prefix} — bullish close-streak (streak=+{streak}, {streak} consecutive up-closes -> momentum continuation)")
    if streak <= -threshold:
        return _base(setup_type, "SELL", (mom + vol) / 2,
                     f"{reason_prefix} — bearish close-streak (streak={streak}, {-streak} consecutive down-closes -> momentum continuation)")
    return None


def _london_close_streak(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session consecutive-close-streak continuation — 07:00-10:00 UTC.

    Source: TradingView consecutive-candle-streak / N-bar momentum strategies
    (session killzones). On a bullish close-streak (N consecutive up-closes)
    >= threshold inside the London killzone -> BUY continuation; bearish
    streak -> SELL. Streak-based (via ``_close_streak_in_window``) — a different
    entry model from london_adx_trend (ADX strength) and london_mtf_align
    (m5-vs-m15 agreement) in the same window, so it lands in its own culturing
    cell. The 30th entry model and a new STATISTICAL dimension (run-length of
    consecutive same-direction closes, distinct from EMA-state / ADX-strength /
    candle-structure patterns). For FX majors + gold + EU indices. Trial via
    symbols: ["EURUSDm","GBPUSDm","USDCHFm","XAUUSDm","FR40m","UK100m"].
    """
    return _close_streak_in_window("london_close_streak", feat, ev, cfg,
                                   "London Close-Streak — 07:00-10:00 UTC")


def _ny_close_streak(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session consecutive-close-streak continuation — 14:00-17:00 UTC.

    Source: TradingView NY consecutive-candle-streak / momentum strategies. On
    a close-streak >= threshold during US RTH -> trade the streak direction
    (bullish -> BUY, bearish -> SELL). Streak-based (via
    ``_close_streak_in_window``). Distinct entry model + window -> distinct
    culturing cell. The 30th entry model + new STATISTICAL dimension. For FX
    majors + US indices + oil + gold. Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm","XAUUSDm"].
    """
    return _close_streak_in_window("ny_close_streak", feat, ev, cfg,
                                   "NY Close-Streak — 14:00-17:00 UTC")


def _order_block_in_window(setup_type: str, feat: dict, ev: dict,
                           cfg: dict[str, Any], reason_prefix: str) -> dict | None:
    """Shared ICT/SMC Order-Block displacement-origin detector, session-gated
    (iter 41).

    Keys off ``feat.order_block`` (bullish_order_block = a prior BEARISH candle
    followed by a strong BULLISH displacement bar whose body >= 0.8*ATR(14); the
    prior bearish candle is the institutional "order block" origin and the strong
    body is the displacement away from it; bearish_order_block = mirror). Inside
    a UTC window. Conf = avg(structure, momentum) — a candle-structure signal
    (the displacement IS the directional signal), so the culturing ledger can
    isolate whether a displacement-magnitude origin carry edge where the 2-bar
    body-WRAP engulf (iter 38), the 3-bar range-nesting inside-bar (iter 39),
    the 3-bar FVG gap (iter 37), and the run-length close-streak (iter 40) do
    not.

    This is the 31ST entry model: Order Block is a genuinely-new CANDLE-STRUCTURE
    dimension — a 2-bar OPPOSITE-ORIGIN + IMPULSE-MAGNITUDE structure. No prior
    setup keys off a displacement body >= k*ATR after an opposite candle:
    engulfing (iter 38) keys off a body WRAP with no magnitude gate; inside_bar
    (iter 39) keys off range-nesting then expansion; fvg (iter 37) keys off a
    3-bar gap between non-adjacent bars; rejection/liquidity_sweep/false_breakout
    use a 1-bar wick signal; close_streak (iter 40) keys off a run-length. None
    gate on a displacement-magnitude body after an opposite origin candle. The
    order-block is the canonical TradingView ICT/SMC "displacement origin"
    signal, a distinct entry model. A session-gated order-block lands in a
    distinct culturing cell from every prior setup, so the ledger can
    distinguish per symbol whether the displacement-origin continuation works
    where the body-wrap engulf, range-nesting, gap, and run-length entries do
    not. Adding an order-block setup is a one-liner wrapper.

    HONESTY: per-cell CI lo>0 is selection-biased across the full specialized-
    replay search (iter33-40 all confirmed; iter40 Close-streak had 4/28 CI+
    overlapping the iter33-adjudicated-negative EUR/GBP L+NY cluster). DSR/SPA
    + OOS walk-forward across the (now larger K) search remains the bar; the
    per-cell numbers below are necessary-not-sufficient, NOT a deployable edge.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    ob = feat.get("order_block")
    struct = float(ev.get("structure", 0))
    mom = float(ev.get("momentum", 0))
    if ob == "bullish_order_block":
        return _base(setup_type, "BUY", (struct + mom) / 2,
                     f"{reason_prefix} — bullish Order Block (prior bearish candle + strong bullish displacement body >= 0.8*ATR -> displacement continuation)")
    if ob == "bearish_order_block":
        return _base(setup_type, "SELL", (struct + mom) / 2,
                     f"{reason_prefix} — bearish Order Block (prior bullish candle + strong bearish displacement body >= 0.8*ATR -> displacement continuation)")
    return None


def _london_order_block(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session ICT/SMC Order-Block displacement continuation — 07:00-10:00 UTC.

    Source: TradingView ICT/SMC order-block + displacement strategies (session
    killzones). On a bullish Order Block (prior bearish candle + strong bullish
    displacement body >= 0.8*ATR) inside the London killzone -> BUY continuation;
    bearish Order Block -> SELL. Order-block-based (via
    ``_order_block_in_window``) — a different entry model from london_engulfing
    (2-bar body wrap) and london_inside_bar (3-bar range-nesting) in the same
    window, so it lands in its own culturing cell. The 31st entry model and a new
    CANDLE-STRUCTURE dimension (opposite-origin + impulse-magnitude, distinct
    from body-wrap engulf + range-nesting inside-bar + 3-bar FVG gap + run-length
    close-streak). For FX majors + gold + EU indices. Trial via symbols:
    ["EURUSDm","GBPUSDm","USDCHFm","XAUUSDm","FR40m","UK100m"].
    """
    return _order_block_in_window("london_order_block", feat, ev, cfg,
                                  "London Order Block — 07:00-10:00 UTC")


def _ny_order_block(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session ICT/SMC Order-Block displacement continuation — 14:00-17:00 UTC.

    Source: TradingView NY ICT/SMC order-block + displacement strategies. On an
    Order Block during US RTH -> trade the displacement direction (bullish order
    block -> BUY, bearish -> SELL). Order-block-based (via
    ``_order_block_in_window``). Distinct entry model + window -> distinct
    culturing cell. The 31st entry model + new CANDLE-STRUCTURE dimension. For FX
    majors + US indices + oil + gold. Trial via symbols:
    ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm","XAUUSDm"].
    """
    return _order_block_in_window("ny_order_block", feat, ev, cfg,
                                  "NY Order Block — 14:00-17:00 UTC")


def _ha_in_window(setup_type: str, feat: dict, ev: dict,
                  cfg: dict[str, Any], reason_prefix: str) -> dict | None:
    """Shared Heikin-Ashi smoothed-candle strong-trend detector, session-gated
    (iter 42).

    Keys off ``feat.ha_trend`` (bullish_ha_strong = a green Heikin-Ashi smoothed
    candle with NO lower wick — HA_close > HA_open AND low >= HA_open — a strong
    smoothed uptrend; bearish_ha_strong = red HA with no upper wick). Inside a UTC
    window. Conf = avg(momentum, volume) — HA is a smoothed momentum-trend signal
    and volume confirms commitment, so the culturing ledger can isolate whether
    a SMOOTHED-PRICE-TRANSFORM wick-absence signal carries edge where the RAW
    close-streak run-length (iter 40), the 2-bar body-wrap engulf (iter 38), the
    displacement-magnitude order_block (iter 41), the 3-bar range-nesting inside
    bar (iter 39), and the EMA/supertrend/ichimoku state models do not.

    This is the 32ND entry model and a genuinely-new 6TH FAMILY: a PRICE TRANSFORM.
    No prior setup transforms the OHLC series before reading structure — they all
    read raw OHLC. HA replaces raw OHLC with a smoothed candle series (HA_open is
    a 0.5-alpha recursive average of prior HA_open+HA_close, dampening noise),
    then reads the smoothed candle's wick-absence for a strong-trend signal. The
    canonical TradingView "Heikin-Ashi smoothed candle" signal, a distinct entry
    model. A session-gated HA setup lands in a distinct culturing cell from every
    prior setup, so the ledger can distinguish per symbol whether the smoothed-
    transform wick-absence trend works where raw-candle and oscillator/state
    entries do not. Adding an HA setup is a one-liner wrapper.

    HONESTY: per-cell CI lo>0 is selection-biased across the full specialized-
    replay search (iter33-41 all confirmed; iter41 Order-Block 1/28 CI+ clean
    negative). DSR/SPA + OOS walk-forward across the (now larger K) search remains
    the bar; the per-cell numbers below are necessary-not-sufficient, NOT a
    deployable edge.
    """
    p = trigger_params(cfg, setup_type)
    if not _in_window(p):
        return None
    ha = feat.get("ha_trend")
    mom = float(ev.get("momentum", 0))
    vol = float(ev.get("volume", 0))
    if ha == "bullish_ha_strong":
        return _base(setup_type, "BUY", (mom + vol) / 2,
                     f"{reason_prefix} — bullish Heikin-Ashi (green smoothed candle, no lower wick -> strong smoothed uptrend continuation)")
    if ha == "bearish_ha_strong":
        return _base(setup_type, "SELL", (mom + vol) / 2,
                     f"{reason_prefix} — bearish Heikin-Ashi (red smoothed candle, no upper wick -> strong smoothed downtrend continuation)")
    return None


def _london_ha(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """London session Heikin-Ashi smoothed-candle strong-trend continuation — 07:00-10:00 UTC.

    Source: TradingView Heikin-Ashi smoothed-candle strategies (session
    killzones). On a bullish HA strong candle (green smoothed, no lower wick)
    inside the London killzone -> BUY continuation; bearish HA -> SELL.
    HA-based (via ``_ha_in_window``) — a different entry model from
    london_close_streak (raw-close run-length) and london_order_block (raw
    displacement) in the same window, so it lands in its own culturing cell. The
    32nd entry model and a new PRICE-TRANSFORM family (smoothed-candle wick-
    absence, distinct from all raw-OHLC entries). For FX majors + gold + EU
    indices. Trial via symbols: ["EURUSDm","GBPUSDm","USDCHFm","XAUUSDm","FR40m",
    "UK100m"].
    """
    return _ha_in_window("london_ha", feat, ev, cfg,
                         "London Heikin-Ashi — 07:00-10:00 UTC")


def _ny_ha(feat: dict, ctx: dict, ev: dict, cfg: dict[str, Any]) -> dict | None:
    """New York session Heikin-Ashi smoothed-candle strong-trend continuation — 14:00-17:00 UTC.

    Source: TradingView NY Heikin-Ashi smoothed-candle strategies. On an HA
    strong candle during US RTH -> trade the smoothed-trend direction (bullish
    HA -> BUY, bearish -> SELL). HA-based (via ``_ha_in_window``). Distinct entry
    model + window -> distinct culturing cell. The 32nd entry model + new
    PRICE-TRANSFORM family. For FX majors + US indices + oil + gold. Trial via
    symbols: ["EURUSDm","GBPUSDm","US30m","US500m","NAS100m","USOILm","XAUUSDm"].
    """
    return _ha_in_window("ny_ha", feat, ev, cfg,
                         "NY Heikin-Ashi — 14:00-17:00 UTC")


# Registry — append a SetupSpec here to add a specialized setup. Each iteration
# of the hourly evolution loop can add one more without touching the classifier.
SPECIALIZED_SETUPS: tuple[dict[str, Any], ...] = (
    {"name": "silver_bullet", "detect": _silver_bullet},
    {"name": "london_judas", "detect": _london_judas},
    # iteration 2 — symbol-specific breakout setups.
    {"name": "oil_orb", "detect": _oil_orb},
    {"name": "asia_range_breakout", "detect": _asia_range_breakout},
    # iteration 3 — US equity-index specialized setups.
    {"name": "us_open_orb", "detect": _us_open_orb},
    {"name": "prev_day_breakout", "detect": _prev_day_breakout},
    # iteration 4 — EU + Tokyo open gaps.
    {"name": "eu_open_orb", "detect": _eu_open_orb},
    {"name": "tokyo_open_orb", "detect": _tokyo_open_orb},
    # iteration 5 — London close reversal + Sydney open ORB.
    {"name": "london_close_reversal", "detect": _london_close_reversal},
    {"name": "sydney_open_orb", "detect": _sydney_open_orb},
    # iteration 6 — London morning breakout + NY lunch reversal.
    {"name": "london_morning_breakout", "detect": _london_morning_breakout},
    {"name": "ny_lunch_reversal", "detect": _ny_lunch_reversal},
    # iteration 7 — session-gated BB mean reversion (third entry model).
    {"name": "london_bb_reversion", "detect": _london_bb_reversion},
    {"name": "ny_bb_reversion", "detect": _ny_bb_reversion},
    # iteration 12 — session-gated stochastic-cross momentum (fourth entry
    # model, uses feat.stoch_cross — previously unused by specialized setups).
    {"name": "london_stoch_cross", "detect": _london_stoch_cross},
    {"name": "ny_stoch_cross", "detect": _ny_stoch_cross},
    # iteration 13 — session-gated volatility-expansion momentum (fifth entry
    # model, uses feat.volatility_regime as a primary key — previously unused).
    {"name": "london_vol_expansion", "detect": _london_vol_expansion},
    {"name": "ny_vol_expansion", "detect": _ny_vol_expansion},
    # iteration 14 — session-gated volume-spike confirmation (sixth entry model,
    # uses feat.volume_ratio as a primary key — previously unused by any setup).
    {"name": "london_volume_spike", "detect": _london_volume_spike},
    {"name": "ny_volume_spike", "detect": _ny_volume_spike},
    # iteration 15 — session-gated multi-timeframe alignment (seventh entry model,
    # uses feat.timeframe_alignment as a primary key — previously unused).
    {"name": "london_mtf_align", "detect": _london_mtf_align},
    {"name": "ny_mtf_align", "detect": _ny_mtf_align},
    # iteration 16 — session-gated stochastic oversold/overbought reversion
    # (eighth entry model, uses feat.stoch_k raw level — stoch_cross used the
    # cross event, not the level; this is a mean-reversion entry).
    {"name": "london_stoch_reversion", "detect": _london_stoch_reversion},
    {"name": "ny_stoch_reversion", "detect": _ny_stoch_reversion},
    # iteration 18 — session-gated higher-timeframe-trend-filtered breakout
    # (ninth entry model, uses feat.m15_trend DIRECTION as a primary key —
    # timeframe_alignment used the m5==m15 boolean match, not m15 direction
    # alone; this filters breakouts by HTF trend direction).
    {"name": "london_htf_breakout", "detect": _london_htf_breakout},
    {"name": "ny_htf_breakout", "detect": _ny_htf_breakout},
    # iteration 19 — session-gated BB-squeeze-release breakout (tenth entry
    # model, uses feat.bb_squeeze_pct — a NEW rolling-compression feature
    # exposed this iteration; no prior setup used a rolling squeeze baseline).
    {"name": "london_squeeze_breakout", "detect": _london_squeeze_breakout},
    {"name": "ny_squeeze_breakout", "detect": _ny_squeeze_breakout},
    # iteration 20 — session-gated volume-confirmed breakout (eleventh entry
    # model, uses the volume_ratio + breakout STATE compound — volume_spike
    # used volume_ratio + m5_trend, not the breakout state).
    {"name": "london_volume_breakout", "detect": _london_volume_breakout},
    {"name": "ny_volume_breakout", "detect": _ny_volume_breakout},
    # iteration 21 — session-gated RSI oversold/overbought mean reversion
    # (twelfth entry model, uses feat.rsi RAW LEVEL — a NEW Wilder's RSI(14)
    # feature exposed this iteration; stoch_reversion used stoch_k, a different
    # oscillator. Tests whether RSI avoids the "stoch stays overbought in a
    # trend" trap that made stoch_reversion a broad per-symbol loser).
    {"name": "london_rsi_reversion", "detect": _london_rsi_reversion},
    {"name": "ny_rsi_reversion", "detect": _ny_rsi_reversion},
    # iteration 22 — session-gated MACD signal-line-cross momentum continuation
    # (thirteenth entry model, uses feat.macd_cross — a NEW Wilder-style
    # smoothed-momentum oscillator feature exposed this iteration; stoch_cross
    # used a price-range oscillator cross, a different oscillator. Tests whether
    # a smoothed-momentum cross works per symbol where a price-range cross
    # didn't).
    {"name": "london_macd_cross", "detect": _london_macd_cross},
    {"name": "ny_macd_cross", "detect": _ny_macd_cross},
    # iteration 23 — session-gated CCI oversold/overbought mean reversion
    # (fourteenth entry model, uses feat.cci raw level — a NEW CCI(20)
    # price-deviation-from-MA oscillator feature exposed this iteration; RSI
    # used a gain/loss ratio, stoch a price-range position. Tests whether a
    # price-deviation fade works per symbol where RSI/stoch fades didn't).
    {"name": "london_cci_reversion", "detect": _london_cci_reversion},
    {"name": "ny_cci_reversion", "detect": _ny_cci_reversion},
    # iteration 24 — session-gated MFI oversold/overbought mean reversion
    # (fifteenth entry model, uses feat.mfi raw level — a NEW MFI(14)
    # VOLUME-WEIGHTED oscillator feature exposed this iteration; the ONLY
    # oscillator here that folds in volume via the money-flow ratio. RSI/CCI/
    # stoch/MACD are all pure-price. Tests whether a volume-weighted fade
    # distinguishes volume-informative symbols where the pure-price fades
    # didn't).
    {"name": "london_mfi_reversion", "detect": _london_mfi_reversion},
    {"name": "ny_mfi_reversion", "detect": _ny_mfi_reversion},
    # iteration 25 — session-gated ADX trend-strength continuation (sixteenth
    # entry model, uses feat.adx + feat.di_plus/di_minus — NEW Wilder DMI
    # trend-STRENGTH feature exposed this iteration; the only strength
    # dimension here. The 15 prior models use direction/reversion/volume, none
    # measure trend strength. Tests whether a strength filter (ADX >= 25 strong
    # trend + DI direction) works where the directional-only trend models
    # didn't).
    {"name": "london_adx_trend", "detect": _london_adx_trend},
    {"name": "ny_adx_trend", "detect": _ny_adx_trend},
    # iteration 26 — session-gated OBV-EMA-cross volume-accumulation
    # continuation (17th entry model, uses feat.obv_cross — a NEW On-Balance
    # Volume cumulative signed-volume line crossing its EMA(20); the only
    # volume-ACCUMULATION dimension here, distinct from volume_spike/
    # volume_breakout single-bar and mfi windowed-ratio).
    {"name": "london_obv_cross", "detect": _london_obv_cross},
    {"name": "ny_obv_cross", "detect": _ny_obv_cross},
    # iteration 27 — session-gated ATR-percentile-breakout relative-vol
    # expansion (18th entry model, uses feat.atr_pct — a NEW ATR(14) rolling
    # 100-bar percentile-rank feature; the only relative-volatility-RANK
    # dimension here, distinct from vol_expansion's absolute atr_ratio gate).
    {"name": "london_atr_pct_breakout", "detect": _london_atr_pct_breakout},
    {"name": "ny_atr_pct_breakout", "detect": _ny_atr_pct_breakout},
    {"name": "london_cmf_continuation", "detect": _london_cmf_continuation},
    {"name": "ny_cmf_continuation", "detect": _ny_cmf_continuation},
    {"name": "london_triple_confirm", "detect": _london_triple_confirm},
    {"name": "ny_triple_confirm", "detect": _ny_triple_confirm},
    {"name": "london_adx_cmf", "detect": _london_adx_cmf},
    {"name": "ny_adx_cmf", "detect": _ny_adx_cmf},
    # iteration 31 — session-gated ADX+ATR-pct 2-way compound (22nd entry model,
    # drops cmf from the triple; attribution complement to iter30's adx×cmf).
    {"name": "london_adx_atr_pct", "detect": _london_adx_atr_pct},
    {"name": "ny_adx_atr_pct", "detect": _ny_adx_atr_pct},
    # iteration 32 — session-gated ATR-pct+CMF 2-way compound (23rd entry model,
    # final compound pair: drops adx from the triple; completes the full 2-way
    # attribution matrix).
    {"name": "london_atr_pct_cmf", "detect": _london_atr_pct_cmf},
    {"name": "ny_atr_pct_cmf", "detect": _ny_atr_pct_cmf},
    {"name": "london_vwap_reversion", "detect": _london_vwap_reversion},
    {"name": "ny_vwap_reversion", "detect": _ny_vwap_reversion},
    {"name": "london_supertrend_flip", "detect": _london_supertrend_flip},
    {"name": "ny_supertrend_flip", "detect": _ny_supertrend_flip},
    {"name": "london_ichimoku_tk_cross", "detect": _london_ichimoku_tk_cross},
    {"name": "ny_ichimoku_tk_cross", "detect": _ny_ichimoku_tk_cross},
    {"name": "london_fvg", "detect": _london_fvg},
    {"name": "ny_fvg", "detect": _ny_fvg},
    {"name": "london_engulfing", "detect": _london_engulfing},
    {"name": "ny_engulfing", "detect": _ny_engulfing},
    {"name": "london_inside_bar", "detect": _london_inside_bar},
    {"name": "ny_inside_bar", "detect": _ny_inside_bar},
    {"name": "london_close_streak", "detect": _london_close_streak},
    {"name": "ny_close_streak", "detect": _ny_close_streak},
    {"name": "london_order_block", "detect": _london_order_block},
    {"name": "ny_order_block", "detect": _ny_order_block},
    {"name": "london_ha", "detect": _london_ha},
    {"name": "ny_ha", "detect": _ny_ha},
)


def detect_specialized(
    feat: dict[str, Any],
    ctx: dict[str, Any],
    ev: dict[str, Any],
    config: dict[str, Any],
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """Return all specialized-setup candidates that fire this bar.

    Called by SetupClassifier. Each candidate matches the classifier's candidate
    dict shape so it drops straight into the existing regime-filter +
    min-confidence + ranking path.

    Modes (all opt-in via signals.specialized_setups):
      * enabled=false, shadow=false -> nothing (no emit, no log).
      * enabled=true,  shadow=false -> emit candidates for trading (the default
        live path once an operator signs off).
      * enabled=false, shadow=true  -> do NOT emit (no orders placed), but
        append every fire to state/specialized_shadow_ledger.jsonl so per-symbol
        fire evidence accumulates safely pre-trading.
      * enabled=true,  shadow=true  -> emit AND log.
    """
    sa = specialized_config(config)
    if not sa["enabled"] and not sa["shadow"]:
        return []
    symbol = feat.get("symbol") or ctx.get("symbol")
    if sa["symbols"] and symbol and symbol not in sa["symbols"]:
        return []
    allowed = set(sa["setups"]) if sa["setups"] else None
    # Per-symbol granular allowlist (iteration 6): if this symbol is keyed,
    # only its listed setups may fire here (on top of the global gates).
    per_symbol_allowed = sa["per_symbol"].get(symbol) if symbol else None
    per_symbol_set = set(per_symbol_allowed) if per_symbol_allowed else None
    # Replay-evidence per-symbol veto (iteration 12): if apply_replay_veto is on
    # and this symbol has vetoed setups, skip them. Reliable losers blocked.
    replay_veto = sa.get("replay_veto") or {}
    vetoed = set(replay_veto.get(symbol, [])) if symbol else set()
    out: list[dict[str, Any]] = []
    for spec in SPECIALIZED_SETUPS:
        if allowed is not None and spec["name"] not in allowed:
            continue
        if per_symbol_set is not None and spec["name"] not in per_symbol_set:
            continue
        if spec["name"] in vetoed:
            continue
        try:
            cand = spec["detect"](feat, ctx, ev, config)
        except Exception as exc:  # noqa: BLE001 — never break the pipeline
            (logger or logging.getLogger("specialized_setups")).warning(
                "specialized setup %s detector raised: %s", spec["name"], exc
            )
            continue
        if cand:
            out.append(cand)
    # Shadow fire-ledger (iteration 8): log every fire, even when not emitting.
    if sa["shadow"]:
        _shadow_log(out, feat, ctx)
    if not sa["enabled"]:
        return []  # shadow-only: logged, not emitted for trading (no orders)
    return out