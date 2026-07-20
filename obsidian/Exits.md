# Exits

Managed by [[position_manager_loop]] via `core/exit_manager.py`.

## Flow

1. **TP1 partial** — close fraction at first target
2. **Deferred trail** — trail only after partial or 0.75R
3. **Runner** — remainder targets TP2
4. **BE lock** — move stop to lock minimum profit (`lock_profit_usd`)

## Config

`config.yaml` → `trading.exits` per symbol family (XAU tighter ATR trail, etc.)

## State

- `state/position_management.json` — active management decisions
- `state/paper_positions.json` — open positions

## Tests

`tests/test_exit_manager.py`