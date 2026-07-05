# Pipeline

Source: `core/pipeline.py` · Heartbeat: `state/supervisor.json`

## Order (canonical)

```
data_loop
  → feature_loop
  → market_context_loop
  → risk_loop
  → signal_loop
  → verifier_loop
  → execution_loop
  → blue_guardian_loop
  → position_manager_loop
  → memory_loop
  → adaptation_loop   # subsumes legacy forward_test_loop
  → trade_log_loop
  → health_loop
```

## Services (supervisor)

| Service | Interval | Log |
|---------|----------|-----|
| trading_pipeline | 45s (`app.loop_interval_seconds`) | `logs/system.log` |
| history_engine | 300s | `logs/history_loop.log` |
| research_engine | 1800s | `logs/research_loop.log` |

## Coordination rules

- **One bot**: only one process may bind dashboard `:8080`
- **No stale loops**: supervisor must show `adaptation_loop`, not `forward_test_loop`
- **Aligned run counts**: all canonical loops within ±2 cycles
- Check: `python scripts/coordination_check.py`

## Related

- [[Process Flow]]
- [[State Files]]
- Per-loop notes in `loops/`