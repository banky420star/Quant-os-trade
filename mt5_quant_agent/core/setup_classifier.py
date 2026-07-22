"""Setup Classifier — classify opportunity type, not generic BUY/SELL."""

from __future__ import annotations

import logging
from typing import Any

from core.setup_library import get_setup, is_regime_compatible
from core.setup_triggers import trigger_params

SETUP_TYPES = (
    "breakout",
    "pullback",
    "trend_continuation",
    "range_fade",
    "liquidity_sweep",
    "mean_reversion",
    "false_breakout",
    "compression_breakout",
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