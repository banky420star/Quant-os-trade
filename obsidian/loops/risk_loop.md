# risk_loop

**File:** `loops/risk_loop.py`

## Inputs

- Account equity (`state/account.json`)
- Open positions
- [[Growth Campaign]] daily targets

## Outputs

- `risk_state.json`
- `kill_switch.json`
- `daily_growth.json`
- `equity_history.json`

## Gates

Blocks [[execution_loop]] when kill switch tripped or daily loss pause active.