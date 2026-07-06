# Phase 2 — SQLite State Store

Back to [README](../README.md) · [Phase 1](PHASE1_UPDATE.md) · [Structure](STRUCTURE.md) · [Pipeline](PIPELINE.md)

Phase 2 adds a SQLite-backed state layer while **keeping JSON mirrors** so the dashboard and rollback path stay intact.

## Architecture

```mermaid
flowchart TD
    A[Loops] --> B[StateStore API]
    B --> C[(SQLite state/quant_os.db)]
    B --> D[JSON mirror layer]
    D --> E[Dashboard]

    A1[signal_loop] --> B
    A2[verifier_loop] --> B
    A3[execution_loop] --> B
```

## Files added

| Path | Role |
|------|------|
| `core/state_schema.py` | Table DDL (signals, approved, rejected, orders, positions, trades, risk/audit, kv) |
| `core/state_store.py` | Read/write API + loop helpers |
| `scripts/migrate_json_state_to_sqlite.py` | Idempotent JSON → SQLite import |
| `tests/test_state_store.py` | Unit tests |

## Configuration

```yaml
state_store:
  enabled: true
  dual_write_json: true
  read_from_sqlite: true
  db_path: quant_os.db
```

Set `enabled: false` to roll back to JSON-only without removing code.

Database path resolves to `state/quant_os.db` (gitignored with `state/`).

## Dual-write flow

1. **signal_loop** — writes `candidate_signals.json` + SQLite `signals`
2. **verifier_loop** — reads candidates from SQLite (fallback JSON); writes approved/rejected to both
3. **execution_loop** — reads approved from SQLite (fallback JSON); syncs orders/positions/trades to both

Dashboard still reads JSON only (Phase 3 can add SQLite readers).

## Migration

```powershell
python scripts/migrate_json_state_to_sqlite.py
python scripts/preflight.py
```

Preflight initializes the DB, checks writability, and seeds from JSON mirrors when present.

## Acceptance checklist

- [x] Bot starts with `state_store.enabled: true`
- [x] signal_loop dual-writes candidates
- [x] verifier_loop reads/writes SQLite + JSON
- [x] execution_loop reads approved + syncs positions/orders/trades
- [x] JSON mirrors still update
- [x] preflight checks DB health
- [x] `pytest tests/test_state_store.py` passes

---

## Known issue — if trade analytics look wrong

**Do not trust SQLite `trades` exit labels until this is fixed.**

`core/exit_manager.py` → `near_take_profit()` uses `tolerance_pct=0.15`, which is **15% of TP price**, not 0.15%. That mislabels many closes as `take_profit` when they were losses or other exit types (especially indices and oil).

Symptoms:

- Closed trades with **negative PnL** and `exit_reason: take_profit`
- Win/loss stats and adaptation inputs skewed

If Phase 2 “goes wrong” on trade forensics or performance metrics, **check this first** before blaming SQLite dual-write. The bug is in exit classification (`trade_tracker.py` / `exit_manager.py`), not in the state store layer.

Planned fix (Phase 2.1 or alongside): symbol-aware tick tolerance (e.g. `0.0015` or points-based), then re-classify or re-import `trades`.