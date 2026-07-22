# Process Flow

End-to-end coordination from market data to learned adaptation.

```mermaid
flowchart TD
    subgraph ingest [Ingest]
        DL[data_loop]
        FL[feature_loop]
        MCL[market_context_loop]
    end

    subgraph decide [Decide]
        RL[risk_loop]
        SL[signal_loop]
        VL[verifier_loop]
    end

    subgraph act [Act]
        EL[execution_loop]
        BGL[blue_guardian_loop]
        PML[position_manager_loop]
    end

    subgraph learn [Learn]
        ML[memory_loop]
        AL[adaptation_loop]
        TLL[trade_log_loop]
    end

    HL[health_loop]

    DL --> FL --> MCL --> RL
    RL --> SL
    SL -->|record_triggers| Arena[(strategy_arena.json)]
    SL -->|write_setup_catalog| Catalog[(setup_catalog.json)]
    SL --> VL --> EL --> BGL --> PML
    PML -->|closes| Trades[(paper_trades.json)]
    Trades --> ML
    ML -->|record_outcomes| Arena
    ML --> Edge[(edge_scores.json)]
    ML --> AL
    AL -->|forward_test| Ledger[(forward_test_ledger.json)]
    AL --> Policy[(symbol_policy_live.json)]
    Ledger --> VL
    Policy --> VL
    ML --> TLL --> Journal[(trade_log.json)]
    EL --> HL
```

## Signal → outcome chain

1. **signal_loop** classifies all 8 setups → `candidate_signals.json` + arena trigger log
2. **verifier_loop** applies culturing vetoes from [[Adaptation]] ledger
3. **execution_loop** places MT5 orders → `paper_orders.json` / `paper_positions.json`
4. **position_manager_loop** manages [[Exits]] → closes update `paper_trades.json`
5. **memory_loop** enriches closes → `edge_scores.json` + `record_outcomes` → arena analytics
6. **adaptation_loop** rebuilds ledger every cycle; calibrates BE/trail on new closes

## Arena analytics path

`record_outcomes` → `strategy_arena.json` analytics → `arena_insights.json` → TUI + `strategy_arena.log`

See [[Strategy Arena]].