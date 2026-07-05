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
    assert len(names) == 8
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