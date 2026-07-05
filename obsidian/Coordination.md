# Coordination

The bot is a **team of 13 loops** orchestrated as one unit. Use this checklist to verify they are working together.

## Quick check

```bat
python scripts/coordination_check.py
```

Exit code `0` = **COORDINATED**. Report persisted with `--write-state` → `state/coordination.json`.

## Gates

| Gate | Meaning |
|------|---------|
| `canonical_agent_alive` | `state/agent_lock.json` PID is running |
| `supervisor_owner` | `supervisor.json` written by canonical agent |
| `no_stale_loops` | No legacy `forward_test_loop` in supervisor |
| `canonical_loops_present` | All 13 loops including `adaptation_loop` |
| `loop_run_counts_aligned` | All loops within ±2 cycles |
| `arena_14_symbols` | Full growth symbol roster |
| `arena_8_setups` | All classifier setups active |
| `adaptation_ledger` | Culturing ledger fresh |

## Agent lock

`start.py` writes `state/agent_lock.json` on boot. Only that PID may write `supervisor.json` and `runtime_mode.json` (prevents zombie instances from corrupting team state).

## Obsidian map

- [[00 Home]] — team overview
- [[Process Flow]] — data-flow diagram
- [[Pipeline]] — loop order
- `loops/*.md` — per-agent roles

## Learning pipeline

Every closed trade must carry full context for culturing, arena, edge DB, and Kelly:

| Stage | What happens |
|-------|----------------|
| verifier_loop | Approved signals → `signal_archive.json` (durable) |
| execution_loop | Orders stamp `signal_meta` via `snapshot_signal_meta` |
| memory_loop | Enriches new closes → updates `paper_trades.json` → memory + edge + arena |
| adaptation_loop | Rebuilds `forward_test_ledger.json` culturing cells |

Check: `state/learning_health.json` or coordination gate `learning_pipeline`.

## Zombie processes

If old `start.py` instances cannot be killed (access denied), the canonical agent still coordinates via agent lock. Warning: extra dashboard listeners on `:8080` until zombies are stopped manually.