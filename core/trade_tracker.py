"""Closed-trade detection for paper and MT5 portfolios."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from core.entry_narrative import build_exit_narrative, r_multiple as entry_r_multiple
from core.exit_manager import near_take_profit
from core.symbol_manager import logical_symbol
from core.strategy_policy import normalize_setup_type
from core.trade_journal import build_mgmt_narratives
# Tier-2 mgmt archive (2026-07-20): when state/position_management.json has
# been rolled (or never contained this ticket), recover the final mgmt_row
# from state/position_mgmt_archive.jsonl so the closed-trade record still
# carries the BE/Partial/Stale flags the dashboard Profit Quality tile needs.
from core.utils import (
    append_payoff_paradox_audit,
    read_archive_for_ticket,
    read_archive_index,
    read_archive_index_cached,
    read_json_state,
    utc_now_iso,
)

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore

import re as _re
_QAGENT_COMMENT_RE = _re.compile(r"^qagent_(.+)$")


def _hydrate_mgmt_from_archive(
    ticket: str | int | None,
    live_mgmt: dict[str, Any],
    archive_latest_by_ticket: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """If ``live_mgmt`` is empty (no live keys) AND an archive record exists
    for ``ticket``, fold it into live_mgmt and stamp ``_from_archive=True``.

    NOTE: live mgmt is treated as "absent" when it has no keys OR when every
    value is False/null. The reviewer-flagged bug was that an all-False dict
    (``{"break_even": False, "trailing": False}`` from a paper close where
    no BE/trail ever triggered) is truthy by Python dict semantics, so a
    naive ``if live_mgmt:`` guard would skip the archive fallback even when
    the archive carries richer information. We use ``any(live_mgmt.values())``
    so an empty/all-false live still consults the archive.
    The live row always wins when ANY flag is True (most-recent in-cycle state).

    Caveat: if a position is reopened under the same MT5 ticket_id across
    two distinct lifecycles (rare; usually only via retry paths), the most
    recent archive record wins regardless of position boundary.
    Returns (merged_mgmt, archive_used).
    """
    if not ticket:
        return live_mgmt, False
    if any(bool(v) for v in live_mgmt.values()):
        return live_mgmt, False
    ticket_key = str(ticket)
    rec = archive_latest_by_ticket.get(ticket_key)
    if not rec:
        return live_mgmt, False
    snap = rec.get("mgmt_row") or {}
    merged = dict(snap)
    merged["_from_archive"] = True
    merged["_archive_reason"] = rec.get("reason")
    return merged, True




def _setup_from_in_comment(comment: str) -> str:
    """Recover the setup label from the OPENING (IN) deal comment.

    MT5 overwrites the CLOSING (OUT) deal comment with the close reason
    (e.g. "[sl 4055.73]"), so setup_type cannot be recovered from the OUT deal.
    The IN deal keeps the entry comment "qagent_<setup>" (truncated to 31 chars,
    so "qagent_trend_continuation" -> "qagent_trend_con"). Map the truncated
    forms back to canonical setup labels; return "" when not recoverable.
    """
    if not comment:
        return ""
    m = _QAGENT_COMMENT_RE.match(comment.strip())
    if not m:
        return ""
    s = m.group(1)
    if s.startswith("trend_con"):
        return "trend_continuation"
    if s.startswith("pullback"):
        return "pullback"
    if s.startswith("compression"):
        return "compression_breakout"
    if s.startswith("range"):
        return "range_fade"
    return s


# ---- Payoff paradox audit (2026-07-20) -------------------------------------
# Per-trade audit row appended to state/payoff_paradox_audit.jsonl by
# _log_payoff_paradox_audit_batch at the END of each close cycle so the
# fitter (scripts/fit_payoff_paradox_floor.py) can recommend a new floor
# for trading.exits.min_r_multiple_win based on realized median winner R.
def _resolve_dotted_get(node: Any, path: str, default: Any) -> Any:
    """Walk a dotted-path key inside a nested dict; return default when any
    segment is missing or the leaf isn't coercible to float."""
    cur = node
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    try:
        return float(cur)
    except (TypeError, ValueError):
        return default


def _build_payoff_paradox_row(
    trade: dict[str, Any],
    projected_floor: float,
) -> dict[str, Any] | None:
    """Coerce a closed-trade record + projected_floor into the JSONL audit
    schema. Returns None when entry/exit/side are unintelligible."""
    if not isinstance(trade, dict):
        return None
    try:
        entry = float(trade.get("entry"))
        exit_price = float(trade.get("exit"))
    except (TypeError, ValueError):
        return None
    side = str(trade.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        return None
    symbol = str(trade.get("symbol") or "").strip()
    if not symbol:
        return None
    sl_raw = trade.get("sl")
    try:
        sl = float(sl_raw) if sl_raw is not None else None
    except (TypeError, ValueError):
        sl = None
    risk_distance = (abs(entry - sl) if (sl is not None and sl > 0) else 0.0)
    if side == "BUY":
        r = (exit_price - entry) / risk_distance if risk_distance > 0 else None
    else:
        r = (entry - exit_price) / risk_distance if risk_distance > 0 else None
    if side == "BUY":
        whb = (entry + projected_floor * risk_distance) if risk_distance > 0 else None
    else:
        whb = (entry - projected_floor * risk_distance) if risk_distance > 0 else None
    pnl = trade.get("pnl")
    try:
        pnl_f = float(pnl) if pnl is not None else 0.0
    except (TypeError, ValueError):
        pnl_f = 0.0
    has_priced_sl = bool(sl is not None and sl > 0)
    # REVIEW FIX 3: when SL was unpriced, r is None; for the result field we
    # fall back to the trade's explicit "result" or pnl sign, NOT the r value
    # (so unpriced losers don't accidentally become "win").
    result = trade.get("result")
    if result not in ("win", "loss"):
        if r is not None:
            result = "win" if r > 0 else "loss"
        else:
            result = "win" if pnl_f > 0 else "loss"
    return {
        "ts": str(trade.get("closed_at") or trade.get("close_ts") or ""),
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "exit": exit_price,
        "sl": sl,
        "r": (round(r, 6) if isinstance(r, float) else None),
        "would_have_been_locked_at": round(whb, 8) if isinstance(whb, float) else None,
        "projected_floor": round(float(projected_floor), 6),
        "result": result,
        "pnl": round(pnl_f, 2),
        "ticket": str(trade.get("position_id") or trade.get("ticket") or ""),
        "_has_priced_sl": has_priced_sl,
    }


def _log_payoff_paradox_audit_batch(
    trades: list[dict[str, Any]],
    config: Any = None,
) -> int:
    """Audit-log a batch of closed trades to state/payoff_paradox_audit.jsonl.
    Returns the number of rows successfully appended (after dedup). Never
    raises — the close cycle MUST keep going even when the JSONL is unwritable.
    """
    if not trades:
        return 0
    cfg = config if isinstance(config, dict) else {}
    ppx_cfg = ((cfg.get("trading") or {}).get("exits") or {}).get("payoff_paradox", {})
    if not ppx_cfg.get("audit_enabled", True):
        return 0
    try:
        audit_filename = str(ppx_cfg.get("audit_filename") or "payoff_paradox_audit.jsonl")
    except (TypeError, ValueError):
        audit_filename = "payoff_paradox_audit.jsonl"
    projection_field = str(
        ppx_cfg.get("projection_field") or "trading.exits.min_r_multiple_win"
    )
    projected_floor = float(_resolve_dotted_get(cfg, projection_field, 0.4))
    appended = 0
    for trade in trades:
        row = _build_payoff_paradox_row(trade, projected_floor)
        if row is None:
            continue
        if append_payoff_paradox_audit(row, filename=audit_filename) is not None:
            appended += 1
    return appended


class TradeTracker:
    """Detect newly closed trades and merge into trade history."""

    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger("trade_tracker")

    def merge_new_trades(
        self,
        existing_trades: list[dict[str, Any]],
        incoming: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Append only trades not already recorded. Returns (all_trades, newly_added)."""
        known_ids = {t.get("trade_id") for t in existing_trades if t.get("trade_id")}
        merged = list(existing_trades)
        added: list[dict[str, Any]] = []
        for trade in incoming:
            tid = trade.get("trade_id")
            if tid and tid in known_ids:
                continue
            merged.append(trade)
            added.append(trade)
            if tid:
                known_ids.add(tid)
        return merged, added


    def detect_paper_closed(
        self,
        previous_positions: list[dict[str, Any]],
        current_positions: list[dict[str, Any]],
        prices: dict[str, float],
    ) -> list[dict[str, Any]]:
        """Infer closed paper positions when they disappear between cycles."""
        current_ids = {p.get("position_id") for p in current_positions}
        closed_trades: list[dict[str, Any]] = []
        archive_index = read_archive_index_cached("position_mgmt_archive.jsonl")

        for pos in previous_positions:
            pid = pos.get("position_id")
            if pid in current_ids:
                continue
            price = prices.get(pos["symbol"], pos.get("entry", 0))
            exit_price = price
            exit_reason = "closed_externally"
            pnl = self._calc_pnl(pos, exit_price)
            meta = pos.get("signal_meta") or {}
            # Tier-2: enrich position_mgmt from archive if the live row is gone.
            # Only seed mgmt_live if at least one live flag is True; otherwise
            # the dict is all-False, the hydrator falls through to archive, and
            # the live dict's emptiness does not block archive recovery.
            mgmt_live: dict[str, Any] = {}
            if pos.get("be_triggered") or pos.get("trail_active"):
                mgmt_live = {
                    "break_even": bool(pos.get("be_triggered")),
                    "trailing": bool(pos.get("trail_active")),
                }
            mgmt_row, _from_archive = _hydrate_mgmt_from_archive(
                pid, mgmt_live, archive_index,
            )
            # Infer exit reason from mgmt_row flags (2026-07-21). Paper closes
            # don't have MT5 DEAL_REASON codes, but the archive/live mgmt_row
            # carries the actual BE/trail/partial flags from the position's
            # lifecycle. Priority matches sync_mt5_closed_deals():
            #   partial > trail > BE > closed_externally
            # The ``pnl`` sign is intentionally NOT checked — the exit reason
            # describes *how* the position exited, not whether it was profitable.
            paper_partial = bool(mgmt_row.get("partial_tp_done"))
            paper_trail = bool(mgmt_row.get("trailing"))
            paper_be = bool(mgmt_row.get("break_even"))
            if paper_partial:
                exit_reason = "partial_take_profit"
            elif paper_trail:
                exit_reason = "trailing_stop"
            elif paper_be:
                exit_reason = "break_even_stop"
            # else keep "closed_externally" as the fallback
            try:
                entry = float(pos.get("entry"))
                stop = float(pos.get("sl"))
                realized_r = round(entry_r_multiple(pos.get("side", ""), entry, stop, exit_price), 3)
            except (TypeError, ValueError):
                realized_r = None
            closed_trade = {
                "trade_id": str(uuid.uuid4()),
                "position_id": pid,
                "signal_id": pos.get("signal_id"),
                "adaptive_symbol_proposal_id": pos.get("adaptive_symbol_proposal_id"),
                "symbol": pos["symbol"],
                "side": pos["side"],
                "entry": pos["entry"],
                "exit": exit_price,
                "sl": pos.get("sl"),
                "tp1": pos.get("tp1"),
                "pnl": round(pnl, 2),
                "result": "win" if pnl > 0 else "loss",
                "exit_reason": exit_reason,
                "setup_type": pos.get("setup_type"),
                "reason": pos.get("reason"),
                "signal_meta": meta,
                "confidence": meta.get("confidence"),
                "confidence_tree": meta.get("confidence_tree"),
                "evidence": meta.get("evidence"),
                "market_context": meta.get("market_context"),
                "be_narrative": pos.get("be_narrative"),
                "trail_narrative": pos.get("trail_narrative"),
                "be_triggered": bool(mgmt_row.get("break_even")),
                "trail_active": bool(mgmt_row.get("trailing")),
                "position_mgmt": mgmt_row,
                "position_mgmt_source": "live" if not _from_archive else "archive",
                "mae_R": mgmt_row.get("mae_R"),
                "mfe_R": mgmt_row.get("mfe_R"),
                "closed_at": utc_now_iso(),
            }
            if realized_r is not None and abs(realized_r) > 0:
                closed_trade["r_multiple"] = realized_r
            closed_trades.append(closed_trade)

        if closed_trades:
            from core.entry_staging import clear_symbol
            for trade in closed_trades:
                sym = trade.get("symbol")
                if sym:
                    clear_symbol(str(sym))
            self.logger.info("Detected %d paper closed trades", len(closed_trades))
        try:
            _log_payoff_paradox_audit_batch(closed_trades)
        except Exception as _exc:
            logging.getLogger("trade_tracker").warning(
                "Payoff paradox audit-batch logging failed: %s", _exc,
            )
        return closed_trades

    def sync_mt5_closed_deals(
        self,
        existing_trades: list[dict[str, Any]],
        magic: int,
        days: int = 30,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Fetch MT5 OUT deals with agent magic and merge into trade history."""
        if mt5 is None:
            return existing_trades, []

        from datetime import datetime, timedelta, timezone

        since = datetime.now(timezone.utc) - timedelta(days=days)
        deals = mt5.history_deals_get(since, datetime.now(timezone.utc))
        if not deals:
            return existing_trades, []

        # Map position_id -> opening (IN) deal for entry price, volume, comment.
        # Critical: without the IN deal, entry falls back to OUT price (entry==exit)
        # and R-multiples / ghost learning go blind — the live book had 141/146
        # trades with entry==exit for this reason.
        in_deal_by_pos: dict[Any, Any] = {}
        in_comment_by_pos: dict[Any, str] = {}
        for _d in deals:
            if _d.entry == mt5.DEAL_ENTRY_IN and _d.magic == magic:
                _pid = getattr(_d, "position_id", None)
                if _pid is not None:
                    in_deal_by_pos[_pid] = _d
                    in_comment_by_pos[_pid] = _d.comment or ""

        # USER-AUTHORIZED 2026-07-01 fix (forward-test / organic-culturing enabler):
        # Join each closing MT5 deal back to its originating order's signal_meta
        # by position ticket, so a closed live trade carries the FULL context tuple
        # (setup | regime | bias | confidence | market_context | evidence) it needs
        # for the adaptive learners to "culture" from live results. Previously only
        # setup_type was recovered from the deal comment and regime/confidence/
        # market_context/signal_id were dropped at close time — so the ranker's
        # by_context aggregates and the win-template gate (keyed on setup|regime|
        # bias) were starved of live data and could only learn from paper/replay.
        # The opening order for any closing position lives in paper_orders.json
        # (written when the position was placed), keyed by mt5_ticket == the MT5
        # position ticket == deal.position_id for both legs of the position.
        orders_state = read_json_state("paper_orders.json", default={})
        ticket_index: dict[str, dict[str, Any]] = {}
        for o in (orders_state.get("orders") or []):
            tkt = o.get("mt5_ticket")
            if tkt and o.get("status") == "filled":
                ticket_index[str(tkt)] = o

        mgmt_state = read_json_state("position_management.json", default={"positions": {}})
        mgmt_positions = mgmt_state.get("positions") if isinstance(mgmt_state.get("positions"), dict) else {}
        # Tier-2: load archive index once per sync (mtime-cached) so each
        # closed deal can recover its BE/Partial/Stale flags even when
        # mgmt_state rolled. The canonical helper in core.utils caches by
        # (filename, key, latest_per_key) tuple — keep args identical to
        # detect_paper_closed's call to share one cache slot per poll.
        archive_index = read_archive_index_cached("position_mgmt_archive.jsonl")

        incoming: list[dict[str, Any]] = []
        for deal in deals:
            if deal.magic != magic or deal.entry != mt5.DEAL_ENTRY_OUT:
                continue
            comment = deal.comment or ""
            deal_ts = datetime.fromtimestamp(int(deal.time), tz=timezone.utc).isoformat()
            pos_id = getattr(deal, "position_id", None)
            order = ticket_index.get(str(pos_id)) if pos_id else None
            meta = (order or {}).get("signal_meta") or {}
            if not isinstance(meta, dict):
                meta = {}
            in_deal = in_deal_by_pos.get(pos_id) if pos_id is not None else None
            _in_setup = _setup_from_in_comment(in_comment_by_pos.get(pos_id, "")) if pos_id else ""
            setup_type = normalize_setup_type(
                (order or {}).get("setup_type") or meta.get("setup_type") or _in_setup or comment,
                meta=meta,
                market_context=meta.get("market_context") if isinstance(meta.get("market_context"), dict) else {},
            )
            # Entry priority: order fill → IN deal price → never OUT price alone.
            if order and order.get("fill_price") is not None:
                entry_price = float(order["fill_price"])
            elif in_deal is not None:
                entry_price = float(in_deal.price)
            else:
                entry_price = float(deal.price)  # last resort (will flag below)
            exit_price = float(deal.price)
            mgmt_row_live = dict(mgmt_positions.get(str(pos_id), {})) if pos_id else {}
            mgmt_row, _from_archive = _hydrate_mgmt_from_archive(
                pos_id, mgmt_row_live, archive_index,
            )
            # SL priority: signal meta → order → mgmt initial_sl → MT5 history order → None
            sl_at_close = (
                meta.get("sl")
                or (order or {}).get("sl")
                or mgmt_row.get("initial_sl")
                or mgmt_row.get("sl")
            )
            try:
                sl_at_close = float(sl_at_close) if sl_at_close is not None else None
            except (TypeError, ValueError):
                sl_at_close = None
            if (sl_at_close is None or sl_at_close <= 0) and pos_id is not None:
                try:
                    hist_orders = mt5.history_orders_get(position=int(pos_id))
                except Exception:
                    hist_orders = None
                if hist_orders:
                    for ho in hist_orders:
                        hsl = float(getattr(ho, "sl", 0) or 0)
                        if hsl > 0:
                            sl_at_close = hsl
                            break
            # Loss closed at stop: exit price is the SL that was hit → R ≈ -1
            if (sl_at_close is None or sl_at_close <= 0) and float(deal.profit) < 0:
                if abs(exit_price - entry_price) > 1e-12:
                    sl_at_close = exit_price
            # OUT deals run opposite to the position side (close BUY -> SELL deal).
            if deal.entry == mt5.DEAL_ENTRY_OUT:
                pos_side = "BUY" if deal.type == mt5.DEAL_TYPE_SELL else "SELL"
            else:
                pos_side = "BUY" if deal.type == mt5.DEAL_TYPE_BUY else "SELL"
            side = (order or {}).get("side") or meta.get("side") or pos_side
            be_triggered = bool(mgmt_row.get("break_even"))
            trail_active = bool(mgmt_row.get("trailing"))
            narr = build_mgmt_narratives(
                side=side,
                entry=entry_price,
                sl=sl_at_close,
                mgmt_row=mgmt_row,
            )
            # Map MT5 deal reason code to internal exit taxonomy (2026-07-21).
            # Provides a meaningful fallback when position_mgmt enrichment is
            # absent, so the Profit Quality dashboard shows accurate exit
            # diagnostics instead of 100% 'mt5_close' for historic backfills.
            _dr = None
            exit_reason = "mt5_close"
            try:
                _dr = getattr(deal, "reason", None)
                if _dr is not None:
                    if _dr == mt5.DEAL_REASON_TP:
                        exit_reason = "take_profit"
                    elif _dr == mt5.DEAL_REASON_SL:
                        exit_reason = "stop_loss"
                    elif _dr == mt5.DEAL_REASON_SO:
                        exit_reason = "stop_out"
            except (AttributeError, TypeError):
                pass
            # Heuristic overrides below are more precise when position_mgmt
            # data exists (partial_tp_done, be_triggered, trail_active flags).
            # The deal.reason mapping above serves as the fallback when mgmt
            # enrichment is absent (historic backfilled trades).
            tp1 = float(meta.get("tp1") or 0)
            tp2 = float(meta.get("tp2") or 0)
            if mgmt_row.get("partial_tp_done") and deal.comment and "partial" in deal.comment.lower():
                exit_reason = "partial_take_profit"
            elif float(deal.profit) > 0 and near_take_profit(
                exit_price,
                tp2 if mgmt_row.get("partial_tp_done") and tp2 > 0 else tp1,
                side=side,
            ):
                exit_reason = "take_profit"
            elif trail_active:
                exit_reason = "trailing_stop"
            elif be_triggered:
                exit_reason = "break_even_stop"
            # Final fallback (2026-07-22): for mt5_close trades that no
            # DEAL_REASON code or mgmt heuristic resolved, use profit sign +
            # stale_closed flag to make a best-effort classification. These
            # are typically MT5 trades where the broker reported the exit as
            # DEAL_REASON_EXPERT (EA-placed order) instead of SL/TP/SO —
            # common on Exness and other ECN brokers that report ALL EA
            # actions as EXPERT regardless of whether it was a stop hit or
            # a manual close. The mgmt_row recovered from archive carries
            # the stale_closed flag set by position_manager for time_stops,
            # and profit sign is the best heuristic for stop_loss vs
            # take_profit when no trail/BE/partial flag was set.
            if exit_reason == "mt5_close":
                if mgmt_row.get("stale_closed"):
                    exit_reason = "time_stop"
                elif float(deal.profit) < 0:
                    exit_reason = "stop_loss"
                elif float(deal.profit) > 0:
                    exit_reason = "take_profit"
                # profit == 0: leave as mt5_close (rare; ambiguous)
            exit_narrative = build_exit_narrative(
                side,
                entry_price,
                float(sl_at_close or entry_price),
                float(meta.get("tp1") or entry_price),
                exit_price,
                exit_reason,
                float(deal.profit),
            )
            volume = float(
                (order or {}).get("volume")
                or (getattr(in_deal, "volume", None) if in_deal is not None else None)
                or getattr(deal, "volume", 0)
                or 0
            )
            # Price-based R when SL known (pnl-based refined later in build_trade_log).
            r_multiple = None
            risk_price = None
            if sl_at_close is not None and entry_price and abs(entry_price - sl_at_close) > 0:
                risk_price = abs(entry_price - float(sl_at_close))
                if risk_price > 0 and abs(entry_price - exit_price) > 1e-12:
                    direction = 1.0 if str(side).upper() == "BUY" else -1.0
                    r_multiple = round(direction * (exit_price - entry_price) / risk_price, 3)
            entry_equals_exit = abs(entry_price - exit_price) < 1e-12
            incoming.append({
                "trade_id": str(deal.ticket),
                "mt5_deal": deal.ticket,
                "mt5_position": int(pos_id) if pos_id else None,
                "signal_id": (order or {}).get("signal_id") or meta.get("signal_id"),
                "symbol": logical_symbol(deal.symbol),
                "side": side,
                "entry": entry_price,
                "exit": exit_price,
                "sl": sl_at_close,
                "sl_initial": sl_at_close,
                "tp1": meta.get("tp1"),
                "volume": volume,
                "pnl": float(deal.profit),
                "result": "win" if deal.profit > 0 else ("breakeven" if deal.profit == 0 else "loss"),
                "exit_reason": exit_reason,
                "setup_type": (order or {}).get("setup_type") or meta.get("setup_type") or setup_type or "unknown",
                "reason": (order or {}).get("reason") or meta.get("reason"),
                "signal_meta": meta,
                "confidence": meta.get("confidence"),
                "confidence_tree": meta.get("confidence_tree"),
                "evidence": meta.get("evidence"),
                "market_context": meta.get("market_context"),
                "move_type": meta.get("market_context", {}).get("move_type") if isinstance(meta.get("market_context"), dict) else None,
                "market_intent": meta.get("market_context", {}).get("market_intent") if isinstance(meta.get("market_context"), dict) else None,
                "be_narrative": narr.get("be"),
                "trail_narrative": narr.get("trail"),
                "exit_narrative": exit_narrative,
                "be_triggered": be_triggered,
                "trail_active": trail_active,
                "position_mgmt": mgmt_row,
                "position_mgmt_source": "live" if not _from_archive else "archive",
                "mae_R": mgmt_row.get("mae_R"),
                "mfe_R": mgmt_row.get("mfe_R"),
                "risk_price": risk_price,
                "r_multiple": r_multiple,
                "entry_source": (
                    "order" if order and order.get("fill_price") is not None
                    else ("in_deal" if in_deal is not None else "out_deal_fallback")
                ),
                "entry_equals_exit": entry_equals_exit,
                "closed_at": deal_ts,
            })

        merged, added = self.merge_new_trades(existing_trades, incoming)
        if added:
            wins = sum(1 for t in added if t.get("result") == "win")
            losses = len(added) - wins
            self.logger.info("MT5 closed trades: %d new (%d wins, %d losses)", len(added), wins, losses)
        try:
            _log_payoff_paradox_audit_batch(added)
        except Exception as _exc:
            logging.getLogger("trade_tracker").warning(
                "Payoff paradox audit-batch logging failed: %s", _exc,
            )
        return merged, added

    @staticmethod
    def _calc_pnl(pos: dict[str, Any], exit_price: float) -> float:
        diff = exit_price - float(pos.get("entry", 0))
        if pos.get("side") == "SELL":
            diff = -diff
        return diff * float(pos.get("size", 0.01))
