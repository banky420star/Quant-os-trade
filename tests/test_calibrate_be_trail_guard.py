"""Regression tests for the PASS-18 BE/trail calibration hardening:

  * The grid was expanded to include the tighter/earlier trailing candidates
    the MFE exit audit flagged as missing (BE_TRIG 0.2, TR_ACT 0.3/0.4,
    TR_DIST 0.1/0.15).
  * The trust gate now requires a selection-robust IMPROVEMENT-over-seed CI95
    (``impr_lo > 0``) in addition to the level CI95 (``ci_lo > 0``) — the guard
    against selection artifacts across the larger grid.
  * Every evaluated symbol carries an ``improvement_ci95`` field.

These lock in the wiring so a revert is caught. The empirical behavior (US30m
newly crosses; the 3 prior trusted survive; USOILm rejected despite positive
improvement because its level CI95 crosses 0) is demonstrated by the dry-run.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import calibrate_be_trail as mod  # noqa: E402


def test_grid_expanded_with_tighter_earlier_trailing():
    """PASS-18: the grid must include the tighter/earlier candidates the exit
    audit said were missing (otherwise give-back symbols can never find them)."""
    assert 0.2 in mod.BE_TRIG_GRID          # earlier BE trigger
    assert 0.3 in mod.TR_ACT_GRID           # earlier trail activation
    assert 0.4 in mod.TR_ACT_GRID
    assert 0.1 in mod.TR_DIST_GRID          # tighter trail distance
    assert 0.15 in mod.TR_DIST_GRID
    # constraint ta > bt still satisfiable (some activation above some trigger)
    assert any(ta > bt for ta in mod.TR_ACT_GRID for bt in mod.BE_TRIG_GRID)


def test_bootstrap_improvement_ci95_returns_pair_of_floats():
    """The improvement-CI95 helper must return a (lo, hi) pair of floats on a
    small synthetic pre list (and not crash)."""
    pre = [(0.2, 1.4, 1.2, 1.0), (1.0, 0.0, -1.0, 1.0), (0.1, 0.8, 0.6, 1.0),
           (0.3, 2.1, 1.8, 1.5), (1.0, 0.05, -1.0, 1.0), (0.0, 1.1, 0.9, 1.0)]
    lo, hi = mod._bootstrap_improvement_ci95(
        pre, be_trig=0.2, be_lock=0.15, tr_act=0.3, tr_dist=0.1,
        s_be_trig=0.5, s_be_lock=0.1, s_tr_act=0.75, s_tr_dist=0.35,
        n_boot=200)
    assert isinstance(lo, float) and isinstance(hi, float)
    assert lo <= hi


def test_bootstrap_improvement_ci95_seed_equals_best_is_zero():
    """When best params == seed params, the improvement distribution is
    identically zero -> lo == hi == 0.0 (no false positive from the guard)."""
    pre = [(0.2, 1.4, 1.2, 1.0), (1.0, 0.0, -1.0, 1.0), (0.1, 0.8, 0.6, 1.0),
           (0.3, 2.1, 1.8, 1.5), (1.0, 0.05, -1.0, 1.0), (0.0, 1.1, 0.9, 1.0)]
    lo, hi = mod._bootstrap_improvement_ci95(
        pre, be_trig=0.3, be_lock=0.1, tr_act=0.5, tr_dist=0.2,
        s_be_trig=0.3, s_be_lock=0.1, s_tr_act=0.5, s_tr_dist=0.2,
        n_boot=200)
    assert lo == 0.0 and hi == 0.0


def test_calibrate_emits_improvement_ci95_field(monkeypatch):
    """Every evaluated (n>=MIN_N) symbol must carry an improvement_ci95 field,
    and the trust gate must require impr_lo>0 (a seed-beating but non-robust
    improvement is not trusted). Uses a synthetic trade log."""
    import logging
    log = logging.getLogger("test_calibrate_be_trail_guard")

    # Synthetic trades: mae_R/mfe_R/r_multiple/tp1 fields. Build a symbol with
    # enough trades (>= APPLY_MIN_N) where the best beats the seed.
    sym_trades = []
    for i in range(60):
        # mostly small winners that peak then give back -> tight trailing helps
        sym_trades.append({
            "symbol": "FAKEm", "entry": 100.0, "sl_initial": 99.0, "tp1": 101.5,
            "mae_R": 0.2, "mfe_R": 1.3, "r_multiple": 0.2,
        })
    fake_log = {"trades": sym_trades}

    monkeypatch.setattr(mod, "read_json_state", lambda name, default=None: fake_log)

    config = {"trading": {"break_even": {"trigger_atr_mult": 0.5, "lock_profit_atr_mult": 0.1,
                                         "per_symbol": {}},
                          "trailing": {"activation_atr_mult": 0.75, "trail_atr_mult": 0.35,
                                       "per_symbol": {}}}}
    out = mod.calibrate(config, log)
    assert "FAKEm" in out["symbols"]
    rec = out["symbols"]["FAKEm"]
    assert "improvement_ci95" in rec
    assert isinstance(rec["improvement_ci95"], list) and len(rec["improvement_ci95"]) == 2
    # trust gate requires BOTH ci_lo>0 AND impr_lo>0
    if rec["trusted"]:
        assert rec["ci95"][0] > 0.0 and rec["improvement_ci95"][0] > 0.0


def _set_mtime(path: Path, mtime: float) -> None:
    import os
    path.write_text("{}")
    os.utime(path, (mtime, mtime))


def test_reset_authoritative_true_when_reset_newer_than_live(tmp_path, monkeypatch):
    """If experiment_reset.json was modified AFTER symbol_be_trail_live.json,
    a deliberate fresh-slate reset happened after the overrides were derived ->
    _reset_authoritative() is True (reset wins, do not carry forward)."""
    monkeypatch.setattr(mod, "STATE", tmp_path)
    _set_mtime(tmp_path / "symbol_be_trail_live.json", 1000.0)
    _set_mtime(tmp_path / "experiment_reset.json", 2000.0)
    assert mod._reset_authoritative() is True


def test_reset_authoritative_false_when_reset_older_than_live(tmp_path, monkeypatch):
    """If the live file was written AFTER the last reset, the overrides belong
    to the current experiment epoch -> not authoritative -> carry forward safe."""
    monkeypatch.setattr(mod, "STATE", tmp_path)
    _set_mtime(tmp_path / "symbol_reset.json", 1000.0)  # noqa: F841 (decoy)
    _set_mtime(tmp_path / "experiment_reset.json", 1000.0)
    _set_mtime(tmp_path / "symbol_be_trail_live.json", 2000.0)
    assert mod._reset_authoritative() is False


def test_reset_authoritative_false_when_no_reset_file(tmp_path, monkeypatch):
    """No experiment_reset.json -> never reset (or accidental truncation only)
    -> not authoritative -> carry forward the trusted lever (the guard's purpose)."""
    monkeypatch.setattr(mod, "STATE", tmp_path)
    _set_mtime(tmp_path / "symbol_be_trail_live.json", 1000.0)
    assert mod._reset_authoritative() is False


def test_calibrate_respects_authoritative_reset(tmp_path, monkeypatch):
    """PASS-20: when a data-lab reset is authoritative, prior trusted overrides
    must NOT be carried forward -- the user chose a fresh slate. Thin trade log
    (n<MIN_N) so no new entry is produced; without the reset check the guard
    would carry FAKE_TRUSTED forward, but the reset must suppress that."""
    import logging
    log = logging.getLogger("test_calibrate_be_trail_guard")
    monkeypatch.setattr(mod, "STATE", tmp_path)
    # reset NEWER than live file -> authoritative
    _set_mtime(tmp_path / "symbol_be_trail_live.json", 1000.0)
    _set_mtime(tmp_path / "experiment_reset.json", 2000.0)
    # empty trade log -> main loop produces no entries
    monkeypatch.setattr(mod, "read_json_state", lambda name, default=None: {"trades": []})
    existing = {"symbols": {"FAKEm": {"trusted": True, "n": 80,
            "break_even": {"trigger_atr_mult": 0.2, "lock_profit_atr_mult": 0.15},
            "trailing": {"activation_atr_mult": 0.3, "trail_atr_mult": 0.1},
            "expectancy_r": 0.31, "ci95": [0.1, 0.5], "improvement_ci95": [0.05, 0.4],
            "reason": "data-driven"}}}
    out = mod.calibrate({"trading": {}}, log, existing=existing)
    assert "FAKEm" not in out["symbols"]  # reset won; not carried forward


def test_calibrate_carries_forward_when_no_reset(tmp_path, monkeypatch):
    """PASS-20 inverse: with NO authoritative reset (accidental truncation only),
    the prior trusted override IS carried forward -- the original guard purpose."""
    import logging
    log = logging.getLogger("test_calibrate_be_trail_guard")
    monkeypatch.setattr(mod, "STATE", tmp_path)
    _set_mtime(tmp_path / "symbol_be_trail_live.json", 2000.0)
    # no experiment_reset.json -> not authoritative
    monkeypatch.setattr(mod, "read_json_state", lambda name, default=None: {"trades": []})
    existing = {"symbols": {"FAKEm": {"trusted": True, "n": 80,
            "break_even": {"trigger_atr_mult": 0.2, "lock_profit_atr_mult": 0.15},
            "trailing": {"activation_atr_mult": 0.3, "trail_atr_mult": 0.1},
            "expectancy_r": 0.31, "ci95": [0.1, 0.5], "improvement_ci95": [0.05, 0.4],
            "reason": "data-driven"}}}
    out = mod.calibrate({"trading": {}}, log, existing=existing)
    assert "FAKEm" in out["symbols"]
    assert out["symbols"]["FAKEm"].get("trusted") is True
    assert out["symbols"]["FAKEm"].get("preserved") is True