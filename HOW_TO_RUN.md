# MT5 Quant OS — How to Run

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

Install dependencies from the project root:

```powershell
cd mt5_quant_agent
pip install -r requirements.txt
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
| `30-real` | **Micro live/demo** — XAU, Oil, UK100; independent per-symbol exposure; ~$30 equity |
| `30` | Micro paper-style gates |
| `growth` | Growth / practice campaign |
| `live` | Full live plan ($50k gates) |

Set explicitly (recommended):

```powershell
python start.py --profile 30-real
```

Or use auto-selection based on logged-in account equity (no `--profile`).

## Start the bot

**Recommended (single instance):**

```powershell
cd mt5_quant_agent
.\scripts\kill_agent.bat
python start.py --profile 30-real
```

**Windows shortcut:**

```powershell
.\START_AGENT.bat
```

`START_AGENT.bat` kills old processes first, then runs `python start.py` (auto profile).

**One pipeline cycle only (smoke test):**

```powershell
python start.py --profile 30-real --once
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
python scripts\reset_session_memory.py
```

Use this after switching MT5 login/account so drawdown and daily-loss baselines match the new equity.

## Switching MT5 accounts

1. Log into the new account in MT5 (same terminal path the bot uses).
2. Run `.\scripts\kill_agent.bat`
3. Run `python scripts\reset_session_memory.py` (or dashboard reset).
4. Start again: `python start.py --profile 30-real`

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
| Dashboard shows BUY, no trade | Check `state/rejected_signals.json` for `failure_codes`; verify Algo Trading is ON |
| `MT5 Algo Trading is OFF` | Enable Algo Trading in MT5 toolbar |
| Kill switch / daily loss pause | Dashboard **Reset state** or `reset_session_memory.py` after account change |
| 100% exposure with one small position | Restart bot after pull — risk loop uses SL-risk not notional (fixed on `blue-guardian`) |
| Two bots running | `.\scripts\kill_agent.bat` then one `start.py` |
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
python scripts\preflight.py
```

Exits 0 when ready; reports kill switch, health, and `trade_allowed` blockers.

## Pipeline architecture

See **`PIPELINE.md`** for the full loop diagram, entry strategy flow, and hardening roadmap.

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
python scripts\reset_session_memory.py   # optional, after account switch
python start.py --profile 30-real

# Dashboard
start http://127.0.0.1:8080

# Tests
python -m pytest tests/test_exposure.py -q
```