# MT5 Quant OS

Evidence-first MT5 trading agent for Windows: sequential pipeline loops, strategy-pinned entries, live verifier gates, and a real-time dashboard.

**Repo:** https://github.com/banky420star/Quant-os-trade.git  
**Branch:** `blue-guardian`

## Quick start

```powershell
pip install -r requirements.txt
.\scripts\kill_agent.bat
python scripts\preflight.py
python start.py --profile 30-real
```

Open **http://127.0.0.1:8080** — MT5 must be logged in with **Algo Trading** enabled.

## Documentation

| Doc | Purpose |
|-----|---------|
| [docs/HOW_TO_RUN.md](docs/HOW_TO_RUN.md) | Setup, profiles, start/stop, reset, troubleshooting |
| [docs/PIPELINE.md](docs/PIPELINE.md) | Loop diagram, entry flow, hardening roadmap |
| [docs/STRUCTURE.md](docs/STRUCTURE.md) | Repository layout and where things live |
| [obsidian/00 Home.md](obsidian/00%20Home.md) | Team wiki (open `obsidian/` as an Obsidian vault) |
| [docs/research/VERDICT.md](docs/research/VERDICT.md) | Historical backtest audit (negative result reference) |

## Profiles

| Profile | Use |
|---------|-----|
| `30-real` | Micro demo/live — XAU, Oil, UK100; independent per-symbol exposure |
| `30` | Micro paper gates |
| `growth` | Growth campaign |
| `live` | Full $50k live plan |

```powershell
python start.py --profile 30-real
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
| Start (micro live) | `python start.py --profile 30-real` |
| Stop | `.\scripts\kill_agent.bat` |
| Reset session state | Dashboard **Reset state** or `python scripts\reset_session_memory.py` |
| Pre-flight | `python scripts\preflight.py` |
| Tests | `python -m pytest tests/ -q` |

## Requirements

- Windows + Python 3.10+
- MetaTrader 5 (same session as the bot)
- Dependencies: `pip install -r requirements.txt`

Local overrides: `config.local.yaml` (gitignored). Never commit credentials.