# Repository structure

Generated/runtime paths are **gitignored** unless noted.

## Top level

| Path | Role |
|------|------|
| `start.py` | Main entry — supervisor, pipeline, dashboard |
| `config.yaml` | Base configuration (tracked) |
| `config.local.yaml` | Machine overrides (gitignored) |
| `requirements.txt` | Python dependencies |
| `README.md` | Project entry point |

### Windows launchers

| Script | What it does |
|--------|----------------|
| `START_AGENT.bat` | Kill dupes → `python start.py` |
| `start30-real.bat` | `start.py --profile 30-real` |
| `start30.bat` / `start30-c*.bat` | Other micro variants |
| `start100.bat` / `start_growth.bat` | Larger account profiles |
| `LAUNCH.bat` | Bot + extra dashboards via `scripts/launch_all.py` |
| `scripts/kill_agent.bat` | Stop all bot/dashboard Python processes |

## Source code

### `core/` — domain logic

| Area | Examples |
|------|----------|
| Pipeline | `pipeline.py`, `supervisor.py` |
| Market | `feature_engine.py`, `market_context.py` |
| Strategy | `decision_engine.py`, `strategy_entry.py`, `entry_pipeline.py` |
| Gates | `verifier.py`, `consensus_gates.py`, `entry_staging.py` |
| Risk | `risk_manager.py`, `exposure.py`, `daily_growth.py` |
| Execution | `mt5_broker.py`, `paper_broker.py`, `position_manager.py` |
| Ops | `audit_log.py`, `ops_alerts.py`, `health_monitor.py` |

### `loops/` — pipeline agents (one file per loop)

Execution order is defined in `core/pipeline.py`:

1. `data_loop` → 2. `feature_loop` → 3. `market_context_loop` → 4. `risk_loop`  
5. `signal_loop` → 6. `verifier_loop` → 7. `execution_loop` → 8. `blue_guardian_loop`  
9. `position_manager_loop` → 10. `memory_loop` → 11. `adaptation_loop` → 12. `trade_log_loop` → 13. `health_loop`

Background (supervisor, separate intervals): `history_loop`, `research_loop`.

### `dashboard/`

- `server.py` — HTTP API reading `state/`
- `index.html` — operator UI

### `profiles/`

YAML overlays merged at startup (`30-real.yaml`, `growth.yaml`, …).

### `scripts/`

| Script | Purpose |
|--------|---------|
| `preflight.py` | Pre-start health checks |
| `reset_session_memory.py` | Clear kill switch, baselines, memory |
| `kill_agent.bat` | Stop processes |
| `launch_all.py` | Full stack launcher |
| `calibrate_*.py` | BE/trail/SLTP research |
| `coordination_check.py` | Multi-system coordination |

### `tests/`

Pytest — run with `python -m pytest tests/ -q`.

### `quant/`

Research modules (cell ranking, adaptation evolution).

## Runtime (gitignored)

### `state/`

Hot JSON written each cycle. Key files:

| File | Writer | Consumer |
|------|--------|----------|
| `features.json` | feature_loop | signal_loop |
| `candidate_signals.json` | signal_loop | verifier_loop |
| `approved_signals.json` | verifier_loop | execution_loop |
| `paper_positions.json` | execution / MT5 sync | risk, verifier, entry pipeline |
| `kill_switch.json` | risk_loop | verifier, execution |
| `health.json` | health_loop | dashboard |
| `account.json` | health / MT5 | risk, sizing |
| `audit_log.jsonl` | verifier, execution, risk | forensics |

### `logs/`

Per-loop log files (`signal_loop.log`, `execution_loop.log`, …).

### `data/history/`

Parquet candle history (incremental downloads).

## Documentation

| Path | Notes |
|------|-------|
| `docs/PHASE1_UPDATE.md` | Phase 1 release notes (ops + entry pipeline) |
| `docs/HOW_TO_RUN.md` | Operator runbook |
| `docs/PIPELINE.md` | Architecture diagrams |
| `docs/STRUCTURE.md` | This file |
| `docs/STATE.md` | Generated snapshot after `run_all.py` / tests |
| `docs/research/VERDICT.md` | Archived research verdict |
| `obsidian/` | Obsidian vault — loop notes, state file index |

## Root artifacts (gitignored)

Research and backtest outputs should not live in git: `*_report*.json`, `*_rseries.json`, `cycle*_*.json`, `*.log`. Re-run scripts to regenerate.