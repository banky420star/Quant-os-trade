"""Setup trigger catalog — all 8 arena strategies with tunable conditions.

Single source of truth for:
  * SetupClassifier threshold params (read via ``trigger_params``)
  * Arena trigger logging (``snapshot_trigger_context``)
  * TUI / evaluator / optimizer (``export_catalog``, ``SETUP_ORDER``)
"""

from __future__ import annotations

from typing import Any

from core.setup_library import SETUP_LIBRARY
from core.utils import utc_now_iso, write_json_state

SETUP_ORDER = (
    "trend_continuation",
    "pullback",
    "breakout",
    "compression_breakout",
    "range_fade",
    "liquidity_sweep",
    "mean_reversion",
    "false_breakout",
    # 2026-07-31 — specialized ICT/SMC killzone setups (opt-in, see
    # signals.specialized_setups in config). Detection lives in
    # core/specialized_setups.py; these entries make them first-class for the
    # catalog, trigger params, and regime compatibility.
    "silver_bullet",
    "london_judas",
    # 2026-07-31 iteration 2 — symbol-specific breakout setups.
    "oil_orb",
    "asia_range_breakout",
    # iteration 3 — US equity-index specialized setups.
    "us_open_orb",
    "prev_day_breakout",
    # iteration 4 — EU + Tokyo open gaps.
    "eu_open_orb",
    "tokyo_open_orb",
    # iteration 5 — London close reversal + Sydney open ORB.
    "london_close_reversal",
    "sydney_open_orb",
    # iteration 6 — London morning breakout + NY lunch reversal.
    "london_morning_breakout",
    "ny_lunch_reversal",
    # iteration 7 — session-gated BB mean reversion (third entry model).
    "london_bb_reversion",
    "ny_bb_reversion",
    # iteration 12 — session-gated stochastic-cross momentum (fourth entry model).
    "london_stoch_cross",
    "ny_stoch_cross",
    # iteration 13 — session-gated volatility-expansion momentum (fifth entry model).
    "london_vol_expansion",
    "ny_vol_expansion",
    # iteration 14 — session-gated volume-spike confirmation (sixth entry model).
    "london_volume_spike",
    "ny_volume_spike",
    # iteration 15 — session-gated multi-timeframe alignment (seventh entry model).
    "london_mtf_align",
    "ny_mtf_align",
    # iteration 16 — session-gated stochastic oversold/overbought reversion
    # (eighth entry model).
    "london_stoch_reversion",
    "ny_stoch_reversion",
    # iteration 18 — session-gated higher-timeframe-trend-filtered breakout
    # (ninth entry model, uses feat.m15_trend DIRECTION as a primary key).
    "london_htf_breakout",
    "ny_htf_breakout",
    # iteration 19 — session-gated BB-squeeze-release breakout (tenth entry
    # model, uses feat.bb_squeeze_pct — a new rolling-compression feature).
    "london_squeeze_breakout",
    "ny_squeeze_breakout",
    # iteration 20 — session-gated volume-confirmed breakout (eleventh entry
    # model, uses the volume_ratio + breakout STATE compound).
    "london_volume_breakout",
    "ny_volume_breakout",
    # iteration 21 — session-gated RSI oversold/overbought mean reversion
    # (twelfth entry model, uses feat.rsi raw level — a new Wilder's RSI(14)
    # feature; distinct oscillator from stoch_reversion).
    "london_rsi_reversion",
    "ny_rsi_reversion",
    # iteration 22 — session-gated MACD signal-line-cross momentum continuation
    # (thirteenth entry model, uses feat.macd_cross — a new smoothed-momentum
    # oscillator feature; distinct oscillator from stoch_cross).
    "london_macd_cross",
    "ny_macd_cross",
    # iteration 23 — session-gated CCI oversold/overbought mean reversion
    # (fourteenth entry model, uses feat.cci raw level — a new CCI(20)
    # price-deviation-from-MA oscillator feature; distinct oscillator from
    # rsi/stoch reversion).
    "london_cci_reversion",
    "ny_cci_reversion",
    # iteration 24 — session-gated MFI oversold/overbought mean reversion
    # (fifteenth entry model, uses feat.mfi raw level — a new MFI(14)
    # VOLUME-WEIGHTED oscillator feature; the only oscillator here that folds
    # in volume. distinct from rsi/cci/stoch reversion, which are pure-price).
    "london_mfi_reversion",
    "ny_mfi_reversion",
    # iteration 25 — session-gated ADX trend-strength continuation (16th entry
    # model, uses feat.adx + feat.di_plus/di_minus — a new Wilder DMI
    # trend-STRENGTH feature; the only strength dimension here. distinct from
    # the directional trend models mtf_align/htf_breakout/vol_expansion).
    "london_adx_trend",
    "ny_adx_trend",
    # iteration 26 — session-gated OBV-EMA-cross volume-accumulation
    # continuation (17th entry model, uses feat.obv_cross — a new On-Balance
    # Volume cumulative signed-volume line crossing its EMA(20); the only
    # volume-ACCUMULATION dimension here, distinct from volume_spike/
    # volume_breakout single-bar and mfi windowed-ratio).
    "london_obv_cross",
    "ny_obv_cross",
    # iteration 27 — session-gated ATR-percentile-breakout relative-vol
    # expansion (18th entry model, uses feat.atr_pct — a new ATR(14) rolling
    # 100-bar percentile-rank feature; the only relative-volatility-RANK
    # dimension here, distinct from vol_expansion's absolute atr_ratio gate).
    "london_atr_pct_breakout",
    "ny_atr_pct_breakout",
    # iteration 28 — session-gated Chaikin-Money-Flow-confirmed continuation
    # (19th entry model, uses feat.cmf — a new CMF(20) intrabar close-location
    # * volume pressure feature; the 4th volume dimension here, distinct from
    # volume_ratio level / mfi price-change ratio / obv cumulative line).
    "london_cmf_continuation",
    "ny_cmf_continuation",
    # iteration 29 — session-gated triple-confirmation momentum continuation
    # (20th entry model, a 3-WAY COMPOUND: the conjunction of the three
    # strongest per-symbol-positive dimensions from iter25/27/28 — ADX trend
    # strength + atr_pct relative-vol rank + cmf intrabar money-flow pressure —
    # is the primary key, distinct from any single-dim setup).
    "london_triple_confirm",
    "ny_triple_confirm",
    # iteration 30 — session-gated ADX+CMF 2-way-compound continuation
    # (21st entry model, the 2nd compound: drops the atr_pct gate from the
    # iter29 triple to isolate whether strength + money-flow pressure alone
    # carry the EUR/GBP edge, or atr_pct adds independent signal).
    "london_adx_cmf",
    "ny_adx_cmf",
    # iter31 — session-gated ADX+ATR-pct 2-way compound (22nd entry model, drops
    # cmf from the triple; attribution complement to iter30's adx×cmf).
    "london_adx_atr_pct",
    "ny_adx_atr_pct",
    # iter32 — session-gated ATR-pct+CMF 2-way compound (23rd entry model, final
    # compound pair: drops adx from the triple; completes the full 2-way
    # attribution matrix).
    "london_atr_pct_cmf",
    "ny_atr_pct_cmf",
    # iteration 34 — session-gated VWAP-band mean reversion (24th entry model;
    # 5th volume dim = volume-weighted price anchor, distinct from BB).
    "london_vwap_reversion",
    "ny_vwap_reversion",
    # iteration 35 — session-gated Supertrend-flip trend continuation (25th
    # entry model; new trend-STATE dim = ATR-band flip, distinct from EMA + ADX).
    "london_supertrend_flip",
    "ny_supertrend_flip",
    # iteration 36 — session-gated Ichimoku Tenkan/Kijun-cross continuation (26th
    # entry model; new EQUILIBRIUM dim = rolling high-low midpoint cross confirmed
    # by cloud position, distinct from EMA direction + ADX strength + ATR-band
    # state + VWAP volume-weighted anchor + close-vs-range oscillators).
    "london_ichimoku_tk_cross",
    "ny_ichimoku_tk_cross",
    # iteration 37 — session-gated Fair Value Gap imbalance continuation (27th
    # entry model; new PRICE-IMBALANCE dim = non-adjacent 3-bar ICT/SMC gap gated
    # by ATR-relative size, distinct from all 1-bar-S/R + continuous-structure setups).
    "london_fvg",
    "ny_fvg",
    # iteration 38 — session-gated 2-bar Engulfing candlestick reversal (28th
    # entry model; new CANDLE-STRUCTURE dim = 2-bar body-vs-body engulf, distinct
    # from 1-bar wick rejection + 3-bar FVG gap).
    "london_engulfing",
    "ny_engulfing",
    # iteration 39 — session-gated 3-bar Inside-Bar breakout (29th entry
    # model; new CANDLE-STRUCTURE dim = 2-3-bar range-nesting then expansion,
    # distinct from 1-bar wick rejection + 2-bar body engulf + 3-bar FVG gap).
    "london_inside_bar",
    "ny_inside_bar",
    # iteration 40 — session-gated consecutive-close-streak continuation (30th
    # entry model; new STATISTICAL dim = run-length of consecutive same-
    # direction closes, distinct from EMA-state/ADX-strength/candle-patterns).
    "london_close_streak",
    "ny_close_streak",
    # iteration 41 — session-gated ICT/SMC Order-Block displacement continuation
    # (31st entry model; new CANDLE-STRUCTURE dim = opposite-origin + impulse-
    # magnitude body >= 0.8*ATR, distinct from body-wrap engulf / range-nesting
    # inside-bar / 3-bar FVG gap / run-length close-streak).
    "london_order_block",
    "ny_order_block",
    # iteration 42 — session-gated Heikin-Ashi smoothed-candle strong-trend
    # continuation (32nd entry model; new PRICE-TRANSFORM family = smoothed OHLC
    # candle wick-absence, distinct from all raw-OHLC entries).
    "london_ha",
    "ny_ha",
)

# Default trigger params — overridden by config intelligence.setup_triggers.
_TRIGGER_DEFAULTS: dict[str, dict[str, Any]] = {
    "trend_continuation": {
        "regime": "trending",
        "move_type": "continuation",
    },
    "pullback": {
        "move_types": ("pullback",),
        "retest_breakouts": ("breakout_retest", "breakdown_retest"),
        "allow_compression_setup": True,
    },
    "breakout": {
        "breakout_states": ("breakout", "breakdown"),
    },
    "compression_breakout": {
        "phase": "compression",
        "breakout_states": ("breakout", "breakdown"),
    },
    "range_fade": {
        "regime": "ranging",
        "boundary_pct": 0.0015,
    },
    "liquidity_sweep": {
        "min_liquidity": 0.65,
        "rejections": ("bullish_rejection", "bearish_rejection"),
    },
    "mean_reversion": {
        "bb_upper": 0.92,
        "bb_lower": 0.08,
    },
    "false_breakout": {
        "bb_upper": 0.85,
        "bb_lower": 0.15,
    },
    # 2026-07-31 — specialized ICT/SMC killzone setups.
    "silver_bullet": {
        # 14:00-15:00 UTC ICT Silver Bullet window (inclusive).
        "utc_hour_start": 14,
        "utc_hour_end": 15,
        "min_liquidity": 0.60,
    },
    "london_judas": {
        # 07:00-10:00 UTC London Open killzone (inclusive start, exclusive end).
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_liquidity": 0.60,
    },
    # 2026-07-31 iteration 2 — symbol-specific breakout setups.
    "oil_orb": {
        # 14:00-19:00 UTC = 9:00 AM ET opening range through 2:00 PM ET
        # primary execution (NexusFi CL Time Map). Inclusive start, exclusive end.
        "utc_hour_start": 14,
        "utc_hour_end": 19,
        "breakout_states": ("breakout", "breakdown"),
    },
    "asia_range_breakout": {
        # 07:00-10:00 UTC London open breaks the Asian session range.
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "breakout_states": ("breakout", "breakdown"),
    },
    # 2026-07-31 iteration 3 — US equity-index specialized setups.
    "us_open_orb": {
        # 14:00-16:00 UTC = 9:30 AM ET RTH open first ~90 min (best window
        # 09:35-10:15 ET per Fazen/Vortex research).
        "utc_hour_start": 14,
        "utc_hour_end": 16,
        "breakout_states": ("breakout", "breakdown"),
    },
    "prev_day_breakout": {
        # 14:00-21:00 UTC = US RTH session (9:30 AM-4:00 PM ET). Close beyond
        # prev day high/low. One per day, NY reset (enforced by culturing cell).
        "utc_hour_start": 14,
        "utc_hour_end": 21,
        "breakout_states": ("breakout", "breakdown"),
    },
    # 2026-07-31 iteration 4 — EU + Tokyo open gaps.
    "eu_open_orb": {
        # 08:00-10:00 UTC = London-Frankfurt synchronized cash open (best
        # window per Fazen DAX research).
        "utc_hour_start": 8,
        "utc_hour_end": 10,
        "breakout_states": ("breakout", "breakdown"),
    },
    "tokyo_open_orb": {
        # 00:00-02:00 UTC = Tokyo open first hour (highest JPY volatility per
        # USDJPY session research).
        "utc_hour_start": 0,
        "utc_hour_end": 2,
        "breakout_states": ("breakout", "breakdown"),
    },
    # 2026-07-31 iteration 5 — London close reversal + Sydney open ORB.
    "london_close_reversal": {
        # 15:00-17:00 UTC = ICT London Close killzone (London close 16:00-17:00
        # UTC). Rejection-based (uses min_liquidity, no breakout_states).
        "utc_hour_start": 15,
        "utc_hour_end": 17,
        "min_liquidity": 0.60,
    },
    "sydney_open_orb": {
        # 21:00-23:00 UTC = Sydney open (~10 PM London), Asia week-open. First
        # major FX centre; AUD leg most volatile here.
        "utc_hour_start": 21,
        "utc_hour_end": 23,
        "breakout_states": ("breakout", "breakdown"),
    },
    # 2026-07-31 iteration 6 — London morning breakout + NY lunch reversal.
    "london_morning_breakout": {
        # 10:00-14:00 UTC = London morning (post-open noise 07-10 settles, trends
        # into NY open 14:00). Breakout-based.
        "utc_hour_start": 10,
        "utc_hour_end": 14,
        "breakout_states": ("breakout", "breakdown"),
    },
    "ny_lunch_reversal": {
        # 17:00-19:00 UTC = NY lunch lull (12:00-14:00 ET). Low vol, fade of
        # morning extreme. Rejection-based (uses min_liquidity, no breakout_states).
        "utc_hour_start": 17,
        "utc_hour_end": 19,
        "min_liquidity": 0.60,
    },
    # 2026-07-31 iteration 7 — session-gated BB mean reversion (third entry model).
    "london_bb_reversion": {
        # 07:00-10:00 UTC London window. BB-based (uses bb_upper/bb_lower, no
        # breakout_states / min_liquidity). Tighter bands than legacy mean_reversion
        # (0.92/0.08) so it fires more often in-session -> distinct culturing cell.
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "bb_upper": 0.85,
        "bb_lower": 0.15,
    },
    "ny_bb_reversion": {
        # 14:00-17:00 UTC US RTH. BB-based.
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "bb_upper": 0.85,
        "bb_lower": 0.15,
    },
    # iteration 12 — session-gated stochastic-cross momentum (4th entry model,
    # uses feat.stoch_cross). min_momentum gate so a flat-market cross is skipped.
    "london_stoch_cross": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_momentum": 0.50,
    },
    "ny_stoch_cross": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "min_momentum": 0.50,
    },
    # iteration 13 — volatility-expansion (5th entry model, uses
    # feat.volatility_regime=="high" + m5_trend direction). min_volatility gate
    # so a low-vol regime expansion is skipped.
    "london_vol_expansion": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_volatility": 0.50,
    },
    "ny_vol_expansion": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "min_volatility": 0.50,
    },
    # iteration 14 — volume-spike confirmation (6th entry model, uses
    # feat.volume_ratio as a primary key). min_volume_ratio gate so a flat-volume
    # bar is skipped (only spike bars confirm a move).
    "london_volume_spike": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_volume_ratio": 1.5,
    },
    "ny_volume_spike": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "min_volume_ratio": 1.5,
    },
    # iteration 15 — multi-timeframe alignment (7th entry model, uses
    # feat.timeframe_alignment boolean as a primary key). min_trend gate so a
    # weak/flat aligned trend is skipped (only meaningful HTF confluence fires).
    "london_mtf_align": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_trend": 0.50,
    },
    "ny_mtf_align": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "min_trend": 0.50,
    },
    # iteration 16 — stochastic oversold/overbought reversion (8th entry model,
    # uses feat.stoch_k raw level — stoch_cross used the cross event). Standard
    # 20/80 oversold/overbought thresholds; tunable per config.
    "london_stoch_reversion": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "oversold": 20,
        "overbought": 80,
    },
    "ny_stoch_reversion": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "oversold": 20,
        "overbought": 80,
    },
    # iteration 18 — higher-timeframe-trend-filtered breakout (9th entry model,
    # uses feat.m15_trend DIRECTION — timeframe_alignment used the m5==m15
    # boolean match, not m15 direction alone). Session window only; the breakout
    # state + m15_trend direction are read from features directly.
    "london_htf_breakout": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
    },
    "ny_htf_breakout": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
    },
    # iteration 19 — BB-squeeze-release breakout (10th entry model, uses
    # feat.bb_squeeze_pct — the percentile rank of BB bandwidth within the
    # prior 50 bars; 0 = tightest squeeze). max_squeeze_pct is the compression
    # gate (fire only when current bandwidth is in the tightest 20% of last 50).
    "london_squeeze_breakout": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "max_squeeze_pct": 0.20,
    },
    "ny_squeeze_breakout": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "max_squeeze_pct": 0.20,
    },
    # iteration 20 — volume-confirmed breakout (11th entry model, uses the
    # volume_ratio + breakout STATE compound; volume_spike used volume_ratio +
    # m5_trend, not the breakout state). min_volume_ratio 1.5x avg tick-volume.
    "london_volume_breakout": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_volume_ratio": 1.5,
    },
    "ny_volume_breakout": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "min_volume_ratio": 1.5,
    },
    # iteration 21 — RSI oversold/overbought mean reversion (12th entry model,
    # uses feat.rsi raw level — Wilder's RSI(14), a new feature; stoch_reversion
    # used stoch_k, a different oscillator). Classic 30/70 thresholds; tunable.
    "london_rsi_reversion": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "oversold": 30,
        "overbought": 70,
    },
    "ny_rsi_reversion": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "oversold": 30,
        "overbought": 70,
    },
    # iteration 22 — MACD signal-line-cross momentum continuation (13th entry
    # model, uses feat.macd_cross — Wilder-style smoothed-momentum oscillator
    # cross; stoch_cross used a price-range oscillator cross). min_momentum gate
    # so a flat-momentum cross is skipped (mirrors stoch_cross).
    "london_macd_cross": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_momentum": 0.50,
    },
    "ny_macd_cross": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "min_momentum": 0.50,
    },
    # iteration 23 — CCI oversold/overbought mean reversion (14th entry model,
    # uses feat.cci raw level — CCI(20) price-deviation-from-MA oscillator; rsi
    # used a gain/loss ratio, stoch a price-range position). Classic +/-100
    # thresholds (distinct scale from RSI 30/70 and stoch 20/80).
    "london_cci_reversion": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "oversold": -100,
        "overbought": 100,
    },
    "ny_cci_reversion": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "oversold": -100,
        "overbought": 100,
    },
    # iteration 24 — MFI oversold/overbought mean reversion (15th entry model,
    # uses feat.mfi raw level — MFI(14) volume-weighted oscillator; rsi used a
    # pure-price gain/loss ratio, cci a price-deviation from MA, stoch a
    # price-range position. MFI is the only one that folds in volume). Classic
    # 20/80 thresholds (same scale as stoch; distinct from RSI 30/70, CCI +/-100).
    "london_mfi_reversion": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "oversold": 20,
        "overbought": 80,
    },
    "ny_mfi_reversion": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "oversold": 20,
        "overbought": 80,
    },
    # iteration 25 — ADX/DI trend-strength continuation. Wilder DMI(14):
    # adx >= 25 = strong trend, then +DI > -DI → BUY / -DI > +DI → SELL
    # (strength-filtered continuation, distinct from the directional trend
    # models which fire on alignment/breakout without a strength gate).
    "london_adx_trend": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_adx": 25,
    },
    "ny_adx_trend": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "min_adx": 25,
    },
    # iteration 26 — OBV-EMA(20) cross volume-accumulation continuation.
    # OBV (cumulative signed volume) crossing its EMA(20): bullish_cross → BUY
    # (net accumulation), bearish_cross → SELL (net distribution). The only
    # volume-ACCUMULATION dimension (distinct from volume_spike/volume_breakout
    # single-bar and mfi windowed-ratio). EMA period fixed at 20 in the feature
    # engine; only the UTC window is tunable here.
    "london_obv_cross": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
    },
    "ny_obv_cross": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
    },
    # iteration 27 — ATR-percentile-breakout relative-vol expansion. ATR(14)'s
    # percentile rank within its own rolling 100-bar history >= min_atr_pct
    # (0.70 classic "high-relative-vol") -> relative-vol expanding -> trade
    # the M5-trend direction. The only relative-volatility-RANK dimension
    # (distinct from vol_expansion's absolute atr_ratio gate). ATR period +
    # lookback fixed at 14/100 in the feature engine; window + threshold here.
    "london_atr_pct_breakout": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_atr_pct": 0.70,
    },
    "ny_atr_pct_breakout": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "min_atr_pct": 0.70,
    },
    # iteration 28 — Chaikin-Money-Flow-confirmed continuation. CMF(20) =
    # sum(money-flow-multiplier * volume) / sum(volume), bounded [-1, +1],
    # where the multiplier = (2*close-high-low)/(high-low) = intrabar close
    # location. cmf >= +min_cmf (0.10 classic accumulation) + bullish M5 trend
    # -> BUY; cmf <= -min_cmf (distribution) + bearish -> SELL. The 4th volume
    # dimension (intrabar close-location * volume pressure, distinct from
    # volume_ratio level / mfi price-change ratio / obv cumulative line). CMF
    # period fixed at 20 in the feature engine; window + threshold here.
    "london_cmf_continuation": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_cmf": 0.10,
    },
    "ny_cmf_continuation": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "min_cmf": 0.10,
    },
    # iteration 29 — triple-confirmation momentum continuation. A 3-WAY
    # COMPOUND: fires only when ADX(14) >= min_adx (25 strong trend) AND
    # atr_pct >= min_atr_pct (0.70 relative-vol expanding) AND cmf agrees with
    # the M5 trend direction (cmf >= +min_cmf 0.10 accumulation when bullish,
    # <= -min_cmf distribution when bearish) — all three at once. The
    # conjunction of the three strongest per-symbol-positive dimensions from
    # iter25/27/28 is the primary key. ADX/ATR-period/CMF-period fixed in the
    # feature engine; window + thresholds here.
    "london_triple_confirm": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_adx": 25,
        "min_atr_pct": 0.70,
        "min_cmf": 0.10,
    },
    "ny_triple_confirm": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "min_adx": 25,
        "min_atr_pct": 0.70,
        "min_cmf": 0.10,
    },
    # iteration 30 — ADX+CMF 2-way-compound continuation. Fires when ADX(14) >=
    # min_adx (25 strong trend) AND cmf agrees with the M5 trend direction
    # (cmf >= +min_cmf 0.10 accumulation when bullish, <= -min_cmf distribution
    # when bearish) — both at once. DROPS the atr_pct gate from iter29's triple
    # to isolate whether strength + money-flow pressure alone carry the edge.
    "london_adx_cmf": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_adx": 25,
        "min_cmf": 0.10,
    },
    "ny_adx_cmf": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "min_adx": 25,
        "min_cmf": 0.10,
    },
    # iter31 — ADX+ATR-pct 2-way compound: adx >= min_adx (25 strong trend) AND
    # atr_pct >= min_atr_pct (0.70 relative-vol expanding) AND M5 directional.
    # DROPS the cmf gate from iter29's triple — attribution complement to
    # iter30's adx×cmf (which dropped atr_pct). Tests whether strength+vol-rank
    # WITHOUT money-flow pressure carry the EUR/GBP edge.
    "london_adx_atr_pct": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_adx": 25,
        "min_atr_pct": 0.70,
    },
    "ny_adx_atr_pct": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "min_adx": 25,
        "min_atr_pct": 0.70,
    },
    # iter32 — ATR-pct+CMF 2-way compound: atr_pct >= min_atr_pct (0.70 relative-
    # vol expanding) AND cmf agrees with the M5 trend direction (cmf >= +min_cmf
    # 0.10 accumulation when bullish, <= -min_cmf distribution when bearish) AND
    # M5 directional. DROPS the adx gate from iter29's triple — final compound
    # pair, completes the full 2-way attribution matrix. Tests whether vol-rank
    # + money-flow WITHOUT trend-strength carry the edge (expected to degrade).
    "london_atr_pct_cmf": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_atr_pct": 0.70,
        "min_cmf": 0.10,
    },
    "ny_atr_pct_cmf": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "min_atr_pct": 0.70,
        "min_cmf": 0.10,
    },
    # iteration 34 — VWAP-band reversion (24th entry model; volume-weighted price
    # anchor). vwap_position is the close's location within the rolling VWAP
    # +/-1.5sigma bands in [0,1]; fade the band extreme back to VWAP.
    "london_vwap_reversion": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "vwap_upper": 0.92,
        "vwap_lower": 0.08,
    },
    "ny_vwap_reversion": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "vwap_upper": 0.92,
        "vwap_lower": 0.08,
    },
    # iteration 35 — Supertrend-flip continuation (25th entry model; ATR-band
    # trend-STATE flip). Fires on the supertrend_flip event (bullish/bearish).
    "london_supertrend_flip": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "period": 10,
        "multiplier": 3.0,
    },
    "ny_supertrend_flip": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "period": 10,
        "multiplier": 3.0,
    },
    # iteration 36 — Ichimoku Tenkan/Kijun-cross continuation (26th entry model;
    # rolling high-low midpoint EQUILIBRIUM cross confirmed by cloud position).
    # Fires on the ichimoku_tk_cross event aligned with ichimoku_cloud_position.
    "london_ichimoku_tk_cross": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "tenkan_period": 9,
        "kijun_period": 26,
        "senkou_b_period": 52,
    },
    "ny_ichimoku_tk_cross": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "tenkan_period": 9,
        "kijun_period": 26,
        "senkou_b_period": 52,
    },
    # iteration 37 — FVG imbalance continuation (27th entry model; non-adjacent
    # 3-bar ICT/SMC structural gap). Fires on the fvg event gated by min_size_atr.
    "london_fvg": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_size_atr": 0.25,
    },
    "ny_fvg": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "min_size_atr": 0.25,
    },
    # iteration 38 — Engulfing candlestick reversal (28th entry model; 2-bar
    # body-vs-body engulf). Fires on the engulfing event (bullish/bearish).
    "london_engulfing": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
    },
    "ny_engulfing": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
    },
    # iteration 39 — Inside-Bar breakout (29th entry model; 3-bar range-nesting
    # then expansion). Fires on the inside-bar breakout event (bullish/bearish).
    "london_inside_bar": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
    },
    "ny_inside_bar": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
    },
    # iteration 40 — Close-streak continuation (30th entry model; run-length of
    # consecutive same-direction closes). Fires when |streak| >= min_streak.
    "london_close_streak": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_streak": 3,
    },
    "ny_close_streak": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "min_streak": 3,
    },
    # iteration 41 — Order-Block displacement continuation (31st entry model;
    # opposite-origin + impulse-magnitude body >= 0.8*ATR). Fires on the
    # order_block event (bullish/bearish). min_displacement_atr documents the
    # feature-level body-magnitude gate baked into feat.order_block.
    "london_order_block": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
        "min_displacement_atr": 0.8,
    },
    "ny_order_block": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
        "min_displacement_atr": 0.8,
    },
    # iteration 42 — Heikin-Ashi smoothed-candle strong-trend continuation (32nd
    # entry model; PRICE-TRANSFORM family — smoothed OHLC wick-absence). Fires on
    # the ha_trend event (bullish_ha_strong / bearish_ha_strong).
    "london_ha": {
        "utc_hour_start": 7,
        "utc_hour_end": 10,
    },
    "ny_ha": {
        "utc_hour_start": 14,
        "utc_hour_end": 17,
    },
}

# Human-readable trigger rules (for TUI + eval reports).
_TRIGGER_RULES: dict[str, list[str]] = {
    "trend_continuation": [
        "regime=trending AND move_type=continuation",
        "M5 trend bullish → BUY / bearish → SELL",
        "confidence = avg(trend, momentum) evidence",
    ],
    "pullback": [
        "move_type=pullback OR breakout retest OR compression_setup in compression phase",
        "M5 trend defines side (bullish=BUY, bearish=SELL)",
        "confidence = avg(trend, structure) evidence",
    ],
    "breakout": [
        "breakout state = breakout|breakdown",
        "confidence = avg(structure, volume, momentum)",
    ],
    "compression_breakout": [
        "phase=compression AND breakout|breakdown",
        "confidence = avg(volatility, structure, volume)",
    ],
    "range_fade": [
        "regime=ranging",
        "price within boundary_pct of support → BUY / resistance → SELL",
        "confidence = liquidity evidence",
    ],
    "liquidity_sweep": [
        "rejection = bullish|bearish AND liquidity >= min_liquidity",
        "confidence = avg(liquidity, volume)",
    ],
    "mean_reversion": [
        "bb_position >= bb_upper → SELL / <= bb_lower → BUY",
        "confidence = avg(structure, liquidity)",
    ],
    "false_breakout": [
        "bearish_rejection + bb_position > bb_upper → SELL",
        "bullish_rejection + bb_position < bb_lower → BUY",
        "confidence = structure evidence",
    ],
    "silver_bullet": [
        "14:00-15:00 UTC window AND rejection (CHoCH proxy) AND liquidity >= min",
        "bullish_rejection → BUY / bearish_rejection → SELL",
        "confidence = avg(liquidity, structure) evidence",
    ],
    "london_judas": [
        "07:00-10:00 UTC London killzone AND rejection at session extreme",
        "bullish_rejection (sweep lows) → BUY / bearish_rejection (sweep highs) → SELL",
        "confidence = avg(liquidity, structure) evidence",
    ],
    "oil_orb": [
        "14:00-19:00 UTC NY execution window AND breakout of opening range",
        "breakout → BUY / breakdown → SELL",
        "confidence = avg(structure, volume, momentum) evidence",
    ],
    "asia_range_breakout": [
        "07:00-10:00 UTC London open AND breakout of Asian session range",
        "breakout → BUY / breakdown → SELL",
        "confidence = avg(structure, volume) evidence",
    ],
    "us_open_orb": [
        "14:00-16:00 UTC US RTH open (9:30 AM ET) AND opening-range break",
        "breakout → BUY / breakdown → SELL",
        "confidence = avg(structure, volume, momentum) evidence",
    ],
    "prev_day_breakout": [
        "14:00-21:00 UTC US RTH AND close beyond prev day high/low",
        "breakout (above PDH) → BUY / breakdown (below PDL) → SELL",
        "confidence = avg(structure, volume, momentum) evidence",
    ],
    "eu_open_orb": [
        "08:00-10:00 UTC EU cash open AND break of first 5-15 min opening range",
        "breakout → BUY / breakdown → SELL",
        "confidence = avg(structure, volume, momentum) evidence",
    ],
    "tokyo_open_orb": [
        "00:00-02:00 UTC Tokyo open first hour AND break of opening range",
        "breakout → BUY / breakdown → SELL",
        "confidence = avg(structure, volume, momentum) evidence",
    ],
    "london_close_reversal": [
        "15:00-17:00 UTC London Close killzone AND rejection at session extreme",
        "bullish_rejection (sweep lows) → BUY / bearish_rejection (sweep highs) → SELL",
        "confidence = avg(liquidity, structure) evidence",
    ],
    "sydney_open_orb": [
        "21:00-23:00 UTC Sydney open (Asia week-open) AND break of opening range",
        "breakout → BUY / breakdown → SELL",
        "confidence = avg(structure, volume, momentum) evidence",
    ],
    "london_morning_breakout": [
        "10:00-14:00 UTC London morning AND breakout of session range",
        "breakout → BUY / breakdown → SELL",
        "confidence = avg(structure, volume, momentum) evidence",
    ],
    "ny_lunch_reversal": [
        "17:00-19:00 UTC NY lunch lull AND rejection at morning extreme",
        "bullish_rejection (sweep lows) → BUY / bearish_rejection (sweep highs) → SELL",
        "confidence = avg(liquidity, structure) evidence",
    ],
    "london_bb_reversion": [
        "07:00-10:00 UTC London window AND bb_position >= bb_upper / <= bb_lower",
        "bb at upper → SELL / bb at lower → BUY (fade overstretched band)",
        "confidence = avg(structure, liquidity) evidence",
    ],
    "ny_bb_reversion": [
        "14:00-17:00 UTC US RTH AND bb_position >= bb_upper / <= bb_lower",
        "bb at upper → SELL / bb at lower → BUY (fade overstretched band)",
        "confidence = avg(structure, liquidity) evidence",
    ],
    "london_stoch_cross": [
        "07:00-10:00 UTC London window AND stoch_cross != none AND momentum >= min",
        "bullish_cross → BUY / bearish_cross → SELL (momentum continuation)",
        "confidence = avg(momentum, structure) evidence",
    ],
    "ny_stoch_cross": [
        "14:00-17:00 UTC US RTH AND stoch_cross != none AND momentum >= min",
        "bullish_cross → BUY / bearish_cross → SELL (momentum continuation)",
        "confidence = avg(momentum, structure) evidence",
    ],
    "london_vol_expansion": [
        "07:00-10:00 UTC London window AND volatility_regime == high AND volatility >= min",
        "bullish M5 trend → BUY / bearish → SELL (vol-expansion continuation)",
        "confidence = avg(volatility, momentum) evidence",
    ],
    "ny_vol_expansion": [
        "14:00-17:00 UTC US RTH AND volatility_regime == high AND volatility >= min",
        "bullish M5 trend → BUY / bearish → SELL (vol-expansion continuation)",
        "confidence = avg(volatility, momentum) evidence",
    ],
    "london_volume_spike": [
        "07:00-10:00 UTC London window AND volume_ratio >= min_volume_ratio (1.5x avg)",
        "bullish M5 trend → BUY / bearish → SELL (volume confirms move)",
        "confidence = avg(volume, momentum) evidence",
    ],
    "ny_volume_spike": [
        "14:00-17:00 UTC US RTH AND volume_ratio >= min_volume_ratio (1.5x avg)",
        "bullish M5 trend → BUY / bearish → SELL (volume confirms move)",
        "confidence = avg(volume, momentum) evidence",
    ],
    "london_mtf_align": [
        "07:00-10:00 UTC London window AND timeframe_alignment == True AND trend >= min",
        "aligned bullish → BUY / aligned bearish → SELL (HTF confluence continuation)",
        "confidence = avg(trend, momentum) evidence",
    ],
    "ny_mtf_align": [
        "14:00-17:00 UTC US RTH AND timeframe_alignment == True AND trend >= min",
        "aligned bullish → BUY / aligned bearish → SELL (HTF confluence continuation)",
        "confidence = avg(trend, momentum) evidence",
    ],
    "london_stoch_reversion": [
        "07:00-10:00 UTC London window AND stoch_k <= oversold (20) OR >= overbought (80)",
        "oversold → BUY / overbought → SELL (fade stoch extreme back to mean)",
        "confidence = avg(structure, liquidity) evidence",
    ],
    "ny_stoch_reversion": [
        "14:00-17:00 UTC US RTH AND stoch_k <= oversold (20) OR >= overbought (80)",
        "oversold → BUY / overbought → SELL (fade stoch extreme back to mean)",
        "confidence = avg(structure, liquidity) evidence",
    ],
    "london_htf_breakout": [
        "07:00-10:00 UTC London window AND m15_trend direction agrees with breakout state",
        "M15 bullish + breakout (close > prior resistance) → BUY / M15 bearish + breakdown → SELL",
        "confidence = avg(trend, structure, volume) evidence",
    ],
    "ny_htf_breakout": [
        "14:00-17:00 UTC US RTH AND m15_trend direction agrees with breakout state",
        "M15 bullish + breakout (close > prior resistance) → BUY / M15 bearish + breakdown → SELL",
        "confidence = avg(trend, structure, volume) evidence",
    ],
    "london_squeeze_breakout": [
        "07:00-10:00 UTC London window AND bb_squeeze_pct <= max_squeeze_pct (0.20) AND breakout state",
        "breakout → BUY / breakdown → SELL (trade the squeeze release direction)",
        "confidence = avg(structure, volume, momentum) evidence",
    ],
    "ny_squeeze_breakout": [
        "14:00-17:00 UTC US RTH AND bb_squeeze_pct <= max_squeeze_pct (0.20) AND breakout state",
        "breakout → BUY / breakdown → SELL (trade the squeeze release direction)",
        "confidence = avg(structure, volume, momentum) evidence",
    ],
    "london_volume_breakout": [
        "07:00-10:00 UTC London window AND volume_ratio >= min_volume_ratio (1.5) AND breakout state",
        "breakout → BUY / breakdown → SELL (volume-confirmed structural break)",
        "confidence = avg(structure, volume, momentum) evidence",
    ],
    "ny_volume_breakout": [
        "14:00-17:00 UTC US RTH AND volume_ratio >= min_volume_ratio (1.5) AND breakout state",
        "breakout → BUY / breakdown → SELL (volume-confirmed structural break)",
        "confidence = avg(structure, volume, momentum) evidence",
    ],
    "london_rsi_reversion": [
        "07:00-10:00 UTC London window AND rsi <= oversold (30) OR >= overbought (70)",
        "oversold → BUY / overbought → SELL (fade RSI extreme back to mean)",
        "confidence = avg(structure, liquidity) evidence",
    ],
    "ny_rsi_reversion": [
        "14:00-17:00 UTC US RTH AND rsi <= oversold (30) OR >= overbought (70)",
        "oversold → BUY / overbought → SELL (fade RSI extreme back to mean)",
        "confidence = avg(structure, liquidity) evidence",
    ],
    "london_macd_cross": [
        "07:00-10:00 UTC London window AND macd_cross == bullish_cross OR bearish_cross AND momentum >= min (0.50)",
        "bullish_cross → BUY / bearish_cross → SELL (smoothed-momentum continuation)",
        "confidence = avg(momentum, structure) evidence",
    ],
    "ny_macd_cross": [
        "14:00-17:00 UTC US RTH AND macd_cross == bullish_cross OR bearish_cross AND momentum >= min (0.50)",
        "bullish_cross → BUY / bearish_cross → SELL (smoothed-momentum continuation)",
        "confidence = avg(momentum, structure) evidence",
    ],
    "london_cci_reversion": [
        "07:00-10:00 UTC London window AND cci <= oversold (-100) OR >= overbought (+100)",
        "oversold → BUY / overbought → SELL (fade CCI extreme back to mean)",
        "confidence = avg(structure, liquidity) evidence",
    ],
    "ny_cci_reversion": [
        "14:00-17:00 UTC US RTH AND cci <= oversold (-100) OR >= overbought (+100)",
        "oversold → BUY / overbought → SELL (fade CCI extreme back to mean)",
        "confidence = avg(structure, liquidity) evidence",
    ],
    "london_mfi_reversion": [
        "07:00-10:00 UTC London window AND mfi <= oversold (20) OR >= overbought (80)",
        "oversold → BUY / overbought → SELL (volume-weighted fade back to mean)",
        "confidence = avg(structure, liquidity) evidence",
    ],
    "ny_mfi_reversion": [
        "14:00-17:00 UTC US RTH AND mfi <= oversold (20) OR >= overbought (80)",
        "oversold → BUY / overbought → SELL (volume-weighted fade back to mean)",
        "confidence = avg(structure, liquidity) evidence",
    ],
    "london_adx_trend": [
        "07:00-10:00 UTC London window AND adx >= 25 strong trend",
        "+DI > -DI → BUY / -DI > +DI → SELL (strength-filtered continuation)",
        "confidence = avg(trend, momentum) evidence",
    ],
    "ny_adx_trend": [
        "14:00-17:00 UTC US RTH AND adx >= 25 strong trend",
        "+DI > -DI → BUY / -DI > +DI → SELL (strength-filtered continuation)",
        "confidence = avg(trend, momentum) evidence",
    ],
    "london_obv_cross": [
        "07:00-10:00 UTC London window AND OBV crosses its EMA(20)",
        "bullish cross → BUY (net accumulation) / bearish cross → SELL (net distribution)",
        "confidence = avg(volume, momentum) evidence",
    ],
    "ny_obv_cross": [
        "14:00-17:00 UTC US RTH AND OBV crosses its EMA(20)",
        "bullish cross → BUY (net accumulation) / bearish cross → SELL (net distribution)",
        "confidence = avg(volume, momentum) evidence",
    ],
    "london_atr_pct_breakout": [
        "07:00-10:00 UTC London window AND atr_pct >= 0.70 (relative-vol expanding vs own 100-bar history)",
        "M5 trend bullish → BUY / bearish → SELL (relative-vol expansion continuation)",
        "confidence = avg(volatility, momentum) evidence",
    ],
    "ny_atr_pct_breakout": [
        "14:00-17:00 UTC US RTH AND atr_pct >= 0.70 (relative-vol expanding vs own 100-bar history)",
        "M5 trend bullish → BUY / bearish → SELL (relative-vol expansion continuation)",
        "confidence = avg(volatility, momentum) evidence",
    ],
    "london_cmf_continuation": [
        "07:00-10:00 UTC London window AND CMF(20) >= +0.10 (accumulation, closes near bar highs on rising vol) / <= -0.10 (distribution)",
        "M5 trend bullish + CMF >= +0.10 → BUY / bearish + CMF <= -0.10 → SELL (volume-pressure continuation)",
        "confidence = avg(volume, momentum) evidence",
    ],
    "ny_cmf_continuation": [
        "14:00-17:00 UTC US RTH AND CMF(20) >= +0.10 (accumulation, closes near bar highs on rising vol) / <= -0.10 (distribution)",
        "M5 trend bullish + CMF >= +0.10 → BUY / bearish + CMF <= -0.10 → SELL (volume-pressure continuation)",
        "confidence = avg(volume, momentum) evidence",
    ],
    "london_triple_confirm": [
        "07:00-10:00 UTC London window AND ALL THREE: ADX(14) >= 25 (strong trend) + ATR-pct >= 0.70 (relative-vol expanding) + CMF(20) agrees with trend",
        "M5 trend bullish + ADX>=25 + ATR-pct>=0.70 + CMF>=+0.10 → BUY / bearish + ... + CMF<=-0.10 → SELL (3-way confluence continuation)",
        "confidence = avg(trend, momentum, volume) evidence",
    ],
    "ny_triple_confirm": [
        "14:00-17:00 UTC US RTH AND ALL THREE: ADX(14) >= 25 (strong trend) + ATR-pct >= 0.70 (relative-vol expanding) + CMF(20) agrees with trend",
        "M5 trend bullish + ADX>=25 + ATR-pct>=0.70 + CMF>=+0.10 → BUY / bearish + ... + CMF<=-0.10 → SELL (3-way confluence continuation)",
        "confidence = avg(trend, momentum, volume) evidence",
    ],
    "london_adx_cmf": [
        "07:00-10:00 UTC London window AND BOTH: ADX(14) >= 25 (strong trend) + CMF(20) agrees with trend (drops ATR-pct from triple)",
        "M5 trend bullish + ADX>=25 + CMF>=+0.10 → BUY / bearish + ADX>=25 + CMF<=-0.10 → SELL (2-way strength+pressure confluence)",
        "confidence = avg(trend, momentum, volume) evidence",
    ],
    "ny_adx_cmf": [
        "14:00-17:00 UTC US RTH AND BOTH: ADX(14) >= 25 (strong trend) + CMF(20) agrees with trend (drops ATR-pct from triple)",
        "M5 trend bullish + ADX>=25 + CMF>=+0.10 → BUY / bearish + ADX>=25 + CMF<=-0.10 → SELL (2-way strength+pressure confluence)",
        "confidence = avg(trend, momentum, volume) evidence",
    ],
    "london_adx_atr_pct": [
        "07:00-10:00 UTC London window AND BOTH: ADX(14) >= 25 (strong trend) + ATR-pct >= 0.70 (relative-vol expanding) (drops CMF from triple)",
        "M5 trend bullish + ADX>=25 + ATR-pct>=0.70 → BUY / bearish + ADX>=25 + ATR-pct>=0.70 → SELL (2-way strength+vol-rank confluence, no money-flow gate)",
        "confidence = avg(trend, momentum, volume) evidence",
    ],
    "ny_adx_atr_pct": [
        "14:00-17:00 UTC US RTH AND BOTH: ADX(14) >= 25 (strong trend) + ATR-pct >= 0.70 (relative-vol expanding) (drops CMF from triple)",
        "M5 trend bullish + ADX>=25 + ATR-pct>=0.70 → BUY / bearish + ADX>=25 + ATR-pct>=0.70 → SELL (2-way strength+vol-rank confluence, no money-flow gate)",
        "confidence = avg(trend, momentum, volume) evidence",
    ],
    "london_atr_pct_cmf": [
        "07:00-10:00 UTC London window AND BOTH: ATR-pct >= 0.70 (relative-vol expanding) + CMF(20) agrees with trend (drops ADX from triple)",
        "M5 trend bullish + ATR-pct>=0.70 + CMF>=+0.10 → BUY / bearish + ATR-pct>=0.70 + CMF<=-0.10 → SELL (2-way vol-rank+pressure confluence, no strength gate)",
        "confidence = avg(trend, momentum, volume) evidence",
    ],
    "ny_atr_pct_cmf": [
        "14:00-17:00 UTC US RTH AND BOTH: ATR-pct >= 0.70 (relative-vol expanding) + CMF(20) agrees with trend (drops ADX from triple)",
        "M5 trend bullish + ATR-pct>=0.70 + CMF>=+0.10 → BUY / bearish + ATR-pct>=0.70 + CMF<=-0.10 → SELL (2-way vol-rank+pressure confluence, no strength gate)",
        "confidence = avg(trend, momentum, volume) evidence",
    ],
    "london_vwap_reversion": [
        "07:00-10:00 UTC London window AND vwap_position at band extreme (rolling VWAP+/-1.5sigma band, volume-weighted anchor)",
        "vwap_position >= 0.92 (above upper band) → SELL fade / <= 0.08 (below lower band) → BUY fade back to VWAP",
        "confidence = avg(structure, liquidity) evidence",
    ],
    "ny_vwap_reversion": [
        "14:00-17:00 UTC US RTH AND vwap_position at band extreme (rolling VWAP+/-1.5sigma band, volume-weighted anchor)",
        "vwap_position >= 0.92 (above upper band) → SELL fade / <= 0.08 (below lower band) → BUY fade back to VWAP",
        "confidence = avg(structure, liquidity) evidence",
    ],
    "london_supertrend_flip": [
        "07:00-10:00 UTC London window AND supertrend_flip event (Supertrend(10,3.0) ATR-band direction change)",
        "supertrend_flip bullish_flip (DOWN→UP) → BUY / bearish_flip (UP→DOWN) → SELL (trend-continuation in flip direction)",
        "confidence = avg(trend, momentum) evidence",
    ],
    "ny_supertrend_flip": [
        "14:00-17:00 UTC US RTH AND supertrend_flip event (Supertrend(10,3.0) ATR-band direction change)",
        "supertrend_flip bullish_flip (DOWN→UP) → BUY / bearish_flip (UP→DOWN) → SELL (trend-continuation in flip direction)",
        "confidence = avg(trend, momentum) evidence",
    ],
    "london_ichimoku_tk_cross": [
        "07:00-10:00 UTC London window AND ichimoku_tk_cross event (Ichimoku 9/26/52: Tenkan-sen crosses Kijun-sen midpoint equilibrium) confirmed by cloud position",
        "bullish_cross (Tenkan over Kijun) + price ABOVE Senkou cloud → BUY / bearish_cross + BELOW cloud → SELL (cross + inside cloud = skip, no confirmation)",
        "confidence = avg(trend, momentum) evidence",
    ],
    "ny_ichimoku_tk_cross": [
        "14:00-17:00 UTC US RTH AND ichimoku_tk_cross event (Ichimoku 9/26/52: Tenkan-sen crosses Kijun-sen midpoint equilibrium) confirmed by cloud position",
        "bullish_cross (Tenkan over Kijun) + price ABOVE Senkou cloud → BUY / bearish_cross + BELOW cloud → SELL (cross + inside cloud = skip, no confirmation)",
        "confidence = avg(trend, momentum) evidence",
    ],
    "london_fvg": [
        "07:00-10:00 UTC London window AND fvg event (ICT/SMC 3-bar Fair Value Gap: bar[i-2].high < bar[i].low bullish / bar[i-2].low > bar[i].high bearish)",
        "bullish_fvg (gap-up imbalance) → BUY / bearish_fvg → SELL (continuation in imbalance direction, gap size >= 0.25*ATR required)",
        "confidence = avg(momentum, volume) evidence",
    ],
    "ny_fvg": [
        "14:00-17:00 UTC US RTH AND fvg event (ICT/SMC 3-bar Fair Value Gap: bar[i-2].high < bar[i].low bullish / bar[i-2].low > bar[i].high bearish)",
        "bullish_fvg (gap-up imbalance) → BUY / bearish_fvg → SELL (continuation in imbalance direction, gap size >= 0.25*ATR required)",
        "confidence = avg(momentum, volume) evidence",
    ],
    "london_engulfing": [
        "07:00-10:00 UTC London window AND engulfing event (2-bar candle-body engulf: bullish bar wraps prior bearish body / bearish wraps prior bullish)",
        "bullish_engulfing → BUY / bearish_engulfing → SELL (reversal in engulfing direction)",
        "confidence = avg(structure, momentum) evidence",
    ],
    "ny_engulfing": [
        "14:00-17:00 UTC US RTH AND engulfing event (2-bar candle-body engulf: bullish bar wraps prior bearish body / bearish wraps prior bullish)",
        "bullish_engulfing → BUY / bearish_engulfing → SELL (reversal in engulfing direction)",
        "confidence = avg(structure, momentum) evidence",
    ],
    "london_inside_bar": [
        "07:00-10:00 UTC London window AND inside_bar event (3-bar range-nesting: inside bar range nests within mother bar, then breakout bar closes outside mother range)",
        "bullish_inside_breakout (closes above mother high) → BUY / bearish_inside_breakout (closes below mother low) → SELL (continuation in breakout direction)",
        "confidence = avg(structure, momentum) evidence",
    ],
    "ny_inside_bar": [
        "14:00-17:00 UTC US RTH AND inside_bar event (3-bar range-nesting: inside bar range nests within mother bar, then breakout bar closes outside mother range)",
        "bullish_inside_breakout (closes above mother high) → BUY / bearish_inside_breakout (closes below mother low) → SELL (continuation in breakout direction)",
        "confidence = avg(structure, momentum) evidence",
    ],
    "london_close_streak": [
        "07:00-10:00 UTC London window AND close_streak event (run-length of consecutive same-direction closes: streak=+N consecutive up-closes / streak=-N consecutive down-closes, N >= min_streak=3)",
        "streak >= +3 → BUY / streak <= -3 → SELL (momentum continuation in streak direction)",
        "confidence = avg(momentum, volume) evidence",
    ],
    "ny_close_streak": [
        "14:00-17:00 UTC US RTH AND close_streak event (run-length of consecutive same-direction closes: streak=+N consecutive up-closes / streak=-N consecutive down-closes, N >= min_streak=3)",
        "streak >= +3 → BUY / streak <= -3 → SELL (momentum continuation in streak direction)",
        "confidence = avg(momentum, volume) evidence",
    ],
    "london_order_block": [
        "07:00-10:00 UTC London window AND order_block event (ICT/SMC displacement origin: prior opposite-colored candle + current displacement bar with body >= min_displacement_atr=0.8*ATR(14))",
        "bullish_order_block (prior bearish + strong bullish displacement) → BUY / bearish_order_block (prior bullish + strong bearish displacement) → SELL (displacement continuation)",
        "confidence = avg(structure, momentum) evidence",
    ],
    "ny_order_block": [
        "14:00-17:00 UTC US RTH AND order_block event (ICT/SMC displacement origin: prior opposite-colored candle + current displacement bar with body >= min_displacement_atr=0.8*ATR(14))",
        "bullish_order_block (prior bearish + strong bullish displacement) → BUY / bearish_order_block (prior bullish + strong bearish displacement) → SELL (displacement continuation)",
        "confidence = avg(structure, momentum) evidence",
    ],
    "london_ha": [
        "07:00-10:00 UTC London window AND ha_trend event (Heikin-Ashi PRICE-TRANSFORM smoothed candle: HA_close=(O+H+L+C)/4, HA_open=(prev_HA_open+prev_HA_close)/2)",
        "bullish_ha_strong (green smoothed candle, no lower wick: HA_close>HA_open AND low>=HA_open) → BUY / bearish_ha_strong (red, no upper wick) → SELL (smoothed-trend continuation)",
        "confidence = avg(momentum, volume) evidence",
    ],
    "ny_ha": [
        "14:00-17:00 UTC US RTH AND ha_trend event (Heikin-Ashi PRICE-TRANSFORM smoothed candle: HA_close=(O+H+L+C)/4, HA_open=(prev_HA_open+prev_HA_close)/2)",
        "bullish_ha_strong (green smoothed candle, no lower wick) → BUY / bearish_ha_strong (red, no upper wick) → SELL (smoothed-trend continuation)",
        "confidence = avg(momentum, volume) evidence",
    ],
}


def _intel_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("intelligence", {}) if isinstance(config.get("intelligence"), dict) else {}


def trigger_params(config: dict[str, Any], setup_type: str) -> dict[str, Any]:
    """Resolved tunable params for a setup (defaults + config overrides)."""
    base = dict(_TRIGGER_DEFAULTS.get(setup_type, {}))
    block = _intel_cfg(config).get("setup_triggers", {})
    if isinstance(block, dict):
        overrides = block.get(setup_type)
        if isinstance(overrides, dict):
            base.update(overrides)
        global_ov = block.get("global")
        if isinstance(global_ov, dict):
            for k, v in global_ov.items():
                base.setdefault(k, v)
    return base


def trigger_rules(setup_type: str) -> list[str]:
    return list(_TRIGGER_RULES.get(setup_type, []))


def describe_trigger(setup_type: str, config: dict[str, Any] | None = None) -> str:
    """One-line trigger summary for TUI."""
    rules = _TRIGGER_RULES.get(setup_type, [])
    if not rules:
        return setup_type
    line = rules[0]
    if config:
        params = trigger_params(config, setup_type)
        if setup_type == "mean_reversion" and params:
            line = f"bb>={params.get('bb_upper', 0.92)} SELL / bb<={params.get('bb_lower', 0.08)} BUY"
        elif setup_type == "range_fade":
            line = f"ranging + within {params.get('boundary_pct', 0.0015)*100:.2f}% of S/R"
        elif setup_type == "liquidity_sweep":
            line = f"rejection + liquidity>={params.get('min_liquidity', 0.65)}"
    return line


def snapshot_trigger_context(
    setup: dict[str, Any],
    feat: dict[str, Any],
    ctx: dict[str, Any],
    ev: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Feature/context slice stamped on arena trigger rows for eval."""
    params = trigger_params(config or {}, setup.get("setup_type", ""))
    return {
        "setup_confidence": setup.get("setup_confidence"),
        "reason": setup.get("reason"),
        "params": params,
        "feat": {
            "m5_trend": feat.get("m5_trend"),
            "breakout": feat.get("breakout"),
            "bb_position": feat.get("bb_position"),
            "rejection": feat.get("rejection"),
            "price": feat.get("price"),
            "support": feat.get("support"),
            "resistance": feat.get("resistance"),
        },
        "ctx": {
            "regime": ctx.get("regime"),
            "phase": ctx.get("phase"),
            "move_type": ctx.get("move_type"),
            "session": ctx.get("session"),
            "market_regime": (ctx.get("market_regime") or {}).get("primary"),
        },
        "evidence": {k: round(float(v), 3) if isinstance(v, (int, float)) else v for k, v in (ev or {}).items()},
    }


def export_catalog(config: dict[str, Any]) -> dict[str, Any]:
    """Full strategy + trigger catalog for evaluator / optimizer."""
    setups: list[dict[str, Any]] = []
    for name in SETUP_ORDER:
        defn = SETUP_LIBRARY.get(name)
        params = trigger_params(config, name)
        setups.append({
            "setup_type": name,
            "display_name": defn.display_name if defn else name,
            "description": defn.description if defn else "",
            "allowed_regimes": list(defn.allowed_regimes) if defn else [],
            "blocked_regimes": list(defn.blocked_regimes) if defn else [],
            "min_confidence": defn.min_confidence if defn else 0.5,
            "min_rr": defn.min_rr if defn else 1.2,
            "entry_hints": list(defn.entry_hints) if defn else [],
            "exit_hints": list(defn.exit_hints) if defn else [],
            "trigger_rules": trigger_rules(name),
            "trigger_summary": describe_trigger(name, config),
            "trigger_params": params,
        })
    arena = config.get("strategy_arena", {}) if isinstance(config.get("strategy_arena"), dict) else {}
    return {
        "timestamp": utc_now_iso(),
        "setup_count": len(setups),
        "setups": setups,
        "arena_symbols": list(arena.get("symbols") or config.get("mt5", {}).get("symbols") or []),
        "emit_all_setups": bool(arena.get("emit_all_setups", True)),
    }


def write_setup_catalog(config: dict[str, Any]) -> dict[str, Any]:
    catalog = export_catalog(config)
    write_json_state("setup_catalog.json", catalog)
    return catalog