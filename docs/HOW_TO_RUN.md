# MT5 Quant OS — How to Run (Phase 0)

Back to [README](../README.md).

Repo: https://github.com/banky420star/Quant-os-trade.git
Phase 0 working branch: `agent/p0-safety-closure`

> **Phase 0 posture.** The only sanctioned run state is the **validation
> profile with zero execution authority**. Validation attaches to your MT5
> terminal for market data, features, the M1 structure shadow engine, and the
> dashboard — it sends **no orders**. Live/demo order routing, live fast
> scalping, and real-account profiles are **NOT part of Phase 0**; they are
> reviewed-deployment-only and are clearly marked in the launchers.

## Prerequisites

| Requirement | Notes |
|-------------|--------|
| **Windows** | MT5 Python API is Windows-only |
| **Python 3.10+** | Tested on 3.12/3.14; use a venv if you prefer |
| **MetaTrader 5** | Logged into the account you want to observe |
| **Algo Trading** | **NOT required for validation.** Only execution profiles need MT5 Algo Trading ON. Keep it OFF for Phase 0 validation/smoke tests. |
| **Same session** | Bot attaches to the MT5 terminal in your Windows session (use `MT5Agent` terminal if running headless/RDP) |

Install dependencies from the project root. The Windows launcher selects a
Python interpreter that can import `MetaTrader5` before starting the bot:

```powershell
cd Quant-os-trade
python -m pip install -r requirements.txt
```

## Safe Windows startup (validation)

```powershell
cd "C:\Users\Administrator\Desktop\new task"
.\scripts\kill_agent.bat
python scripts\preflight.py
START_AGENT.bat
```

`START_AGENT.bat` defaults to `--profile validation` (read-only, zero orders).
To be explicit:

```powershell
python start.py --profile validation
```

The dashboard is at **http://127.0.0.1:8080**.

### How to verify zero execution authority

```powershell
python -c "import core.mt5_owner as o; print('gate blocked =', o.execution_gate_blocked())"
```

Expected: `gate blocked = True`. While blocked, every `order_send` path returns
`None` — no broker mutation is possible regardless of dashboard clicks, state
files, or presets. The safety matrix on the dashboard should read
**DISARMED** / `execution_allowed: false`.

## Safe Windows shutdown

```powershell
.\scripts\kill_agent.bat
```

Always use `kill_agent.bat` before starting again — duplicate `start.py`
processes cause stale state. Never leave two agents running.

## Temporary M1 shadow smoke (optional)

M1 structure is **disabled by default** and stays disabled unless you are
running a deliberate shadow smoke. To run one:

1. Stop the agent.
2. In `config.yaml`, change only:
   ```yaml
   m1_structure:
     enabled: true
   ```
   Keep `max_data_age_seconds: 90` and `loop_interval_seconds: 10` unchanged.
3. Start with `START_AGENT.bat` (validation profile) with **Algo Trading OFF**.
4. Verify the M1 card: `state/m1_structure_decisions.json` updates about every
   10 seconds; fresh data → `WAIT`/`BUY`/`SELL`; the current M1 candle is
   `FORMING`/provisional and only closes after a newer bar arrives.
5. Watch `state/market_data_feed.json`: `worker_alive: true`,
   `last_refresh_success_at` ticking, `last_market_timestamp` advancing while
   the market is open.

## Restore M1 to disabled

```powershell
.\scripts\kill_agent.bat
```

Then in `config.yaml`:

```yaml
m1_structure:
  enabled: false
```

Verify the config is untouched:

```powershell
git diff -- config.yaml
```

No output = clean. Do **not** commit the temporary M1 enable.

## Kill-switch behavior (STOP)

The dashboard **STOP TRADING** button is instant and **one-way**:

* It writes `state/kill_switch.json` with `source: operator`.
* It cannot be reversed from the kill-switch endpoint — a direct "off" is
  rejected with a message pointing at the verified RESUME flow.
* The risk manager never clears an operator stop.

## RESUME behavior

RESUME is a separate, **verified** path (`/api/resume`):

* The confirmation phrase is derived **server-side** (`RESUME DEMO <login>`).
* The dashboard never accepts a client-supplied expected value.
* Demo resume requires an explicit execution opt-in, healthy status, a fresh
  heartbeat, and a connected account — plus the exact typed confirmation.
* Real-account resume is **disabled during Phase 0**.

## Reset behavior

The **Reset state** action is fail-safe in Phase 0:

* Refuses with a `409` while any MT5 or paper position is open.
* Calls `reset_session_memory(preserve_safety_gates=True)` — it preserves the
  operator kill switch, risk gates, and `position_management.json`.
* The only way to clear safety gates is the explicit out-of-band CLI flag:

```powershell
python scripts\reset_session_memory.py --full
```

Do not use reset as a way to re-enable trading after STOP — use verified
RESUME instead.

## Stale-data behavior

Market-data freshness comes from the **real market bar timestamp**, never from
"the worker ran" or "a file was rewritten":

* Fresh (last M1 bar ≤ 90 s old) → normal `WAIT` / `BUY` / `SELL` evaluation.
* Stale (> 90 s) or unknown timestamp → **mandatory `WAIT`**, `data_fresh:
  false`, countdown zeroed, trigger set to `Stale M1 data`.

A connected terminal with stale prices is not valid trading input.

## What "WAIT" means

`WAIT` is a decision state, not a failure. It means the structure engine has
no confirmed entry chain: only forming structures, conflicting evidence,
insufficient confirmation, stale data, or the engine being disabled. No signal
is emitted from a `WAIT` state.

## What Phase 0 does NOT approve

Phase 0 does **not** approve: live order routing, fast-mode live execution,
real-account profiles, automatic M1 execution, `30-real` as a normal path,
clearing the kill switch via reset, or any dashboard execution-authority
shortcut. Those are later phases behind a reviewed deployment.

## Logs and state

| Path | Purpose |
|------|---------|
| `logs/` | Loop logs (`system.log`, `data_loop.log`, `m1_structure_loop.log` via `system.log`) |
| `state/latest_candles.json` | Current M5/M15 (+ M1 when enabled) candle snapshot |
| `state/market_data_feed.json` | Feed health: worker alive, refresh attempts/successes, consecutive failures, last market timestamp |
| `state/m1_structure_decisions.json` | Per-symbol `WAIT`/`BUY`/`SELL` decisions (shadow) |
| `state/m1_structure_events.jsonl` | Append-only structure event ledger |
| `state/health.json` | Health check output |

## Common issues

| Symptom | Fix |
|---------|-----|
| `MetaTrader5 is unavailable in this Python runtime` | Use `.\START_AGENT.bat`, or install into that interpreter with `python -m pip install -r requirements.txt` |
| Dashboard shows M1 `WAIT` with `data_fresh: false` | Feed is stale — check `market_data_feed.json`; MT5 terminal may be disconnected or the market closed |
| Kill switch ON from operator STOP | Use the verified RESUME flow (typed confirmation), never reset |
| Reset refuses with 409 | Close/manage open positions first |
| Two bots running | `.\scripts\kill_agent.bat` then one `START_AGENT.bat` |

## Pre-flight check

```powershell
python scripts\preflight.py
```

Exits 0 when ready; reports kill switch, health, and `trade_allowed` blockers.
Without `--full`, `reset_session_memory` preserves the kill switch, risk gates,
and position management by default.
