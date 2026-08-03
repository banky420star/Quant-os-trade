"""Setup Classifier — classify opportunity type, not generic BUY/SELL."""

from __future__ import annotations

import logging
from typing import Any

from core.setup_library import get_setup, is_regime_compatible
from core.setup_triggers import trigger_params
from core.specialized_setups import detect_specialized

SETUP_TYPES = (
    "breakout",
    "pullback",
    "trend_continuation",
    "range_fade",
    "liquidity_sweep",
    "mean_reversion",
    "false_breakout",
    "compression_breakout",
    # 2026-07-31 — specialized ICT/SMC killzone setups (opt-in).
    "silver_bullet",
    "london_judas",
    # iteration 2 — symbol-specific breakout setups (opt-in).
    "oil_orb",
    "asia_range_breakout",
    # iteration 3 — US equity-index specialized setups (opt-in).
    "us_open_orb",
    "prev_day_breakout",
    # iteration 4 — EU + Tokyo open gaps (opt-in).
    "eu_open_orb",
    "tokyo_open_orb",
    # iteration 5 — London close reversal + Sydney open ORB (opt-in).
    "london_close_reversal",
    "sydney_open_orb",
    # iteration 6 — London morning breakout + NY lunch reversal (opt-in).
    "london_morning_breakout",
    "ny_lunch_reversal",
    # iteration 7 — session-gated BB mean reversion (opt-in, third entry model).
    "london_bb_reversion",
    "ny_bb_reversion",
    # iteration 12 — session-gated stochastic-cross momentum (4th entry model).
    "london_stoch_cross",
    "ny_stoch_cross",
    # iteration 13 — session-gated volatility-expansion momentum (5th entry model).
    "london_vol_expansion",
    "ny_vol_expansion",
    # iteration 14 — session-gated volume-spike confirmation (6th entry model).
    "london_volume_spike",
    "ny_volume_spike",
    # iteration 15 — session-gated multi-timeframe alignment (7th entry model).
    "london_mtf_align",
    "ny_mtf_align",
    # iteration 16 — session-gated stochastic oversold/overbought reversion
    # (8th entry model, uses stoch_k raw level — mean reversion, not cross).
    "london_stoch_reversion",
    "ny_stoch_reversion",
    # iteration 18 — session-gated higher-timeframe-trend-filtered breakout
    # (9th entry model, uses m15_trend DIRECTION as a primary key).
    "london_htf_breakout",
    "ny_htf_breakout",
    # iteration 19 — session-gated BB-squeeze-release breakout (10th entry
    # model, uses bb_squeeze_pct — a new rolling-compression feature).
    "london_squeeze_breakout",
    "ny_squeeze_breakout",
    # iteration 20 — session-gated volume-confirmed breakout (11th entry model,
    # uses the volume_ratio + breakout STATE compound).
    "london_volume_breakout",
    "ny_volume_breakout",
    # iteration 21 — session-gated RSI oversold/overbought mean reversion
    # (12th entry model, uses feat.rsi raw level — Wilder's RSI(14), new feature).
    "london_rsi_reversion",
    "ny_rsi_reversion",
    # iteration 22 — session-gated MACD signal-line-cross momentum continuation
    # (13th entry model, uses feat.macd_cross — smoothed-momentum oscillator).
    "london_macd_cross",
    "ny_macd_cross",
    # iteration 23 — session-gated CCI oversold/overbought mean reversion
    # (14th entry model, uses feat.cci raw level — CCI(20) price-deviation
    # oscillator).
    "london_cci_reversion",
    "ny_cci_reversion",
    # iteration 24 — session-gated MFI oversold/overbought mean reversion
    # (15th entry model, uses feat.mfi raw level — MFI(14) VOLUME-WEIGHTED
    # oscillator, the only one here that folds in volume via the money-flow
    # ratio).
    "london_mfi_reversion",
    "ny_mfi_reversion",
    # iteration 25 — session-gated ADX/DI trend-strength continuation (16th
    # entry model, uses feat.adx + feat.di_plus/di_minus — Wilder DMI(14)
    # trend-STRENGTH gate, the only strength dimension here).
    "london_adx_trend",
    "ny_adx_trend",
    # iteration 26 — session-gated OBV-EMA-cross volume-accumulation
    # continuation (17th entry model, uses feat.obv_cross — On-Balance Volume
    # cumulative signed-volume line crossing its EMA(20); the only
    # volume-ACCUMULATION dimension here).
    "london_obv_cross",
    "ny_obv_cross",
    # iteration 27 — session-gated ATR-percentile-breakout relative-vol
    # expansion (18th entry model, uses feat.atr_pct — ATR(14) rolling 100-bar
    # percentile rank; the only relative-volatility-RANK dimension here).
    "london_atr_pct_breakout",
    "ny_atr_pct_breakout",
    # iteration 28 — session-gated Chaikin-Money-Flow-confirmed continuation
    # (19th entry model, uses feat.cmf — CMF(20) intrabar close-location *
    # volume pressure, bounded [-1,+1]; the 4th volume dimension here, distinct
    # from volume_ratio level / mfi price-change ratio / obv cumulative line).
    "london_cmf_continuation",
    "ny_cmf_continuation",
    # iteration 29 — session-gated triple-confirmation momentum continuation
    # (20th entry model, a 3-WAY COMPOUND: the conjunction of ADX trend
    # strength + atr_pct relative-vol rank + cmf intrabar money-flow pressure
    # is the primary key, distinct from any single-dim setup).
    "london_triple_confirm",
    "ny_triple_confirm",
    # iteration 30 — session-gated ADX+CMF 2-way-compound continuation (21st
    # entry model, 2nd compound: drops the atr_pct gate from the triple to
    # isolate whether strength + money-flow pressure alone carry the edge).
    "london_adx_cmf",
    "ny_adx_cmf",
    # iteration 31 — session-gated ADX+ATR-pct 2-way-compound continuation (22nd
    # entry model, 3rd compound: drops the cmf gate from the triple — the
    # attribution complement to iter30's adx×cmf, isolates whether strength +
    # relative-vol-rank WITHOUT money-flow pressure carry the EUR/GBP edge).
    "london_adx_atr_pct",
    "ny_adx_atr_pct",
    # iteration 32 — session-gated ATR-pct+CMF 2-way-compound continuation (23rd
    # entry model, 4th and FINAL compound: drops the adx gate from the triple —
    # completes the full 2-way attribution matrix; tests whether vol-rank +
    # money-flow WITHOUT trend-strength carry the edge, expected to degrade).
    "london_atr_pct_cmf",
    "ny_atr_pct_cmf",
    # iteration 34 — session-gated VWAP-band mean reversion (24th entry model,
    # 5th volume dimension: a volume-weighted PRICE anchor + bands, distinct
    # from bb_position's price-only SMA band and from the flow/level volume
    # features; keys off feat.vwap_position — fade a band extreme to VWAP).
    "london_vwap_reversion",
    "ny_vwap_reversion",
    # iteration 35 — session-gated Supertrend-flip trend continuation (25th
    # entry model, new trend-STATE dimension: an ATR-band direction flip,
    # distinct from m5_trend EMA DIRECTION and adx STRENGTH; keys off
    # feat.supertrend_flip — trade the ATR-band trend-state change).
    "london_supertrend_flip",
    "ny_supertrend_flip",
    # iteration 36 — session-gated Ichimoku Tenkan/Kijun-cross continuation (26th
    # entry model, new EQUILIBRIUM dimension: a rolling high-low midpoint cross
    # confirmed by cloud position, distinct from EMA direction + ADX strength +
    # ATR-band state + VWAP volume-weighted anchor + close-vs-range oscillators;
    # keys off feat.ichimoku_tk_cross + feat.ichimoku_cloud_position).
    "london_ichimoku_tk_cross",
    "ny_ichimoku_tk_cross",
    # iteration 37 — session-gated Fair Value Gap imbalance continuation (27th
    # entry model, new PRICE-IMBALANCE dimension: a non-adjacent 3-bar ICT/SMC
    # structural gap gated by ATR-relative size, distinct from all 1-bar-S/R
    # breakouts + continuous-structure setups; keys off feat.fvg).
    "london_fvg",
    "ny_fvg",
    # iteration 38 — session-gated 2-bar Engulfing candlestick reversal (28th
    # entry model, new CANDLE-STRUCTURE dimension: a 2-bar body-vs-body engulf,
    # distinct from 1-bar wick rejection + 3-bar FVG price gap + continuous
    # oscillators; keys off feat.engulfing — bullish bar wraps prior bearish
    # body → BUY / bearish bar wraps prior bullish body → SELL).
    "london_engulfing",
    "ny_engulfing",
    # iteration 39 — session-gated 3-bar Inside-Bar breakout (29th entry
    # model, new CANDLE-STRUCTURE dimension: a 2-3-bar range-nesting then
    # expansion, distinct from 1-bar wick rejection + 2-bar body engulf +
    # 3-bar FVG gap + continuous oscillators; keys off feat.inside_bar —
    # bullish_inside_breakout (closes above mother high) → BUY /
    # bearish_inside_breakout (closes below mother low) → SELL).
    "london_inside_bar",
    "ny_inside_bar",
    # iteration 40 — session-gated consecutive-close-streak continuation (30th
    # entry model, new STATISTICAL dimension: a run-length of consecutive
    # same-direction closes, distinct from EMA-state / ADX-strength / candle-
    # structure patterns / oscillators; keys off feat.close_streak —
    # streak >= +3 consecutive up-closes → BUY / streak <= -3 → SELL).
    "london_close_streak",
    "ny_close_streak",
    # iteration 41 — session-gated ICT/SMC Order-Block displacement continuation
    # (31st entry model, new CANDLE-STRUCTURE dimension: opposite-origin + impulse-
    # magnitude body >= 0.8*ATR, distinct from body-wrap engulf / range-nesting
    # inside-bar / 3-bar FVG gap / run-length close-streak; keys off
    # feat.order_block — prior opposite candle + strong displacement bar).
    "london_order_block",
    "ny_order_block",
    # iteration 42 — session-gated Heikin-Ashi smoothed-candle strong-trend
    # continuation (32nd entry model, new PRICE-TRANSFORM family: smoothed OHLC
    # candle wick-absence, distinct from all raw-OHLC candle-structure / oscillator
    # / state / run-length entries; keys off feat.ha_trend — green HA no lower wick
    # / red HA no upper wick).
    "london_ha",
    "ny_ha",
)


class SetupClassifier:
    """Classify the dominant setup type and direction from evidence + context."""

    def __init__(self, config: dict[str, Any] | None = None, logger: logging.Logger | None = None):
        self.config = config or {}
        self.logger = logger or logging.getLogger("setup_classifier")
        intel = self.config.get("intelligence", {})
        trading = self.config.get("trading", {})
        aggressive = bool(trading.get("aggressive_mode", False))
        self.regime_filter = bool(intel.get("regime_filter", not aggressive))
        self.setup_confidence_mult = float(intel.get("setup_confidence_mult", 0.85 if aggressive else 1.0))

    def classify(
        self,
        feat: dict[str, Any],
        context: dict[str, Any],
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        candidates = [
            self._trend_continuation(feat, context, evidence),
            self._breakout(feat, context, evidence),
            self._compression_breakout(feat, context, evidence),
            self._pullback(feat, context, evidence),
            self._mean_reversion(feat, context, evidence),
            self._range_fade(feat, context, evidence),
            self._liquidity_sweep(feat, context, evidence),
            self._false_breakout(feat, context, evidence),
        ]
        # 2026-07-31 — specialized ICT/SMC killzone setups (opt-in, default
        # off). detect_specialized returns [] unless signals.specialized_setups
        # is enabled. Candidates match the shape above, so they flow through
        # the same regime-filter + min-confidence + ranking path.
        candidates.extend(detect_specialized(feat, context, evidence, self.config, self.logger))
        valid = [c for c in candidates if c]
        primary_regime = context.get("market_regime", {}).get("primary", "")
        if self.regime_filter and primary_regime:
            valid = [c for c in valid if is_regime_compatible(c["setup_type"], primary_regime)]
        if not valid:
            return None
        best = max(valid, key=lambda x: x["setup_confidence"])
        defn = get_setup(best["setup_type"])
        min_conf = (defn.min_confidence * self.setup_confidence_mult) if defn else 0.5
        if defn and best["setup_confidence"] < min_conf:
            return None
        return best

    def classify_all(
        self,
        feat: dict[str, Any],
        context: dict[str, Any],
        evidence: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Return every setup that fires this bar (arena competition mode)."""
        candidates = [
            self._trend_continuation(feat, context, evidence),
            self._breakout(feat, context, evidence),
            self._compression_breakout(feat, context, evidence),
            self._pullback(feat, context, evidence),
            self._mean_reversion(feat, context, evidence),
            self._range_fade(feat, context, evidence),
            self._liquidity_sweep(feat, context, evidence),
            self._false_breakout(feat, context, evidence),
        ]
        candidates.extend(detect_specialized(feat, context, evidence, self.config, self.logger))
        valid = [c for c in candidates if c]
        primary_regime = context.get("market_regime", {}).get("primary", "")
        if self.regime_filter and primary_regime:
            valid = [c for c in valid if is_regime_compatible(c["setup_type"], primary_regime)]
        passed: list[dict[str, Any]] = []
        for cand in valid:
            defn = get_setup(cand["setup_type"])
            min_conf = (defn.min_confidence * self.setup_confidence_mult) if defn else 0.5
            if defn and cand["setup_confidence"] < min_conf:
                continue
            passed.append(cand)
        passed.sort(key=lambda x: x["setup_confidence"], reverse=True)
        return passed

    def _base(self, setup_type: str, side: str, confidence: float, reason: str) -> dict[str, Any]:
        return {
            "setup_type": setup_type,
            "side": side,
            "setup_confidence": round(confidence, 2),
            "reason": reason,
        }

    def _params(self, setup_type: str) -> dict:
        return trigger_params(self.config, setup_type)

    def _trend_continuation(self, feat: dict, ctx: dict, ev: dict) -> dict | None:
        p = self._params("trend_continuation")
        if ctx.get("regime") != p.get("regime", "trending") or ctx.get("move_type") != p.get("move_type", "continuation"):
            return None
        m5 = feat.get("m5_trend")
        if m5 == "bullish":
            conf = (ev["trend"] + ev["momentum"]) / 2
            return self._base("trend_continuation", "BUY", conf, "Aligned trend continuation — market pushing higher")
        if m5 == "bearish":
            conf = (ev["trend"] + ev["momentum"]) / 2
            return self._base("trend_continuation", "SELL", conf, "Aligned trend continuation — market pushing lower")
        return None

    def _breakout(self, feat: dict, ctx: dict, ev: dict) -> dict | None:
        states = tuple(self._params("breakout").get("breakout_states", ("breakout", "breakdown")))
        if feat.get("breakout") not in states:
            return None
        side = "BUY" if feat.get("breakout") == "breakout" else "SELL"
        conf = (ev["structure"] + ev["volume"] + ev["momentum"]) / 3
        return self._base("breakout", side, conf, f"Breakout {'above resistance' if side == 'BUY' else 'below support'}")

    def _compression_breakout(self, feat: dict, ctx: dict, ev: dict) -> dict | None:
        p = self._params("compression_breakout")
        if ctx.get("phase") != p.get("phase", "compression"):
            return None
        states = tuple(p.get("breakout_states", ("breakout", "breakdown")))
        if feat.get("breakout") in states:
            side = "BUY" if feat["breakout"] == "breakout" else "SELL"
            conf = (ev["volatility"] + ev["structure"] + ev["volume"]) / 3
            return self._base("compression_breakout", side, conf, "Compression breakout — coiled energy release")
        return None

    def _pullback(self, feat: dict, ctx: dict, ev: dict) -> dict | None:
        p = self._params("pullback")
        move = ctx.get("move_type")
        retest_states = tuple(p.get("retest_breakouts", ("breakout_retest", "breakdown_retest")))
        move_types = tuple(p.get("move_types", ("pullback",)))
        retest = feat.get("breakout") in retest_states
        compression_pullback = (
            p.get("allow_compression_setup", True)
            and ctx.get("phase") == "compression"
            and move == "compression_setup"
        )
        if move not in move_types and not retest and not compression_pullback:
            return None
        m5 = feat.get("m5_trend", "neutral")
        if m5 == "bullish":
            return self._base("pullback", "BUY", (ev["trend"] + ev["structure"]) / 2, "Pullback in bullish trend — retest hold")
        if m5 == "bearish":
            return self._base("pullback", "SELL", (ev["trend"] + ev["structure"]) / 2, "Pullback in bearish trend — retest hold")
        return None

    def _mean_reversion(self, feat: dict, ctx: dict, ev: dict) -> dict | None:
        p = self._params("mean_reversion")
        bb = feat.get("bb_position", 0.5)
        upper = float(p.get("bb_upper", 0.92))
        lower = float(p.get("bb_lower", 0.08))
        if bb >= upper:
            return self._base("mean_reversion", "SELL", (ev["structure"] + ev["liquidity"]) / 2, "Mean reversion from upper band — overstretched")
        if bb <= lower:
            return self._base("mean_reversion", "BUY", (ev["structure"] + ev["liquidity"]) / 2, "Mean reversion from lower band — overstretched")
        return None

    def _range_fade(self, feat: dict, ctx: dict, ev: dict) -> dict | None:
        p = self._params("range_fade")
        if ctx.get("regime") != p.get("regime", "ranging"):
            return None
        boundary = float(p.get("boundary_pct", 0.0015))
        price = feat.get("price", 0)
        support = feat.get("support", price)
        resistance = feat.get("resistance", price)
        if price and abs(price - support) / price < boundary:
            return self._base("range_fade", "BUY", ev["liquidity"], "Range fade — buy support in ranging market")
        if price and abs(resistance - price) / price < boundary:
            return self._base("range_fade", "SELL", ev["liquidity"], "Range fade — sell resistance in ranging market")
        return None

    def _liquidity_sweep(self, feat: dict, ctx: dict, ev: dict) -> dict | None:
        p = self._params("liquidity_sweep")
        min_liq = float(p.get("min_liquidity", 0.65))
        if feat.get("rejection") == "bullish_rejection" and ev["liquidity"] > min_liq:
            return self._base("liquidity_sweep", "BUY", (ev["liquidity"] + ev["volume"]) / 2, "Liquidity sweep below support — bullish rejection")
        if feat.get("rejection") == "bearish_rejection" and ev["liquidity"] > min_liq:
            return self._base("liquidity_sweep", "SELL", (ev["liquidity"] + ev["volume"]) / 2, "Liquidity sweep above resistance — bearish rejection")
        return None

    def _false_breakout(self, feat: dict, ctx: dict, ev: dict) -> dict | None:
        p = self._params("false_breakout")
        upper = float(p.get("bb_upper", 0.85))
        lower = float(p.get("bb_lower", 0.15))
        if feat.get("rejection") == "bearish_rejection" and feat.get("bb_position", 0) > upper:
            return self._base("false_breakout", "SELL", ev["structure"], "False breakout — rejection at highs")
        if feat.get("rejection") == "bullish_rejection" and feat.get("bb_position", 1) < lower:
            return self._base("false_breakout", "BUY", ev["structure"], "False breakdown — rejection at lows")
        return None