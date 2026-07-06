"""SQLite-backed state layer with optional JSON mirror compatibility."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from core.state_schema import DDL, SCHEMA_VERSION
from core.utils import STATE_DIR, read_json_state, utc_now_iso

DEFAULT_DB_PATH = STATE_DIR / "quant_os.db"


def state_store_enabled(config: dict[str, Any]) -> bool:
    return bool((config.get("state_store") or {}).get("enabled"))


def read_from_sqlite(config: dict[str, Any]) -> bool:
    cfg = config.get("state_store") or {}
    if not cfg.get("enabled"):
        return False
    return cfg.get("read_from_sqlite", True)


def get_state_store(config: dict[str, Any] | None = None) -> StateStore | None:
    if config is None:
        from core.utils import load_config
        config = load_config()
    if not state_store_enabled(config):
        return None
    cfg = config.get("state_store") or {}
    db_path = Path(cfg.get("db_path") or DEFAULT_DB_PATH)
    if not db_path.is_absolute():
        db_path = STATE_DIR / db_path
    return StateStore(db_path)


class StateStore:
    """Primary SQLite state with helpers matching hot JSON document shapes."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.executescript(DDL)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                    ("version", str(SCHEMA_VERSION)),
                )
                conn.commit()

    def health_check(self) -> tuple[bool, str]:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.execute("SELECT 1")
                conn.execute(
                    "INSERT OR REPLACE INTO kv_state (key, value_json, updated_at) VALUES (?, ?, ?)",
                    ("_health_probe", "{}", utc_now_iso()),
                )
                conn.commit()
            return True, f"ok ({self.db_path})"
        except (sqlite3.Error, OSError) as exc:
            return False, str(exc)

    def write_signals(self, doc: dict[str, Any]) -> None:
        ts = doc.get("timestamp") or utc_now_iso()
        candidates = list(doc.get("candidates") or [])
        meta = {k: v for k, v in doc.items() if k != "candidates"}
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM signals WHERE status = 'candidate'")
                for row in candidates:
                    sid = str(row.get("signal_id") or uuid.uuid4())
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO signals (
                            signal_id, symbol, side, setup_type, confidence,
                            entry, sl, tp1, tp2, entry_quality, status,
                            created_at, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
                        """,
                        (
                            sid,
                            row.get("symbol", ""),
                            row.get("side", ""),
                            row.get("setup_type"),
                            _float_or_none(row.get("confidence")),
                            _float_or_none(row.get("entry")),
                            _float_or_none(row.get("sl")),
                            _float_or_none(row.get("tp1")),
                            _float_or_none(row.get("tp2")),
                            _float_or_none(row.get("entry_quality")),
                            ts,
                            json.dumps(row, default=str),
                        ),
                    )
                self._set_kv(conn, "candidate_signals_meta", meta, ts)
                conn.commit()

    def read_signals(self) -> dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT raw_json FROM signals WHERE status = 'candidate' ORDER BY created_at DESC"
                ).fetchall()
                meta = self._get_kv(conn, "candidate_signals_meta", default={})
        candidates = [json.loads(r[0]) for r in rows]
        return {**meta, "candidates": candidates, "count": len(candidates)}

    def write_approved(self, doc: dict[str, Any]) -> None:
        ts = doc.get("timestamp") or utc_now_iso()
        approved = list(doc.get("approved") or [])
        meta = {k: v for k, v in doc.items() if k != "approved"}
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM approved_signals")
                for row in approved:
                    sid = str(row.get("signal_id") or uuid.uuid4())
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO approved_signals (signal_id, approved_at, verifier_result_json)
                        VALUES (?, ?, ?)
                        """,
                        (sid, ts, json.dumps(row, default=str)),
                    )
                self._set_kv(conn, "approved_signals_meta", meta, ts)
                conn.commit()

    def read_approved(self) -> dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT verifier_result_json FROM approved_signals ORDER BY approved_at DESC"
                ).fetchall()
                meta = self._get_kv(conn, "approved_signals_meta", default={})
        approved = [json.loads(r[0]) for r in rows]
        return {**meta, "approved": approved, "count": len(approved)}

    def write_rejected(self, doc: dict[str, Any]) -> None:
        ts = doc.get("timestamp") or utc_now_iso()
        rejected = list(doc.get("rejected") or [])
        meta = {k: v for k, v in doc.items() if k != "rejected"}
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM rejected_signals")
                for row in rejected:
                    sid = str(row.get("signal_id") or uuid.uuid4())
                    reason = row.get("reason") or row.get("reject_reason")
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO rejected_signals
                            (signal_id, rejected_at, reason, verifier_result_json)
                        VALUES (?, ?, ?, ?)
                        """,
                        (sid, ts, reason, json.dumps(row, default=str)),
                    )
                self._set_kv(conn, "rejected_signals_meta", meta, ts)
                conn.commit()

    def read_rejected(self) -> dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT verifier_result_json FROM rejected_signals ORDER BY rejected_at DESC"
                ).fetchall()
                meta = self._get_kv(conn, "rejected_signals_meta", default={})
        rejected = [json.loads(r[0]) for r in rows]
        return {**meta, "rejected": rejected, "count": len(rejected)}

    def write_evaluated(self, doc: dict[str, Any]) -> None:
        ts = doc.get("timestamp") or utc_now_iso()
        evaluated = list(doc.get("evaluated") or [])
        meta = {k: v for k, v in doc.items() if k not in ("evaluated", "skipped")}
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM evaluated_signals")
                for row in evaluated:
                    sid = str(row.get("signal_id") or uuid.uuid4())
                    ev = row.get("evaluation") or {}
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO evaluated_signals
                            (signal_id, symbol, action, policy_score, created_at, raw_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sid,
                            row.get("symbol", ""),
                            ev.get("action") or row.get("execution_policy", {}).get("action"),
                            _float_or_none(ev.get("policy_score")),
                            ts,
                            json.dumps(row, default=str),
                        ),
                    )
                self._set_kv(conn, "evaluated_signals_meta", meta, ts)
                conn.commit()

    def read_evaluated(self) -> dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT raw_json FROM evaluated_signals ORDER BY created_at DESC"
                ).fetchall()
                meta = self._get_kv(conn, "evaluated_signals_meta", default={})
        evaluated = [json.loads(r[0]) for r in rows]
        return {**meta, "evaluated": evaluated, "count": len(evaluated)}

    def write_best_policies(self, doc: dict[str, Any]) -> None:
        ts = doc.get("timestamp") or utc_now_iso()
        with self._lock:
            with self._connect() as conn:
                self._set_kv(conn, "best_policies", doc, ts)
                conn.commit()

    def read_best_policies(self) -> dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                return self._get_kv(conn, "best_policies", default={})

    def write_policy_scores(self, doc: dict[str, Any]) -> None:
        ts = doc.get("timestamp") or utc_now_iso()
        variants = list(doc.get("variants") or [])
        meta = {k: v for k, v in doc.items() if k != "variants"}
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM policy_scores")
                for row in variants:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO policy_scores (
                            policy_id, symbol, setup_type, session, entry_type,
                            limit_offset_atr, sl_atr_mult, tp1_r, be_trigger_r,
                            trail_start_r, score, sample_n, created_at, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row.get("policy_id") or uuid.uuid4()),
                            row.get("symbol", ""),
                            row.get("setup_type"),
                            row.get("session"),
                            row.get("entry_type"),
                            _float_or_none(row.get("limit_offset_atr")),
                            _float_or_none(row.get("sl_atr_mult")),
                            _float_or_none(row.get("tp1_r")),
                            _float_or_none(row.get("be_trigger_r")),
                            _float_or_none(row.get("trail_start_r")),
                            _float_or_none(row.get("score")),
                            int(row.get("sample_n") or 0),
                            ts,
                            json.dumps(row, default=str),
                        ),
                    )
                self._set_kv(conn, "policy_scores_meta", meta, ts)
                conn.commit()

    def read_policy_scores(self) -> dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT raw_json FROM policy_scores
                    ORDER BY score DESC, sample_n DESC
                    """
                ).fetchall()
                meta = self._get_kv(conn, "policy_scores_meta", default={})
        variants = [json.loads(r[0]) for r in rows]
        return {**meta, "variants": variants, "variant_count": len(variants)}

    def write_positions(self, doc: dict[str, Any]) -> None:
        ts = doc.get("timestamp") or utc_now_iso()
        positions = list(doc.get("positions") or [])
        meta = {k: v for k, v in doc.items() if k != "positions"}
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM positions")
                for row in positions:
                    pid = str(row.get("position_id") or row.get("ticket") or uuid.uuid4())
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO positions (
                            position_id, symbol, side, size, entry, sl, tp,
                            mt5_ticket, status, opened_at, updated_at, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            pid,
                            row.get("symbol", ""),
                            row.get("side", ""),
                            _float_or_none(row.get("size") or row.get("volume")),
                            _float_or_none(row.get("entry") or row.get("fill_price")),
                            _float_or_none(row.get("sl")),
                            _float_or_none(row.get("tp") or row.get("tp1")),
                            str(row.get("ticket") or row.get("mt5_ticket") or pid),
                            row.get("status", "open"),
                            row.get("opened_at") or row.get("created_at") or ts,
                            ts,
                            json.dumps(row, default=str),
                        ),
                    )
                self._set_kv(conn, "paper_positions_meta", meta, ts)
                conn.commit()

    def read_positions(self) -> dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT raw_json FROM positions ORDER BY updated_at DESC"
                ).fetchall()
                meta = self._get_kv(conn, "paper_positions_meta", default={})
        positions = [json.loads(r[0]) for r in rows]
        return {**meta, "positions": positions}

    def write_orders(self, doc: dict[str, Any]) -> None:
        ts = doc.get("timestamp") or utc_now_iso()
        orders = list(doc.get("orders") or [])
        meta = {k: v for k, v in doc.items() if k != "orders"}
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM orders")
                for row in orders:
                    oid = str(row.get("order_id") or uuid.uuid4())
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO orders (
                            order_id, signal_id, symbol, side, order_type,
                            entry, sl, tp, status, mt5_ticket, created_at, updated_at, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            oid,
                            row.get("signal_id"),
                            row.get("symbol", ""),
                            row.get("side", ""),
                            row.get("order_type") or row.get("type"),
                            _float_or_none(row.get("entry") or row.get("fill_price")),
                            _float_or_none(row.get("sl")),
                            _float_or_none(row.get("tp") or row.get("tp1")),
                            row.get("status"),
                            str(row.get("ticket") or row.get("mt5_ticket") or ""),
                            row.get("created_at") or ts,
                            ts,
                            json.dumps(row, default=str),
                        ),
                    )
                self._set_kv(conn, "paper_orders_meta", meta, ts)
                conn.commit()

    def write_trades(self, doc: dict[str, Any]) -> None:
        ts = doc.get("timestamp") or utc_now_iso()
        trades = list(doc.get("trades") or [])
        with self._lock:
            with self._connect() as conn:
                for row in trades:
                    tid = str(row.get("trade_id") or row.get("mt5_deal") or uuid.uuid4())
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO trades (
                            trade_id, symbol, side, entry, exit, pnl,
                            setup_type, opened_at, closed_at, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            tid,
                            row.get("symbol", ""),
                            row.get("side", ""),
                            _float_or_none(row.get("entry")),
                            _float_or_none(row.get("exit")),
                            _float_or_none(row.get("pnl")),
                            row.get("setup_type"),
                            row.get("opened_at"),
                            row.get("closed_at") or ts,
                            json.dumps(row, default=str),
                        ),
                    )
                conn.commit()

    def append_audit(self, event: str, *, symbol: str | None = None, details: dict | None = None) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO audit_events (event, symbol, details_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (event, symbol, json.dumps(details or {}, default=str), utc_now_iso()),
                )
                conn.commit()

    def set_kv(self, key: str, value: Any) -> None:
        with self._lock:
            with self._connect() as conn:
                self._set_kv(conn, key, value, utc_now_iso())
                conn.commit()

    def get_kv(self, key: str, default: Any = None) -> Any:
        with self._lock:
            with self._connect() as conn:
                return self._get_kv(conn, key, default=default)

    def seed_from_json(self) -> dict[str, int]:
        """Idempotent import from existing JSON mirrors."""
        counts = {
            "signals": 0,
            "approved": 0,
            "rejected": 0,
            "positions": 0,
            "orders": 0,
            "trades": 0,
            "audit": 0,
        }
        cand = read_json_state("candidate_signals.json", default={})
        if cand.get("candidates"):
            self.write_signals(cand)
            counts["signals"] = len(cand["candidates"])

        approved = read_json_state("approved_signals.json", default={})
        if approved.get("approved") is not None:
            self.write_approved(approved)
            counts["approved"] = len(approved.get("approved") or [])

        rejected = read_json_state("rejected_signals.json", default={})
        if rejected.get("rejected") is not None:
            self.write_rejected(rejected)
            counts["rejected"] = len(rejected.get("rejected") or [])

        positions = read_json_state("paper_positions.json", default={})
        if positions.get("positions") is not None:
            self.write_positions(positions)
            counts["positions"] = len(positions.get("positions") or [])

        orders = read_json_state("paper_orders.json", default={})
        if orders.get("orders") is not None:
            self.write_orders(orders)
            counts["orders"] = len(orders.get("orders") or [])

        trades = read_json_state("paper_trades.json", default={})
        if trades.get("trades"):
            self.write_trades(trades)
            counts["trades"] = len(trades["trades"])

        audit_path = STATE_DIR / "audit_log.jsonl"
        if audit_path.exists():
            try:
                lines = audit_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines[-500:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.append_audit(
                    row.get("event", "unknown"),
                    symbol=row.get("symbol"),
                    details=row.get("details"),
                )
                counts["audit"] += 1

        kill = read_json_state("kill_switch.json", default={})
        if kill:
            self.set_kv("kill_switch", kill)
        health = read_json_state("health.json", default={})
        if health:
            self.set_kv("health", health)

        return counts

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _set_kv(conn: sqlite3.Connection, key: str, value: Any, updated_at: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO kv_state (key, value_json, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value, default=str), updated_at),
        )

    @staticmethod
    def _get_kv(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
        row = conn.execute("SELECT value_json FROM kv_state WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default


def _has_candidates(doc: dict[str, Any] | None) -> bool:
    return bool(doc and doc.get("candidates") is not None)


def read_candidate_signals(config: dict[str, Any]) -> dict[str, Any] | None:
    if read_from_sqlite(config):
        store = get_state_store(config)
        if store:
            doc = store.read_signals()
            if _has_candidates(doc):
                return doc
    return read_json_state("candidate_signals.json")


def read_approved_signals(config: dict[str, Any]) -> dict[str, Any] | None:
    if read_from_sqlite(config):
        store = get_state_store(config)
        if store:
            doc = store.read_approved()
            if doc.get("approved") is not None:
                return doc
    return read_json_state("approved_signals.json")


def read_position_state(config: dict[str, Any]) -> dict[str, Any]:
    if read_from_sqlite(config):
        store = get_state_store(config)
        if store:
            doc = store.read_positions()
            if doc.get("positions") is not None:
                return doc
    return read_json_state("paper_positions.json", default={"positions": []})


def dual_write_enabled(config: dict[str, Any]) -> bool:
    cfg = config.get("state_store") or {}
    return state_store_enabled(config) and cfg.get("dual_write_json", True)


def sync_store_from_doc(config: dict[str, Any], kind: str, doc: dict[str, Any]) -> None:
    """Write hot state document to SQLite when state_store is enabled."""
    store = get_state_store(config)
    if store is None:
        return
    if kind == "signals":
        store.write_signals(doc)
    elif kind == "approved":
        store.write_approved(doc)
    elif kind == "rejected":
        store.write_rejected(doc)
    elif kind == "positions":
        store.write_positions(doc)
    elif kind == "orders":
        store.write_orders(doc)
    elif kind == "trades":
        store.write_trades(doc)
    elif kind == "evaluated":
        store.write_evaluated(doc)
    elif kind == "best_policies":
        store.write_best_policies(doc)
    elif kind == "policy_scores":
        store.write_policy_scores(doc)


def read_evaluated_signals(config: dict[str, Any]) -> dict[str, Any] | None:
    if read_from_sqlite(config):
        store = get_state_store(config)
        if store:
            doc = store.read_evaluated()
            if doc.get("evaluated") is not None:
                return doc
    return read_json_state("evaluated_signals.json")


def evaluated_available(config: dict[str, Any]) -> bool:
    if read_from_sqlite(config):
        store = get_state_store(config)
        if store and store.read_evaluated().get("evaluated") is not None:
            return True
    return (STATE_DIR / "evaluated_signals.json").exists()


def read_best_policies(config: dict[str, Any]) -> dict[str, Any]:
    if read_from_sqlite(config):
        store = get_state_store(config)
        if store:
            doc = store.read_best_policies()
            if doc.get("policies") is not None:
                return doc
    return read_json_state("best_policies.json", default={})


def best_policies_available(config: dict[str, Any]) -> bool:
    if read_from_sqlite(config):
        store = get_state_store(config)
        if store and store.read_best_policies().get("policies") is not None:
            return True
    return (STATE_DIR / "best_policies.json").exists()


def read_policy_scores(config: dict[str, Any]) -> dict[str, Any] | None:
    if read_from_sqlite(config):
        store = get_state_store(config)
        if store:
            doc = store.read_policy_scores()
            if doc.get("variants") is not None:
                return doc
    return read_json_state("policy_scores.json")


def policy_scores_available(config: dict[str, Any]) -> bool:
    if read_from_sqlite(config):
        store = get_state_store(config)
        if store and store.read_policy_scores().get("variants") is not None:
            return True
    return (STATE_DIR / "policy_scores.json").exists()


def read_verifier_candidates(config: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Prefer evaluated signals when evaluation layer is enabled."""
    from core.evaluation_policy import evaluation_enabled

    if evaluation_enabled(config):
        ev_doc = read_evaluated_signals(config) or {}
        if ev_doc.get("evaluated") is not None:
            return ev_doc, list(ev_doc.get("evaluated") or [])
    cand_doc = read_candidate_signals(config) or {}
    return cand_doc, list(cand_doc.get("candidates") or [])


def candidates_available(config: dict[str, Any]) -> bool:
    if read_from_sqlite(config):
        store = get_state_store(config)
        if store and store.read_signals().get("candidates") is not None:
            return True
    return (STATE_DIR / "candidate_signals.json").exists()


def approved_available(config: dict[str, Any]) -> bool:
    if read_from_sqlite(config):
        store = get_state_store(config)
        if store and store.read_approved().get("approved") is not None:
            return True
    return (STATE_DIR / "approved_signals.json").exists()


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None