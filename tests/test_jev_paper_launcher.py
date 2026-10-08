"""Safe one-cycle launcher refuses live/stale/missing inputs."""
from datetime import datetime, timedelta, timezone

import pytest
from scripts import jev_paper_once


def _config(mode="paper",live=False):
    return {"execution":{"mode":mode,"live_trading_enabled":live},
            "jev":{"enabled":True,"mode":"paper_gate",
                   "max_input_age_seconds":180}}


def test_paper_launcher_refuses_live():
    with pytest.raises(RuntimeError,match="Refusing"):
        jev_paper_once._assert_paper(_config("mt5",True))
    with pytest.raises(RuntimeError,match="Refusing"):
        jev_paper_once._assert_paper(_config("paper",True))


def test_preflight_refuses_missing_data(monkeypatch):
    monkeypatch.setattr(jev_paper_once,"read_json_state",
                        lambda name,default=None: default)
    with pytest.raises(RuntimeError,match="Missing features"):
        jev_paper_once.preflight(_config())


def test_preflight_refuses_stale_data(monkeypatch):
    old=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    fake={
      "features.json":{"timestamp":old,"symbols":{"XAUUSDm":{"price":2600}}},
      "candidate_signals.json":{"timestamp":old,"candidates":[{"symbol":"XAUUSDm","side":"BUY"}]},
    }
    monkeypatch.setattr(jev_paper_once,"read_json_state",
                        lambda name,default=None:fake.get(name,default))
    with pytest.raises(RuntimeError,match="stale"):
        jev_paper_once.preflight(_config())


def test_preflight_allows_only_fresh_paper(monkeypatch):
    fresh=datetime.now(timezone.utc).isoformat()
    fake={
      "features.json":{"timestamp":fresh,"symbols":{"XAUUSDm":{"price":2600}}},
      "candidate_signals.json":{"timestamp":fresh,"candidates":[{"symbol":"XAUUSDm","side":"BUY"}]},
    }
    monkeypatch.setattr(jev_paper_once,"read_json_state",
                        lambda name,default=None:fake.get(name,default))
    assert jev_paper_once.preflight(_config())["candidate_count"]==1
