# MT5 Quant OS — Pipeline Architecture

Back to [README](../README.md) · [Phase 1 update](PHASE1_UPDATE.md) · [Structure](STRUCTURE.md) · [How to run](HOW_TO_RUN.md)

## Trading cycle (every ~45s)

Each supervisor tick runs **one sequential pipeline**. A failure in one loop is isolated — later loops still run.

```mermaid
flowchart TB
    subgraph ingest["1 — Market data"]
        DL[data_loop<br/>MT5 candles M5/M15]
        FL[feature_loop<br/>indicators + parquet]
        MCL[market_context_loop<br/>regime / session / bias]
    end

    subgraph risk["2 — Risk gate"]
        RL[risk_loop<br/>exposure · drawdown · kill switch]
    end

    subgraph decide["3 — Strategy & entry"]
    SL[signal_loop<br/>DecisionEngine + EntryPipeline]
    EV[evaluation_loop<br/>corrective policy · shadow]
    VL[verifier_loop<br/>gates · confirm · exposure]
    end

    subgraph execute["4 — Execution & management"]
        EL[execution_loop<br/>MT5 orders]
        BGL[blue_guardian_loop<br/>prop-firm rules]
        PML[position_manager_loop<br/>BE · trail · partials]
    end

    subgraph learn["5 — Memory & adaptation"]
        ML[memory_loop<br/>edge · adjustments]
        AL[adaptation_loop<br/>culturing · BE/trail cal]
        TLL[trade_log_loop<br/>per-trade journal]
    end

    subgraph ops["6 — Operations"]
        HL[health_loop<br/>MT5 · candles · disk]
    end

    DL --> FL --> MCL --> RL
    RL --> SL --> EV --> VL --> EL
    EL --> BGL --> PML --> ML --> AL --> TLL --> HL

    SL -.->|candidate_signals| EV
    EV -.->|evaluated_signals| VL
    VL -.->|SQLite + approved_signals.json| EL
    RL -.->|kill_switch.json| VL
    RL -.->|kill_switch.json| EL
```

## Background services (supervisor)

These run on their own intervals, **outside** the main trading pipeline:

```mermaid
flowchart LR
    SUP[Supervisor] --> PIPE[trading_pipeline<br/>every 45s]
    SUP --> HIST[history_engine<br/>every 5m]
    SUP --> RES[research_engine<br/>every 30m]
    PIPE --> DASH[Dashboard :8080<br/>reads state/]
```

## Entry strategy flow (signal → fill)

Improved entry path inside **signal_loop** and **verifier_loop**:

```mermaid
flowchart TD
    DE[DecisionEngine<br/>setup classify · confidence tree]
    PIN[pin_strategy_entry<br/>anchor · SL/TP · within_reach]
    EP[EntryPipeline<br/>refresh levels · entry_quality · capacity filter]
    CS[candidate_signals.json]

    DE --> PIN --> EP --> CS

    CS --> VF[Verifier gates]
    VF --> EC[entry_confirm staging<br/>30s hold + drift reset]
    VF --> DE2[dynamic_entry<br/>pyramid spacing]
    VF --> EX[exposure + culturing]

    EC --> AP[approved_signals.json]
    DE2 --> AP
    EX --> AP
    AP --> MT5[execution_loop → MT5]
```

### Entry pipeline rules (`core/entry_pipeline.py`)

| Step | What it does |
|------|----------------|
| Capacity filter | Skip symbols already at `max_open_per_symbol` |
| Refresh levels | Re-pin entry/SL/TP to latest M5 bar each cycle |
| `entry_quality` | 0–100 score (anchor distance, reach, structure) |
| Unreachable drop | Skip limit entries where price is > `max_entry_wait_atr` from anchor |

Configure in `trading.strategy_entries` (enabled on `30-real` profile).

## State files (hot path)

**Phase 2:** Loops dual-write `state/quant_os.db` (SQLite) and JSON mirrors. See [PHASE2_UPDATE.md](PHASE2_UPDATE.md).

| File / store | Writer | Reader |
|--------------|--------|--------|
| `quant_os.db` → `signals` | signal_loop | verifier_loop |
| `quant_os.db` → `approved_signals` | verifier_loop | execution_loop |
| `features.json` | feature_loop | signal_loop |
| `candidate_signals.json` (mirror) | signal_loop | dashboard |
| `approved_signals.json` (mirror) | verifier_loop | dashboard |
| `paper_positions.json` | execution / position sync | risk, verifier, entry pipeline |
| `kill_switch.json` | risk_loop | verifier, execution |
| `audit_log.jsonl` | verifier, execution, risk | ops / forensics |

## Roadmap layers (hardening)

```mermaid
flowchart TB
    P1[Phase 1 — Ops<br/>audit · alerts · preflight · PIPELINE.md]
    P2[Phase 2 — State store<br/>SQLite positions/orders]
    P3[Phase 3 — Desk discipline<br/>per-account namespace · reconciliation]
    P4[Phase 4 — Infra<br/>VPS per account · metrics · CI]

    P1 --> P2 --> P3 --> P4
```

Phase 1: `audit_log.jsonl`, `ops.alerts` webhooks, `scripts/preflight.py`, entry pipeline refinement.

Phase 2 (current): `core/state_store.py`, dual-write SQLite + JSON mirrors, `scripts/migrate_json_state_to_sqlite.py`.