# Growth Campaign

Campaign: `growth-all-2026-07-02` · Mode: `state/runtime_mode.json`

## Targets

- **Daily growth**: +35%
- **Kelly**: full (`kelly_fraction: 1.0`)
- **Account**: Exness demo `435656990`
- **Symbols**: 14 (see `core/campaign_symbols.py`)

## Symbol roster

XAUUSDm · USOILm · BTCUSDm · EURUSDm · GBPUSDm · USDJPYm · USDCHFm · AUDUSDm · US500m · US30m · NAS100m · UK100m · FR40m · JP225m

## State files

| File | Purpose |
|------|---------|
| `growth_campaign.json` | Campaign metadata, targets |
| `daily_growth.json` | Day-start equity, progress |
| `runtime_mode.json` | growth / arena / Kelly flags |

## Launch / reset

```bat
python scripts/run_growth_campaign.py
python start.py
```

## Links

- [[Strategy Arena]] — all setups compete on all symbols
- [[risk_loop]] — daily loss pause, drawdown kill
- [[Exits]] — aggressive profit capture for growth mode