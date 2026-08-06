# MT5 Quant OS — How to Run

Back to [README](../README.md).

Repo: https://github.com/banky420star/Quant-os-trade.git  
Active branch: `blue-guardian`

## Prerequisites

| Requirement | Notes |
|-------------|--------|
| **Windows** | MT5 Python API is Windows-only |
| **Python 3.10+** | Tested on 3.14; use a venv if you prefer |
| **MetaTrader 5** | Logged into the account you want to trade |
| **Algo Trading ON** | MT5 toolbar → **Algo Trading** must be enabled (green) |
| **Same session** | Bot attaches to the MT5 terminal in your Windows session (use `MT5Agent` terminal if running headless/RDP) |

Install dependencies from the project root. The Windows launcher selects a Python interpreter that can import `MetaTrader5` before starting the bot:

```powershell
cd Quant-os-trade
C:\Python314\python.exe -m pip install -r requirements.txt
```

If you use another Python installation, install the same requirements into that exact interpreter. The selector checks `MT5_PYTHON_OVERRIDE`, then `C:\Python314\python.exe`, then `py -3.14`, then `python`. To force a different interpreter:

```bat
set MT5_PYTHON_OVERRIDE=C:\path\to\python.exe
START_AGENT.bat
```

## First-time setup

1. **Clone and enter the repo**

   ```powershell
   git clone https://github.com/banky420star/Quant-os-trade.git
   cd Quant-os-trade
   git checkout blue-guardian
   ```

2. **Local overrides (optional, not committed)**

   Create `config.local.yaml` in the project root if you need machine-specific settings:

   ```yaml
   mt5:
     use_logged_in_account: true
     ignore_stored_credentials: true
   app:
     remote_access:
       enabled: false
   ```

   The bot uses **whichever account is logged into your MT5 window**. Do not put passwords in config files.

3. **Log into MT5** on the terminal the bot will attach to (`C:\Users\Administrator\MT5Agent\terminal64.exe` or your default MT5).

4. **Enable Algo Trading** in that terminal.

## Profiles

Profiles live in `profiles/*.yaml`. Pick one with `--profile`:

| Profile | Use case |
|---------|----------|
| `validation` | **Default — read-only**. MT5 data/features/journal only. Zero orders. Safe everywhere. |
| `30-real` | Micro live — XAU, Oil, UK100; requires explicit opt-in |
| `30` | Micro paper-style gates — no external orders |
| `growth` | Growth / practice campaign (demo); requires explicit opt-in |
| `live` | Full live plan; requires explicit opt-in |

Set explicitly (recommended):

```powershell
C:\Python314\python.exe start.py --profile validation
```

To enable order routing, the profile must set `execution.explicit_opt_in_danger_zone: true`.

Or use the batch launcher, which selects the MT5-capable interpreter automatically:

```bat
START_AGENT.bat
START_AGENT.bat --profile growth
LAUNCH.bat --profile 30-real
```

## Start the bot

**Recommended (single instance):**

```powershell
cd Quant-os-trade
.\scripts\kill_agent.bat
C:\Python314\python.exe scripts\preflight.py
C:\Python314\python.exe start.py --profile validation
```

**Windows shortcut:**

```powershell
.\START_AGENT.bat
```

`START_AGENT.bat` kills old processes, selects a Python interpreter that can import `MetaTrader5`, and forwards arguments to `start.py`.

**One pipeline cycle only (smoke test):**

```powershell
C:\Python314\python.exe start.py --profile 30-real --once
```

## Stop the bot

```powershell
.\scripts\kill_agent.bat
```

Or `Ctrl+C` in the terminal running `start.py`.

Always use `kill_agent.bat` before starting again — duplicate `start.py` processes cause stale state and double orders.

## Dashboard

Default URL: **http://127.0.0.1:8080**

Started automatically with `start.py`. Shows equity, positions, signals, health, and supervisor status.

### Reset session state

Clears kill switch, daily baselines, edge memory, and signal history. **Does not close MT5 positions.**

- Top bar → **Reset state** link, or  
- **Settings** → **Reset session state** button

CLI equivalent:

```powershell
C:\Python314\python.exe scripts\reset_session_memory.py
```

Use this after switching MT5 login/account so drawdown and daily-loss baselines match the new equity.

## Switching MT5 accounts

1. Log into the new account in MT5 (same terminal path the bot uses).
2. Run `.\scripts\kill_agent.bat`
3. Run `C:\Python314\python.exe scripts\reset_session_memory.py` (or dashboard reset).
4. Start again: `.\START_AGENT.bat --profile 30-real`

The risk loop auto-rebaselines on login change, but a manual reset avoids stale kill-switch state.

## Logs and state

| Path | Purpose |
|------|---------|
| `logs/` | Loop logs (`verifier_loop.log`, `execution_loop.log`, …) |
| `state/` | Runtime JSON (positions, signals, risk, account) |
| `state/health.json` | Last health check |
| `state/paper_positions.json` | Synced MT5 positions in live mode |

## Common issues

| Symptom | Fix |
|---------|-----|
| `MetaTrader5 is unavailable in this Python runtime` | Use `.\START_AGENT.bat`, or install with `C:\Python314\python.exe -m pip install -r requirements.txt` |
| Dashboard shows BUY, no trade | Check `state/rejected_signals.json` for `failure_codes`; verify Algo Trading is ON |
| `MT5 Algo Trading is OFF` | Enable Algo Trading in MT5 toolbar |
| Kill switch / daily loss pause | Dashboard **Reset state** or `reset_session_memory.py` after account change |
| 100% exposure with one small position | Restart bot after pull — risk loop uses SL-risk not notional (fixed on `blue-guardian`) |
| Two bots running | `.\scripts\kill_agent.bat` then one `START_AGENT.bat` |
| Gold blocked, other symbols open | Ensure profile has `independent_symbol_exposure: true` (`30-real` does) |

## Push your changes to GitHub

```powershell
git add -A
git commit -m "your message"
git push origin blue-guardian
```

## Pre-flight check

Before starting (or after switching accounts):

```powershell
C:\Python314\python.exe scripts\preflight.py
```

Exits 0 when ready; reports kill switch, health, and `trade_allowed` blockers.

## Pipeline architecture

See **[PIPELINE.md](PIPELINE.md)** for the full loop diagram, entry strategy flow, and hardening roadmap.  
Repository layout: **[STRUCTURE.md](STRUCTURE.md)**.

## Ops alerts (optional)

Enable Discord/Slack-style webhooks in `config.yaml`:

```yaml
ops:
  alerts:
    enabled: true
    webhook_url: "https://discord.com/api/webhooks/..."
```

Alerts fire on kill switch, health degradation, and execution errors. Audit trail: `state/audit_log.jsonl`.

## Quick reference

```powershell
# Full restart (demo micro)
.\scripts\kill_agent.bat
C:\Python314\python.exe scripts\reset_session_memory.py   # optional, after account switch
.\START_AGENT.bat --profile 30-real

# Dashboard
start http://127.0.0.1:8080

# Tests
C:\Python314\python.exe -m pytest tests/test_exposure.py -q
```