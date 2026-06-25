"""Signal verification — reject weak or dangerous trades before execution."""

from __future__ import annotations

import logging
from typing import Any

from core.exposure import check_exposure_limits
from core.dynamic_entry import evaluate_dynamic_entry, symbol_capacity_available
from core.trade_limits import is_duplicate_position, unlimited_trades
from core.utils import utc_now_iso


class Verifier:
    """Bouncer at the velvet rope — approve or reject candidate signals."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("verifier")
        self.aggressive = bool(config.get("trading", {}).get("aggressive_mode", False))
        filters = config.get("filters", {})
        self.min_volume_ratio = float(filters.get("min_volume_ratio", 0.8))
        if self.aggressive:
            self.min_volume_ratio = min(self.min_volume_ratio, 0.3)
        self.spread_mult = float(filters.get("spread_mult", 2.0 if self.aggressive else 1.0))

    def verify_batch(
        self,
        candidates: list[dict[str, Any]],
        features_data: dict[str, Any],
        active_signals: list[dict[str, Any]] | None = None,
        kill_switch: bool = False,
        spread_data: dict[str, float] | None = None,
        equity: float | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Verify all candidates; return (approved, rejected)."""
        active_signals = active_signals or []
        spread_data = spread_data or {}
        features = features_data.get("symbols", {})
        if equity is None:
            equity = float(self.config.get("execution", {}).get("starting_cash", 1000))

        approved: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        for signal in candidates:
            feat = features.get(signal["symbol"], {})
            spread_pts = spread_data.get(signal["symbol"], 0.0)
            result = self._verify_one(signal, feat, active_signals, kill_switch, spread_pts, equity)
            if result["approved"]:
                approved.append(result)
            else:
                rejected.append(result)

        self.logger.info("Verified %d signals: %d approved, %d rejected", len(candidates), len(approved), len(rejected))
        return approved, rejected

    def _verify_one(
        self,
        signal: dict[str, Any],
        feat: dict[str, Any],
        active_signals: list[dict[str, Any]],
        kill_switch: bool,
        spread_pts: float,
        equity: float,
    ) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        failures: list[str] = []

        checks["valid_levels"] = self._check_valid_levels(signal)
        checks["confidence"] = signal.get("confidence", 0) >= self.config["signals"]["min_confidence"]
        checks["timeframe_alignment"] = self._check_timeframe_alignment(signal, feat)
        checks["not_buying_resistance"] = self._check_not_buying_resistance(signal, feat)
        checks["not_selling_support"] = self._check_not_selling_support(signal, feat)
        checks["spread_safe"] = self._check_spread(signal["symbol"], spread_pts)
        checks["atr_safe"] = feat.get("atr_ratio", 0) >= self.config["filters"]["min_atr_ratio"]
        checks["volume_safe"] = feat.get("volume_ratio", 0) >= self.min_volume_ratio
        min_rr = float(self.config.get("signals", {}).get("min_risk_reward", 1.2))
        checks["risk_reward_safe"] = self._check_risk_reward(signal, min_rr=min_rr)
        if unlimited_trades(self.config):
            exposure_ok, exposure_details = True, {"unlimited_trades": True}
            checks["exposure_safe"] = True
        else:
            exposure_ok, exposure_details = check_exposure_limits(active_signals, signal, equity, self.config)
            checks["exposure_safe"] = exposure_ok
            if not exposure_ok:
                failures.append("exposure_limit_exceeded")

        checks["no_duplicate"] = not is_duplicate_position(
            self.config,
            signal,
            active_signals,
        )
        checks["symbol_capacity"] = symbol_capacity_available(
            self.config,
            signal["symbol"],
            active_signals,
        )
        dyn_ok, adjusted_signal, dyn_reason = evaluate_dynamic_entry(
            self.config,
            signal,
            active_signals,
            feat,
        )
        checks["dynamic_entry"] = dyn_ok
        if not dyn_ok and dyn_reason:
            failures.append(dyn_reason)

        checks["kill_switch_safe"] = not kill_switch

        for name, passed in checks.items():
            if not passed and name != "exposure_safe":
                failures.append(name)

        approved = len(failures) == 0
        record = {
            "signal_id": signal["signal_id"],
            "symbol": signal["symbol"],
            "side": signal.get("side"),
            "setup_type": signal.get("setup_type"),
            "approved": approved,
            "checks": checks,
            "failures": failures,
            "spread_points": spread_pts,
            "confidence": signal.get("confidence"),
            "reason": signal.get("reason"),
            "verified_at": utc_now_iso(),
            "exposure": exposure_details,
        }
        if approved:
            record["approved_at"] = utc_now_iso()
            record["signal"] = adjusted_signal if dyn_ok else signal
        else:
            record["rejected_at"] = utc_now_iso()
            record["rejection_reason"] = "; ".join(failures)

        return record

    def _check_valid_levels(self, signal: dict[str, Any]) -> bool:
        entry = signal.get("entry")
        sl = signal.get("sl")
        tp1 = signal.get("tp1")
        if not all(isinstance(v, (int, float)) and v > 0 for v in (entry, sl, tp1)):
            return False
        if signal.get("side") == "BUY":
            return sl < entry < tp1
        return tp1 < entry < sl

    def _check_timeframe_alignment(self, signal: dict[str, Any], feat: dict[str, Any]) -> bool:
        m5 = feat.get("m5_trend", "neutral")
        m15 = feat.get("m15_trend", "neutral")
        if m5 == "bullish" and m15 == "bearish" and signal.get("side") == "BUY":
            return False
        if m5 == "bearish" and m15 == "bullish" and signal.get("side") == "SELL":
            return False
        return True

    def _check_not_buying_resistance(self, signal: dict[str, Any], feat: dict[str, Any]) -> bool:
        if signal.get("side") != "BUY":
            return True
        price = feat.get("price", signal.get("entry", 0))
        resistance = feat.get("resistance", price * 1.01)
        return abs(resistance - price) / price > 0.001 if price else True

    def _check_not_selling_support(self, signal: dict[str, Any], feat: dict[str, Any]) -> bool:
        if signal.get("side") != "SELL":
            return True
        price = feat.get("price", signal.get("entry", 0))
        support = feat.get("support", price * 0.99)
        return abs(price - support) / price > 0.001 if price else True

    def _check_spread(self, symbol: str, spread_pts: float) -> bool:
        max_spread = float(self.config["filters"]["max_spread_points"].get(symbol, 999))
        return spread_pts <= max_spread * self.spread_mult

    def _check_risk_reward(self, signal: dict[str, Any], min_rr: float = 1.2) -> bool:
        entry = signal.get("entry", 0)
        sl = signal.get("sl", 0)
        tp1 = signal.get("tp1", 0)
        risk = abs(entry - sl)
        reward = abs(tp1 - entry)
        if risk <= 0:
            return False
        return (reward / risk) >= min_rr

