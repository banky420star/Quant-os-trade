"""Setup trigger catalog and classifier param wiring."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.setup_classifier import SetupClassifier
from core.setup_triggers import SETUP_ORDER, describe_trigger, export_catalog, trigger_params, trigger_rules


def test_all_eight_setups_in_catalog():
    from core.utils import load_config
    config = load_config()
    cat = export_catalog(config)
    names = [s["setup_type"] for s in cat["setups"]]
    assert names == list(SETUP_ORDER)
    # 2026-07-31 — 8 legacy setups + 38 specialized setups (2 ICT killzone +
    # 2 symbol-specific breakout iter2 + 2 US index iter3 + 2 EU/Tokyo iter4 +
    # 2 London-close/Sydney iter5 + 2 London-morning/NY-lunch iter6 +
    # 2 session-gated BB-reversion iter7 + 2 session-gated stoch-cross iter12 +
    # 2 session-gated vol-expansion iter13 + 2 session-gated volume-spike iter14 +
    # 2 session-gated MTF-alignment iter15 + 2 session-gated stoch-reversion iter16 +
    # 2 session-gated HTF-trend-filtered breakout iter18 +
    # 2 session-gated BB-squeeze breakout iter19 +
    # 2 session-gated volume-confirmed breakout iter20 +
    # 2 session-gated RSI-reversion iter21 +
    # 2 session-gated MACD-cross iter22 +
    # 2 session-gated CCI-reversion iter23 +
    # 2 session-gated MFI-reversion iter24 +
    # 2 session-gated ADX-trend iter25 +
    # 2 session-gated OBV-cross iter26 +
    # 2 session-gated ATR-pct-breakout iter27 +
    # 2 session-gated CMF-continuation iter28 +
    # 2 session-gated triple-confirm iter29 +
    # 2 session-gated ADX+CMF 2-way compound iter30 +
    # 2 session-gated ADX+ATR-pct 2-way compound iter31 +
    # 2 session-gated ATR-pct+CMF 2-way compound iter32 +
    # 2 session-gated VWAP-band reversion iter34 +
    # 2 session-gated Supertrend-flip continuation iter35 +
    # 2 session-gated Ichimoku-TK-cross continuation iter36 +
    # 2 session-gated Fair-Value-Gap imbalance continuation iter37 +
    # 2 session-gated 2-bar Engulfing candlestick reversal iter38 +
    # 2 session-gated 3-bar Inside-Bar breakout iter39 +
    # 2 session-gated consecutive-close-streak continuation iter40 +
    # 2 session-gated ICT/SMC Order-Block displacement continuation iter41 +
    # 2 session-gated Heikin-Ashi smoothed-candle continuation iter42)
    # = 80. The specialized seventy-two span 34 entry models and are opt-in via
    # signals.specialized_setups.enabled but always in the catalog.
    assert len(names) == 80
    # the original 8 legacy setups are all still present, in order, first
    legacy_8 = names[:8]
    assert legacy_8 == [
        "trend_continuation", "pullback", "breakout", "compression_breakout",
        "range_fade", "liquidity_sweep", "mean_reversion", "false_breakout",
    ]
    assert set(names[8:]) == {
        "silver_bullet", "london_judas", "oil_orb", "asia_range_breakout",
        "us_open_orb", "prev_day_breakout", "eu_open_orb", "tokyo_open_orb",
        "london_close_reversal", "sydney_open_orb", "london_morning_breakout",
        "ny_lunch_reversal", "london_bb_reversion", "ny_bb_reversion",
        "london_stoch_cross", "ny_stoch_cross",
        "london_vol_expansion", "ny_vol_expansion",
        "london_volume_spike", "ny_volume_spike",
        "london_mtf_align", "ny_mtf_align",
        "london_stoch_reversion", "ny_stoch_reversion",
        "london_htf_breakout", "ny_htf_breakout",
        "london_squeeze_breakout", "ny_squeeze_breakout",
        "london_volume_breakout", "ny_volume_breakout",
        "london_rsi_reversion", "ny_rsi_reversion",
        "london_macd_cross", "ny_macd_cross",
        "london_cci_reversion", "ny_cci_reversion",
        "london_mfi_reversion", "ny_mfi_reversion",
        "london_adx_trend", "ny_adx_trend",
        "london_obv_cross", "ny_obv_cross",
        "london_atr_pct_breakout", "ny_atr_pct_breakout",
        "london_cmf_continuation", "ny_cmf_continuation",
        "london_triple_confirm", "ny_triple_confirm",
        "london_adx_cmf", "ny_adx_cmf",
        "london_adx_atr_pct", "ny_adx_atr_pct",
        "london_atr_pct_cmf", "ny_atr_pct_cmf",
        "london_vwap_reversion", "ny_vwap_reversion",
        "london_supertrend_flip", "ny_supertrend_flip",
        "london_ichimoku_tk_cross", "ny_ichimoku_tk_cross",
        "london_fvg", "ny_fvg",
        "london_engulfing", "ny_engulfing",
        "london_inside_bar", "ny_inside_bar",
        "london_close_streak", "ny_close_streak",
        "london_order_block", "ny_order_block",
        "london_ha", "ny_ha",
    }
    for entry in cat["setups"]:
        assert entry.get("trigger_rules")
        assert entry.get("trigger_summary")
        assert entry.get("trigger_params") is not None


def test_trigger_params_override_from_config():
    from core.utils import load_config
    config = load_config()
    p = trigger_params(config, "mean_reversion")
    assert p["bb_upper"] == 0.92
    assert p["bb_lower"] == 0.08
    assert len(trigger_rules("liquidity_sweep")) >= 2


def test_classifier_uses_config_thresholds():
    from core.utils import load_config
    config = load_config()
    config["intelligence"]["setup_triggers"]["mean_reversion"] = {"bb_upper": 0.80, "bb_lower": 0.20}
    clf = SetupClassifier(config)
    feat = {
        "bb_position": 0.85,
        "m5_trend": "neutral",
        "price": 100.0,
        "support": 99.0,
        "resistance": 101.0,
    }
    ctx = {"regime": "ranging", "phase": "normal", "move_type": "none", "market_regime": {"primary": "range"}}
    ev = {"trend": 0.5, "structure": 0.7, "momentum": 0.5, "volume": 0.5, "liquidity": 0.6, "volatility": 0.5, "risk": 0.3}
    hit = clf._mean_reversion(feat, ctx, ev)
    assert hit is not None
    assert hit["side"] == "SELL"


def test_describe_trigger_not_empty():
    from core.utils import load_config
    config = load_config()
    for name in SETUP_ORDER:
        assert describe_trigger(name, config)