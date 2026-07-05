# MT5 Quant OS — Team Map

> Open this folder as an Obsidian vault: **File → Open folder as vault** → `mt5_quant_agent/obsidian`

## Status

- **Last check:** COORDINATED · PID 7224 · campaign `growth-all-2026-07-02`
- Run: `python scripts/coordination_check.py` → see [[Coordination]]
- Live state: [[State Files]]
- Pipeline order: [[Pipeline]]

## Core systems

| System | Role | State |
|--------|------|-------|
| [[Pipeline]] | Sequential loop orchestration | `state/supervisor.json` |
| [[Growth Campaign]] | +35%/day, 14 symbols, full Kelly | `state/growth_campaign.json` |
| [[Strategy Arena]] | 8 setups compete; triggers + outcomes | `state/strategy_arena.json` |
| [[Exits]] | Partial TP, BE lock, runner trails | `state/position_management.json` |
| [[Adaptation]] | Culturing vetoes, BE/trail calibration | `state/forward_test_ledger.json` |

## The team (loops)

1. [[data_loop]] → candles, broker symbols
2. [[feature_loop]] → indicators, structure
3. [[market_context_loop]] → regime, session, volatility
4. [[risk_loop]] → exposure, kill switch, daily growth
5. [[signal_loop]] → candidates + arena triggers
6. [[verifier_loop]] → culturing vetoes, confidence gates
7. [[execution_loop]] → MT5 orders
8. [[blue_guardian_loop]] → funded-account shields (disabled in growth)
9. [[position_manager_loop]] → exits, partial TP, trails
10. [[memory_loop]] → edge DB + arena outcomes
11. [[adaptation_loop]] → ledger refresh + organic tuning
12. [[trade_log_loop]] → comprehensive trade journal
13. [[health_loop]] → MT5 ping, heartbeat

## Process

See [[Process Flow]] for the full data-flow diagram and coordination gates.