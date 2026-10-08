from core.jev_decision_bus import JevDecisionBus, normalize_candidate
from core.jev_ab_report import build_veto_report, match_shadow_to_trades

def test_strong_buy_and_weak_wait():
    bus=JevDecisionBus({"jev":{"provider":"heuristic"}})
    s1=normalize_candidate({"symbol":"XAUUSDm","side":"BUY","confidence":82,"trade_score":78,"policy_score":72,"entry_quality":80},{"symbols":{"XAUUSDm":{}}},{"market_context":{"symbols":{"XAUUSDm":{"regime":"trend"}}}})
    assert bus.decide(s1).action=="BUY"
    s2=normalize_candidate({"symbol":"EURUSDm","side":"SELL","confidence":35,"trade_score":30,"policy_score":30,"entry_quality":35},{"symbols":{"EURUSDm":{}}},{"market_context":{"symbols":{"EURUSDm":{"regime":"chop"}}}})
    assert bus.decide(s2).action=="WAIT"

def test_ab_veto_is_conservative():
    shadow=[
        {"timestamp":"2026-10-01T10:00:00+00:00","symbol":"XAUUSDm","candidate_side":"BUY","state":{"candidate":{"signal_id":"a","side":"BUY"}},"decision":{"action":"BUY"}},
        {"timestamp":"2026-10-01T11:00:00+00:00","symbol":"XAUUSDm","candidate_side":"BUY","state":{"candidate":{"signal_id":"b","side":"BUY"}},"decision":{"action":"WAIT"}},
        {"timestamp":"2026-10-01T12:00:00+00:00","symbol":"XAUUSDm","candidate_side":"SELL","state":{"candidate":{"signal_id":"c","side":"SELL"}},"decision":{"action":"BUY"}},
    ]
    trades=[
        {"signal_id":"a","symbol":"XAUUSDm","side":"BUY","opened_at":"2026-10-01T10:00:10+00:00","pnl":10,"r_multiple":1.0},
        {"signal_id":"b","symbol":"XAUUSDm","side":"BUY","opened_at":"2026-10-01T11:00:10+00:00","pnl":-8,"r_multiple":-0.8},
        {"signal_id":"c","symbol":"XAUUSDm","side":"SELL","opened_at":"2026-10-01T12:00:10+00:00","pnl":5,"r_multiple":0.5},
    ]
    matches=match_shadow_to_trades(shadow,trades)
    report=build_veto_report(matches)
    assert report["matched_trades"]==3
    assert report["jev_veto_shadow"]["trades"]==1
    assert report["avoided_losses"]==1
    assert report["skipped_winners"]==1
    assert report["baseline"]["total_pnl"]==7
    assert report["jev_veto_shadow"]["total_pnl"]==10
