# trade_log_loop

**File:** `loops/trade_log_loop.py`

## Role

Comprehensive per-trade journal: open/close times, setup, R-multiple, drawdown, all fields.

## Output

- `state/trade_log.json`

## Throttle

Rebuilds only when `paper_trades.json` changed (trade closed). Also triggered by [[adaptation_loop]] on new closes.