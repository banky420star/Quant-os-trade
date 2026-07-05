# data_loop

**File:** `loops/data_loop.py` · **Log:** `logs/data_loop.log`

## Inputs

- MT5 terminal (`core/mt5_terminal_manager.py`)
- `config.yaml` → `mt5.symbols`

## Outputs

- `latest_candles.json`
- `broker_symbols.json`
- Session alignment logged each cycle

## Downstream

→ [[feature_loop]]