# position_manager_loop

**File:** `loops/position_manager_loop.py`

## Role

Manage open positions: partial TP, break-even, trailing stops, runner exits.

## Related

- [[Exits]] — `core/exit_manager.py`
- `state/position_management.json`

## Downstream

Closes feed `paper_trades.json` → [[memory_loop]]