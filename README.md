# MT5 Quant OS

Evidence-first MT5 trading agent for Windows: sequential pipeline loops, strategy-pinned entries, live verifier gates, and a real-time dashboard.

**Repo:** https://github.com/banky420star/Quant-os-trade.git  
**Branch:** `blue-guardian`

## Quick start

```powershell
pip install -r requirements.txt
.\scripts\kill_agent.bat
python scripts\preflight.py
python start.py --profile validation
```

Open **http://127.0.0.1:8080**. The validation profile is **read-only** — it connects to MT5 for data/features/journal but sends **zero orders**. Safe to run on any account.

To enable demo trading, use `--profile growth`. To enable live trading, use `--profile 30-real` or `--profile 100` — each requires an explicit opt-in via `execution.explicit_opt_in_danger_zone: true` in the profile.

## Documentation

| Doc | Purpose |
|-----|---------|
| [docs/PHASE1_UPDATE.md](docs/PHASE1_UPDATE.md) | Phase 1 release notes — ops hardening + entry pipeline (`65de9df`) |
| [docs/PHASE2_UPDATE.md](docs/PHASE2_UPDATE.md) | Phase 2 — SQLite state store (dual-write + known issues) |
| [docs/PHASE2.3_FAST_MODE.md](docs/PHASE2.3_FAST_MODE.md)
| [docs/PHASE2.4_LEARNING_LOOP.md](docs/PHASE2.4_LEARNING_LOOP.md) | Phase 2.4 - normalized learning loop (review + bounded config proposals) | | Phase 2.3 - tick-reactive fast scalper layer (presets, gates, pending orders) |
| [docs/HOW_TO_RUN.md](docs/HOW_TO_RUN.md) | Setup, profiles, start/stop, reset, troubleshooting |
| [docs/PIPELINE.md](docs/PIPELINE.md)
| [docs/PIPELINE_REVIEW.md](docs/PIPELINE_REVIEW.md) | Full pipeline review - every loop, status, skip conditions, visual diagram | | Loop diagram, entry flow, hardening roadmap |
| [docs/STRUCTURE.md](docs/STRUCTURE.md) | Repository layout and where things live |
| [obsidian/00 Home.md](obsidian/00%20Home.md) | Team wiki (open `obsidian/` as an Obsidian vault) |
| [docs/research/VERDICT.md](docs/research/VERDICT.md) | Historical backtest audit (negative result reference) |

## Profiles

| Profile | Use |
|---------|-----|
| `validation` | **Default** — read-only MT5 connection, zero orders. Safe everywhere. |
| `growth` | Demo growth campaign — 14 symbols, fraction-Kelly (explicit opt-in) |
| `30-real` | Micro live — XAU, Oil, UK100; requires explicit opt-in |
| `30` | Micro paper gates — no external orders |
| `100` | Small live — XAU+FX, up to 0.02 lot; requires explicit opt-in |
| `live` | Full live plan; requires explicit opt-in |

```powershell
# Safe default (recommended):
python start.py --profile validation

# Demo trading (explicit opt-in):
python start.py --profile growth
```

## Project layout (summary)

```
core/          Business logic (risk, entries, verifier, exposure, …)
loops/         Pipeline agents (data → signal → verifier → execution → …)
dashboard/     Web UI + API (port 8080)
profiles/      YAML profile overlays (30-real, growth, …)
scripts/       Ops utilities (preflight, reset, kill, research)
state/         Runtime JSON (gitignored) — positions, signals, health
tests/         Pytest suite
docs/          Operator docs + generated STATE.md snapshot
obsidian/      Internal team notes (loops, state files, campaigns)
```

See [docs/STRUCTURE.md](docs/STRUCTURE.md) for the full tree.

## Common commands

| Action | Command |
|--------|---------|
| Start (safe default) | `python start.py --profile validation` |
| Start (demo trading) | `python start.py --profile growth` |
| Stop | `.\scripts\kill_agent.bat` |
| Reset session state | Dashboard **Reset state** or `python scripts\reset_session_memory.py` |
| Pre-flight | `python scripts\preflight.py` |
| Safety audit | `python scripts\safety_audit.py` |
| Tests | `python -m pytest tests/ -q` |

## Requirements

- Windows + Python 3.10+
- MetaTrader 5 (same session as the bot)
- Dependencies: `pip install -r requirements.txt`

Local overrides: `config.local.yaml` (gitignored). Never commit credentials.