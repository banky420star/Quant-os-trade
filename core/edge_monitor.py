"""Live edge monitor — per-symbol halt-on-divergence state machine.

Compares a symbol's realised-daily PnL over the last
``HALT_DECAY_WINDOW_DAYS`` (14) days against the latest
``verify_edge`` projection. When **BOTH** 7-day halves independently
diverge > ``HALT_SIGMA_THRESHOLD`` (2.0) sample standard deviations
from the projection, the symbol is auto-HALTED. Recovery requires
``HALT_RECOVERY_REQUIRED_DAYS`` (5) consecutive in-band observations.

ENTRY (HALT) conditions — symbol enters STATUS_HALT when ALL of these hold:
    E1. projection_daily_usd > 0        # must have a positive edge to monitor
    E2. realised window >= 14 days       # enough data to be meaningful
    E3. older 7-day half mean |μ_a - projection| > 2.0 * std_a
                                        # (or, if std near zero, |μ_a - proj| > 0.5)
    E4. newer 7-day half mean |μ_b - projection| > 2.0 * std_b
                                        # (similarly for std near zero)
    E5. symbol not already HALT          # no double-counting; transitions are atomic

EXIT (recovery) conditions — symbol returns to STATUS_OK when ALL of:
    X1. previous status = STATUS_HALT   # entry was an actual halt, not a 'watch'
    X2. an "in-band" observation = BOTH halves mean within 1σ of projection
    X3. >=5 consecutive in-band observations
  The COOLING_OFF intermediate phase is NOT used in the live recovery
  path; the symbol transits HALT->OK directly on the 5th in-band tick.
  STATUS_COOLING_OFF remains reserved in the enum for backward-compat
  with downstream readers of the state document.

WATCH (advisory) status — symbol enters STATUS_WATCH when:
    W1. E1 + E2 satisfied (enough positive-edge data)
    W2. exactly ONE of E3/E4 holds       # single half diverged
    W3. status was OK at prior observation
  (WATCH is purely advisory; no entry block; no audit-row special treatment.)

What this module DOES NOT do (observe-only invariant — mirrors Phase 2.5 + canary):
    * Does NOT mutate ``config.yaml``,
      ``learning_config_overrides.json``, ``config_proposals.jsonl``,
      or ``learning_modifiers.json``.
    * Does NOT modify running positions.
    * Does NOT trade live or paper.

Operator-facing surfaces:
    * ``state/edge_monitor_state.json`` — per-symbol status record.
    * ``state/edge_monitor_audit.jsonl``  — one row per monitored symbol per tick.
    * ``is_halted(symbol, state_path=...) -> bool`` — query hook the
      order-entry layer (e.g. ``core/position_sizing.py``) reads to
      block NEW entries on a halted symbol without touching config.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.utils import STATE_DIR, utc_now_iso, write_json_state

EDGE_MONITOR_STATE_FILE = "edge_monitor_state.json"
EDGE_MONITOR_AUDIT_FILE = "edge_monitor_audit.jsonl"

# Hard-block contract (mirrors Phase 2.5 `_BLOCKED_TARGETS`):
# edge_monitor never writes to any of these.
EDGE_BLOCKED_TARGETS = (
    "config.yaml",
    "learning_config_overrides.json",
    "config_proposals.jsonl",
    "learning_modifiers.json",
)

# Auto-halt parameters
HALT_SIGMA_THRESHOLD = 2.0           # two consecutive halves each diverging > 2σ
HALT_DECAY_WINDOW_DAYS = 14          # half-half split window length
HALT_HALF_WINDOW_DAYS = 7            # each half
HALT_RECOVERY_REQUIRED_DAYS = 5      # consecutive in-band observations to exit HALT
HALT_MIN_SAMPLE_STD = 1e-6           # floor for std to avoid divide-by-zero
HALT_DETERMINISTIC_FLOOR_USD = 0.50  # zero-variance half must diverge by >= this USD
HALT_MIN_NONZERO_DAYS = 7            # window must have >= this many non-zero close-days
                                        # (gate behind which WATCH + HALT both fire —
                                        # advisory noise floor)

# Status enum (string-valued for JSON friendliness)
STATUS_OK = "ok"
STATUS_WATCH = "watch"
STATUS_HALT = "halt"
STATUS_COOLING_OFF = "cooling_off"


# ----------------------------------------------------------------------
# Dataclass — one row per symbol
# ----------------------------------------------------------------------
@dataclass
class SymbolEdgeState:
    symbol: str
    status: str = STATUS_OK
    projection_daily_usd: float = 0.0
    projection_cached_at: str | None = None
    last_check_iso: str | None = None
    consecutive_in_band: int = 0
    last_in_band_day: str | None = None  # YYYY-MM-DD of last in-band ++
    consecutive_observed: int = 0
    halt_entered_at: str | None = None
    halt_entered_count: int = 0  # total HALT entries (audit / dashboard)
    cooling_off_started_at: str | None = None
    watch_entered_at: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    last_audit_decision: str | None = None
    last_audit_iso: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SymbolEdgeState":
        if not isinstance(d, dict):
            return cls(symbol="UNKNOWN")
        allowed = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in d.items() if k in allowed}
        # symbol is required — fall back if missing
        kwargs.setdefault("symbol", "UNKNOWN")
        return cls(**kwargs)


# ----------------------------------------------------------------------
# Helpers (re-use canary helpers for ISO parsing & bucket compatibility)
# ----------------------------------------------------------------------
def _parse_iso_date(ca: Any) -> str | None:
    if not ca:
        return None
    s = str(ca).strip()
    if len(s) < 10:
        return None
    date_part = s[:10]
    try:
        datetime.strptime(date_part, "%Y-%m-%d")
    except ValueError:
        return None
    return date_part


def _bucket_realized_by_day(trades: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in trades or []:
        ca = t.get("closed_at") or t.get("exit_time") or ""
        sym = t.get("symbol")
        date = _parse_iso_date(ca)
        if date is None or sym is None:
            continue
        try:
            pnl = float(t.get("pnl") or t.get("profit") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        out.setdefault(date, 0.0)
        out[date] += pnl
    return out


def _realized_daily_series(
    trades: list[dict[str, Any]],
    symbol: str,
    days: int,
    *,
    today_anchor: str | None = None,
) -> list[float]:
    """Return a chronologically-ascending list of realised PnL per day for ``symbol``
    over the last ``days`` days (zero for days with no closes).

    An optional ``today_anchor`` (YYYY-MM-DD) overrides the internal
    ``datetime.now(timezone.utc).date()`` so the producer of the bucket
    and the consumer of the series share the SAME reference date.
    Avoids the rare UTC-midnight-shift oscillation where trades snap to
    one date and the series-rebuild consumes another.
    """
    if days <= 0:
        return []
    sym_trades = [t for t in (trades or []) if t.get("symbol") == symbol]
    daily = _bucket_realized_by_day(sym_trades)
    if today_anchor:
        try:
            today = datetime.strptime(today_anchor, "%Y-%m-%d").date()
        except ValueError:
            today = datetime.now(timezone.utc).date()
    else:
        today = datetime.now(timezone.utc).date()
    series: list[float] = []
    for i in range(days - 1, -1, -1):
        d = today.toordinal() - i
        date_str = datetime.fromordinal(d).date().isoformat()
        series.append(daily.get(date_str, 0.0))
    return series


def _mean(xs: list[float]) -> float:
    return float(sum(xs) / len(xs)) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return float(statistics.pstdev([x - m for x in xs]))


def _today_iso_date(now_iso: str | None = None) -> str:
    if now_iso:
        return now_iso[:10]
    return datetime.now(timezone.utc).date().isoformat()


def _window_has_real_data(window: list[float]) -> bool:
    return any(abs(x) > 1e-9 for x in window)


def _half_divergence(
    half: list[float],
    projection: float,
    *,
    sigma_threshold: float = HALT_SIGMA_THRESHOLD,
) -> dict[str, Any]:
    """Return the per-half diagnostic record (mean, std, divergence) + the
    boolean verdict ``diverges`` (True when |μ - projection| > σ_threshold * std).

    Special-case: when std < HALT_MIN_SAMPLE_STD (deterministic half),
    ``diverges`` is the strict check on |μ - projection| > 0.5 USD — so a
    deterministic half whose mean is meaningfully off-projection still
    trips the halt trigger (constant off-target PnL is ITS OWN signal).
    """
    n = len(half)
    if n < 2 or projection == 0.0:
        return {
            "n": n,
            "mean": 0.0,
            "std": 0.0,
            "diff": 0.0,
            "diverges": False,
            "reason": "insufficient_sample" if n < 2 else "projection_zero",
        }
    m = _mean(half)
    s = _std(half)
    diff = m - projection
    # Deterministic half (zero std): divergence is a function of how far the
    # constant mean sits from the projection. Use a fixed USD floor so a
    # 1-cent rounding artefact cannot trip the trigger.
    if s < HALT_MIN_SAMPLE_STD:
        return {
            "n": n, "mean": m, "std": s,
            "diff": diff,
            "diverges": abs(diff) > HALT_DETERMINISTIC_FLOOR_USD,
            "reason": "zero_variance_deterministic",
        }
    return {
        "n": n, "mean": m, "std": s,
        "diff": diff,
        "diverges": abs(diff) > sigma_threshold * s,
        "reason": "evaluated",
    }


def _upsert_today_history(history: list[dict[str, Any]], new_row: dict[str, Any]) -> str:
    """Same-day idempotent upsert used by canary — mirror here."""
    today = _today_iso_date(new_row.get("ts"))
    if history and str(history[-1].get("ts", ""))[:10] == today:
        history[-1] = new_row
        return "overwrote"
    history.append(new_row)
    return "appended"


# ----------------------------------------------------------------------
# State-machine tick
# ----------------------------------------------------------------------
def evaluate_symbol(
    symbol: str,
    *,
    projection_daily_usd: float,
    realized_trades: list[dict[str, Any]] | None = None,
    state: SymbolEdgeState | None = None,
    config: dict[str, Any] | None = None,
    log: logging.Logger | None = None,
) -> tuple[SymbolEdgeState, dict[str, Any]]:
    """Advance one symbol's edge-monitor state by one observation.

    Pure (modulo STATE writes via the caller); entry/exit conditions
    are documented in the module docstring. Returns:

        (new_state, audit_row)

    The audit_row is a plain ``dict`` ready to be appended to
    ``state/edge_monitor_audit.jsonl``. It NEVER contains keys that
    would be interpreted as a config patch — caller is responsible for
    routing the row only through the audit JSONL.
    """
    cfg = (config or {}).get("edge_monitor") or {}
    sigma_threshold = float(cfg.get("sigma_threshold", HALT_SIGMA_THRESHOLD))
    decay_days = int(cfg.get("decay_window_days", HALT_DECAY_WINDOW_DAYS))
    half_days = decay_days // 2
    recovery_required = int(cfg.get("recovery_required_days",
                                   HALT_RECOVERY_REQUIRED_DAYS))

    realized_trades = list(realized_trades or [])
    state = state or SymbolEdgeState(symbol=symbol)
    state.symbol = symbol

    now = utc_now_iso()
    state.last_check_iso = now
    state.projection_daily_usd = float(projection_daily_usd)
    state.projection_cached_at = now

    # ---- E1 guard: no positive projection → no edge to monitor -------------
    if float(projection_daily_usd or 0.0) <= 0.0:
        decision = "no_data"
        state.last_audit_decision = decision
        state.last_audit_iso = now
        audit = _audit_row(
            now=now, symbol=symbol, decision=decision,
            previous_status=state.status, new_status=state.status,
            projection=projection_daily_usd,
            half_a=None, half_b=None,
            in_band_series=state.consecutive_in_band,
            halt_count=state.halt_entered_count,
        )
        return state, audit

    # ---- E2 guard: need at least HALT_MIN_NONZERO_DAYS of close-days for
    # the symbol in the recent lookback; otherwise the window is too sparse
    # to fire WATCH or HALT (avoids advisory flicker on quiet symbols).
    sym_recent_pnls = [
        t.get("pnl") or 0.0 for t in realized_trades if t.get("symbol") == symbol
    ]
    nz_days = sum(1 for p in sym_recent_pnls if abs(p) > 1e-9)
    if nz_days < HALT_MIN_NONZERO_DAYS:
        decision = "no_data"
        state.last_audit_decision = decision
        state.last_audit_iso = now
        audit = _audit_row(
            now=now, symbol=symbol, decision=decision,
            previous_status=state.status, new_status=state.status,
            projection=projection_daily_usd,
            half_a=None, half_b=None,
            in_band_series=state.consecutive_in_band,
            halt_count=state.halt_entered_count,
            nz_days=nz_days,
            min_required_nz_days=HALT_MIN_NONZERO_DAYS,
        )
        return state, audit

    # ---- Realised window ----
    # Anchor the "today" date from the same `now` we captured at function
    # entry so a UTC-midnight transition mid-call can never shift the
    # bucket-by-day keys vs the series-rebuild dates. (Realised trades
    # already snap `closed_at` to a specific timestamp; we just need the
    # SAME today's date for both producer and consumer.)
    today_anchor = (now[:10]) if now else datetime.now(timezone.utc).date().isoformat()
    window = _realized_daily_series(
        realized_trades, symbol, days=decay_days, today_anchor=today_anchor,
    )
    half_a = window[:half_days]
    half_b = window[half_days:]
    half_a_diag = _half_divergence(half_a, projection_daily_usd,
                                   sigma_threshold=sigma_threshold)
    half_b_diag = _half_divergence(half_b, projection_daily_usd,
                                   sigma_threshold=sigma_threshold)

    # ---- ENTRY (halt) conditions ----
    prev_status = state.status
    halt_triggered = (
        half_a_diag["diverges"]
        and half_b_diag["diverges"]
        and state.status != STATUS_HALT
    )

    if halt_triggered:
        state.status = STATUS_HALT
        state.halt_entered_at = now
        state.halt_entered_count += 1
        state.consecutive_in_band = 0
        state.watch_entered_at = None
        state.cooling_off_started_at = None
        decision = "halt_entered"
    else:
        # ---- In-band / partial-divergence / recovery logic ----
        in_band_a = not half_a_diag["diverges"]
        in_band_b = not half_b_diag["diverges"]
        in_band = in_band_a and in_band_b

        # Same-day guard: a 5-minute cron running repeatedly within a single
        # trading day must NOT inflate `consecutive_in_band`. The counter
        # advances at most once per UTC calendar day, so the 5-day recovery
        # contract actually demands 5 distinct trading days, not 5 cron
        # runs.
        today_key = now[:10] if now else ""
        if in_band:
            if state.last_in_band_day != today_key:
                state.consecutive_in_band += 1
                state.last_in_band_day = today_key
        else:
            state.consecutive_in_band = 0
            state.last_in_band_day = None

        # ---- EXIT (recovery) — direct HALT→OK after 5 consecutive
        # in-band observations; the COOLING_OFF intermediate phase is
        # dropped (the original 6-tick path was redundant and confusing).
        # STATUS_COOLING_OFF is kept as a reserved enum value for
        # backward compat with downstream state-doc readers.
        if (
            state.status == STATUS_HALT
            and in_band
            and state.consecutive_in_band >= recovery_required
        ):
            state.status = STATUS_OK
            state.consecutive_observed += 1
            decision = "ok_resumed"
        elif (
            half_a_diag["diverges"] or half_b_diag["diverges"]
        ) and state.status == STATUS_OK:
            # Single-half divergence sets WATCH; not a halt, not a block.
            state.status = STATUS_WATCH
            state.watch_entered_at = now
            decision = "watch_entered"
        elif state.status == STATUS_WATCH and in_band:
            # Back to OK after both halves observed in band.
            state.status = STATUS_OK
            state.watch_entered_at = None
            decision = "ok_resumed_from_watch"
        else:
            decision = "evaluated_no_state_change"

    state.consecutive_observed += 1
    state.last_audit_decision = decision
    state.last_audit_iso = now

    # Idempotent history upsert (same-day re-runs overwrite today's row)
    _upsert_today_history(state.history, {
        "ts": now,
        "decision": decision,
        "symbol": symbol,
        "status": state.status,
        "consecutive_in_band": state.consecutive_in_band,
        "half_a_diverges": half_a_diag["diverges"],
        "half_b_diverges": half_b_diag["diverges"],
    })
    state.history = state.history[-90:]

    audit = _audit_row(
        now=now, symbol=symbol, decision=decision,
        previous_status=prev_status, new_status=state.status,
        projection=projection_daily_usd,
        half_a=half_a_diag, half_b=half_b_diag,
        in_band_series=state.consecutive_in_band,
        halt_count=state.halt_entered_count,
    )
    if log:
        log.info(
            "edge_monitor[%s]: decision=%s status=%s (a_dvg=%s b_dvg=%s "
            "in_band_streak=%d/%d)",
            symbol, decision, state.status,
            half_a_diag["diverges"], half_b_diag["diverges"],
            state.consecutive_in_band, recovery_required,
        )
    return state, audit


def _audit_row(
    *, now: str, symbol: str, decision: str,
    previous_status: str, new_status: str, projection: float,
    half_a: dict | None, half_b: dict | None,
    in_band_series: int, halt_count: int,
    nz_days: int | None = None,
    min_required_nz_days: int | None = None,
) -> dict[str, Any]:
    """Build the JSONL audit row. NEVER carries patch-style keys.

    nz_days + min_required_nz_days are emitted ONLY on no_data rows so
    downstream parsers see absent fields (not zero-valued noise) on the
    normal HALT/WATCH/in-band rows.
    """
    row: dict[str, Any] = {
        "ts": now,
        "symbol": symbol,
        "decision": decision,
        "previous_status": previous_status,
        "new_status": new_status,
        "projection_daily_usd": round(float(projection or 0.0), 4),
        "half_a": half_a,
        "half_b": half_b,
        "consecutive_in_band": int(in_band_series),
        "halt_count": int(halt_count),
        "writes_blocked": list(EDGE_BLOCKED_TARGETS),
    }
    if nz_days is not None:
        row["nz_days"] = int(nz_days)
    if min_required_nz_days is not None:
        row["min_required_nz_days"] = int(min_required_nz_days)
    return row


# ----------------------------------------------------------------------
# Bulk tick — dispatch over projection rows from verify_edge
# ----------------------------------------------------------------------
def tick(
    projections: list[dict[str, Any]],
    *,
    realized_trades: list[dict[str, Any]] | None = None,
    state_doc: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    log: logging.Logger | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Bulk tick: evaluate every trusted+untrusted projection row.

    Args:
        projections: per-symbol rows from verify_edge (each MUST carry
                     ``symbol`` + either ``best.projected_daily_usd`` or
                     ``seed.projected_daily_usd``).
        realized_trades: closed-trade history for authorised PnL lookback.
        state_doc: existing state file content (or None for fresh state).
        config: ``config.yaml`` dict (controls thresholds; see
                ``config.yaml`` ``edge_monitor`` section).

    Returns:
        (new_state_doc, audit_rows)
    """
    state_doc = dict(state_doc or {})
    audit_rows: list[dict[str, Any]] = []
    symbols = sorted({p.get("symbol") for p in projections if p.get("symbol")})
    now = utc_now_iso()

    for sym in symbols:
        row = next((p for p in projections if p.get("symbol") == sym), None)
        if row is None:
            continue
        best = row.get("best") or {}
        seed = row.get("seed") or {}
        try:
            projection = float(
                best.get("projected_daily_pnl_usd")
                if best.get("projected_daily_pnl_usd") is not None
                else seed.get("projected_daily_pnl_usd", 0.0)
                or 0.0
            )
        except (TypeError, ValueError):
            projection = 0.0

        prev_serialised = state_doc.get(sym) or {}
        prev_state = SymbolEdgeState.from_dict(prev_serialised)
        new_state, audit = evaluate_symbol(
            sym,
            projection_daily_usd=projection,
            realized_trades=realized_trades,
            state=prev_state,
            config=config,
            log=log,
        )
        state_doc[sym] = new_state.to_dict()
        audit_rows.append(audit)
    return state_doc, audit_rows


# ----------------------------------------------------------------------
# State persistence (mirrors canary.load_state / save_state / append_audit)
# ----------------------------------------------------------------------
def state_path() -> Path:
    return STATE_DIR / EDGE_MONITOR_STATE_FILE


def audit_path() -> Path:
    return STATE_DIR / EDGE_MONITOR_AUDIT_FILE


def load_state(path: Path | None = None) -> dict[str, Any]:
    p = path or state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state_doc: dict[str, Any], path: Path | None = None) -> None:
    p = path or state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: serialize to .tmp then rename to the canonical path so
    # partial writes on crash never leave a corrupt state file. Mirrors the
    # Phase 2.5 observe-only contract — no config writes, only state.
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state_doc, default=str, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        os.replace(tmp, p)
    except OSError:
        # Some filesystems (e.g. Windows Samba shares) reject cross-volume
        # rename. Fall back to a non-atomic write so the state still
        # survives; explicit failure here preserves debug visibility.
        p.write_text(
            tmp.read_text(encoding="utf-8"), encoding="utf-8",
        )
        try:
            tmp.unlink()
        except OSError:
            pass


def append_audit(audit_row: dict[str, Any], path: Path | None = None) -> None:
    p = path or audit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(audit_row, default=str, ensure_ascii=False) + "\n")


# ----------------------------------------------------------------------
# Order-entry hook
# ----------------------------------------------------------------------
def is_halted(symbol: str, state_path_arg: Path | None = None) -> bool:
    """``True`` iff ``symbol`` is currently in STATUS_HALT *or*
    STATUS_COOLING_OFF (so an order-entry pipeline can short-circuit).

    Reads ``state/edge_monitor_state.json`` synchronously. Safe to call
    on every entry attempt; no I/O happens when the file is absent.
    """
    p = state_path_arg or state_path()
    if not p.exists():
        return False
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    row = doc.get(symbol)
    if not isinstance(row, dict):
        return False
    return row.get("status") in {STATUS_HALT, STATUS_COOLING_OFF}


def current_status(symbol: str, state_path_arg: Path | None = None) -> str:
    """Lightweight status reader; returns ``STATUS_OK`` when unknown."""
    p = state_path_arg or state_path()
    if not p.exists():
        return STATUS_OK
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return STATUS_OK
    row = doc.get(symbol)
    if not isinstance(row, dict):
        return STATUS_OK
    return row.get("status") or STATUS_OK
