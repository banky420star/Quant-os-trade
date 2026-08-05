from __future__ import annotations

import json


def test_reset_clears_derived_state_without_mt5_side_effects(tmp_path, monkeypatch):
    import scripts.reset_data_lab_experiment as reset_mod

    state = tmp_path / "state"
    state.mkdir()
    reset_mod.STATE = state
    (state / "trade_log.json").write_text(json.dumps({"trades": [1]}), encoding="utf-8")
    (state / "mt5_orders.json").write_text(json.dumps({"orders": [1]}), encoding="utf-8")
    monkeypatch.setattr(reset_mod, "ROOT", tmp_path)
    monkeypatch.setattr(reset_mod, "STATE", state)
    monkeypatch.setattr(reset_mod, "_utc_now", lambda: "2026-08-04T12:00:00+00:00")
    monkeypatch.setattr(reset_mod, "shutil", reset_mod.shutil)
    monkeypatch.setattr(reset_mod, "_assert_bot_stopped", lambda: None)

    # Avoid loading the real profile; the default reset still verifies the
    # execution mode before touching local derived ledgers.
    monkeypatch.setattr(
        "core.utils.load_config",
        lambda: {"active_profile": "data-lab", "execution": {"mode": "mt5"}},
    )
    result = reset_mod.reset(backup=False, flatten_demo=False)
    assert "trade_log.json" in result["cleared"]
    assert "specialized_shadow_ledger.jsonl" in result["missing"]
    assert not (state / "trade_log.json").exists()
    assert not (state / "mt5_orders.json").exists()
    marker = json.loads((state / "experiment_reset.json").read_text(encoding="utf-8"))
    assert marker["live_mt5_orders_touched"] is False
    assert marker["live_mt5_positions_touched"] is False
