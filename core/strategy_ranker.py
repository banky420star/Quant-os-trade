"""Strategy Ranking Engine — pick best setup for current market conditions."""

from __future__ import annotations

import logging
from typing import Any

from core.edge_database import EdgeDatabase
from core.setup_library import SETUP_LIBRARY
from core.strategy_policy import preferred_setup_rank, setup_allowed, symbol_rule
from core.utils import read_json_state
from quant.research.cell_ranking import demote_vetoed_setups

# Live move_type from market context → setup the classifier can emit.
MOVE_TYPE_SETUP_MAP: dict[str, str] = {
    "pullback": "pullback",
    "continuation": "trend_continuation",
    "compression": "compression_breakout",
    "range": "range_fade",
}


class StrategyRanker:
    """Rank strategies by historical edge under today's regime and session."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("strategy_ranker")
        self.edge_db = EdgeDatabase(logger)
        quant = config.get("quant", {})
        self._quant = quant
        self.enabled = bool(quant.get("strategy_ranking_enabled", True))
        self.min_samples = int(quant.get("min_rank_sample_size", 3))
        self.min_win_rate = float(quant.get("min_rank_win_rate", 35))
        self.require_top_rank = bool(quant.get("require_top_ranked_setup", True))
        self.ranking_flex_enabled = bool(quant.get("ranking_flex_enabled", True))
        self.ranking_flex_min_win_rate = float(quant.get("ranking_flex_min_win_rate", 55))
        self.ranking_flex_min_samples = int(quant.get("ranking_flex_min_samples", 10))
        self.ranking_flex_top_n = max(1, int(quant.get("ranking_flex_top_n", 2)))
        self.ranking_context_align = bool(quant.get("ranking_context_align", True))
        self.cell_veto_demote = bool(quant.get("cell_veto_demote", True))

    def _live_policy_for_symbol(self, symbol: str) -> dict[str, Any]:
        policy = read_json_state("symbol_policy_live.json", default={}) or {}
        symbols = (policy.get("symbols") or {}) if isinstance(policy, dict) else {}
        row = symbols.get(symbol) or {}
        return row if isinstance(row, dict) else {}

    def _quant_for_symbol(self, symbol: str) -> dict[str, Any]:
        per = self._quant.get("per_symbol", {}) or {}
        sym = per.get(symbol, {}) if isinstance(per, dict) else {}
        return sym if isinstance(sym, dict) else {}

    def _ranking_params_for_symbol(self, symbol: str) -> dict[str, Any]:
        sym = self._quant_for_symbol(symbol)
        return {
            "require_top": bool(sym.get("require_top_ranked_setup", self.require_top_rank)),
            "min_win_rate": float(sym.get("min_rank_win_rate", self.min_win_rate)),
            "min_samples": int(sym.get("min_rank_sample_size", self.min_samples)),
            "flex_enabled": bool(sym.get("ranking_flex_enabled", self.ranking_flex_enabled)),
            "flex_min_win_rate": float(sym.get("ranking_flex_min_win_rate", self.ranking_flex_min_win_rate)),
            "flex_min_samples": int(sym.get("ranking_flex_min_samples", self.ranking_flex_min_samples)),
            "flex_top_n": max(1, int(sym.get("ranking_flex_top_n", self.ranking_flex_top_n))),
            "preferred_setups": list(sym.get("preferred_setups") or []),
            "preferred_sessions": list(sym.get("preferred_sessions") or []),
        }

    def rank_for_symbol(
        self,
        symbol: str,
        context: dict[str, Any],
        features: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return setups ranked by edge score for current conditions."""
        feat = features or {}
        regime = context.get("market_regime", {})
        primary = regime.get("primary") or context.get("regime", "unknown")
        session = context.get("session", "unknown")
        volatility = feat.get("volatility_regime", "normal")
        rule = symbol_rule(self.config, symbol)
        params = self._ranking_params_for_symbol(symbol)

        rankings = self.edge_db.rank_setups_for_context(
            symbol,
            primary,
            session,
            volatility,
            min_samples=params["min_samples"],
        )

        if not rankings:
            rankings = self._default_rankings(primary, symbol=symbol)

        live_policy = self._live_policy_for_symbol(symbol)
        promoted_cells = set(live_policy.get("promoted_cells") or [])
        vetoed_cells = set(live_policy.get("vetoed_cells") or [])
        cell_rankings = {
            row.get("setup"): row
            for row in (live_policy.get("cell_rankings") or [])
            if isinstance(row, dict) and row.get("setup")
        }

        for i, r in enumerate(rankings):
            r["rank"] = i + 1
            r["regime"] = primary
            r["session"] = session
            r["preferred_rank"] = preferred_setup_rank(rule, r["setup_type"])
            boost = 0.0
            if r["setup_type"] in params.get("preferred_setups", []):
                boost += 6.0
            if session in params.get("preferred_sessions", []):
                boost += 4.0
            if promoted_cells and any(
                str(p).startswith(f"{r['setup_type']}|") and str(p).endswith(f"|{session}")
                for p in promoted_cells
            ):
                boost += 12.0
            cell_row = cell_rankings.get(r["setup_type"])
            if cell_row and float(cell_row.get("expectancy_net_r") or 0) > 0:
                boost += min(10.0, float(cell_row.get("expectancy_net_r", 0)) * 20.0)
            if boost:
                r["score"] = round(float(r.get("score", 0)) + boost, 3)
                r["quant_boost"] = round(boost, 3)

        rankings.sort(key=lambda r: (r.get("preferred_rank", 999), -float(r.get("score", 0)), r.get("rank", 999)))

        if self.cell_veto_demote and vetoed_cells:
            sym_cells = {
                row.get("cell", ""): {
                    "verdict": row.get("verdict"),
                    "n": row.get("n"),
                }
                for row in (live_policy.get("cell_rankings") or [])
                if isinstance(row, dict) and row.get("cell")
            }
            rankings = demote_vetoed_setups(
                rankings,
                cells=sym_cells,
                vetoed=vetoed_cells,
            )

        return rankings

    def allow_setup(
        self,
        setup_type: str,
        symbol: str,
        context: dict[str, Any],
        features: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Check if setup is top-ranked (or has insufficient data fallback)."""
        if not self.enabled:
            return True, {"allowed": True, "reason": "ranking_disabled"}

        params = self._ranking_params_for_symbol(symbol)
        require_top = params["require_top"]
        min_win_rate = params["min_win_rate"]

        rule = symbol_rule(self.config, symbol)
        if not setup_allowed(rule, setup_type):
            return False, {
                "allowed": False,
                "reason": "symbol_setup_restricted",
                "symbol_rule": rule,
            }

        rankings = self.rank_for_symbol(symbol, context, features)
        if not rankings:
            return True, {"allowed": True, "reason": "no_rankings"}

        top = rankings[0]
        match = next((r for r in rankings if r["setup_type"] == setup_type), None)

        if match and match.get("insufficient_data"):
            return True, {
                "allowed": True,
                "reason": "insufficient_data_fallback",
                "rankings": rankings[:5],
            }

        if match is None and all(r.get("insufficient_data") for r in rankings):
            return True, {
                "allowed": True,
                "reason": "unranked_insufficient_fallback",
                "rankings": rankings[:5],
            }

        if not require_top:
            if match and match.get("win_rate_pct", 0) >= min_win_rate:
                return True, {
                    "allowed": True,
                    "reason": "ranking_not_required",
                    "rank": match["rank"],
                    "rankings": rankings[:5],
                }
            if match is None:
                return False, {
                    "allowed": False,
                    "reason": "setup_unranked",
                    "rankings": rankings[:5],
                }
            return False, {
                "allowed": False,
                "reason": f"win_rate_below_{min_win_rate}",
                "rankings": rankings[:5],
            }

        allowed_depth = self._allowed_rank_depth(rankings, params)
        allowed_setups = {r["setup_type"] for r in rankings[:allowed_depth]}

        if setup_type in allowed_setups:
            rank = match["rank"] if match else 1
            reason = "top_ranked" if allowed_depth == 1 and rank == 1 else "top_n_flex"
            return True, {
                "allowed": True,
                "reason": reason,
                "rank": rank,
                "allowed_depth": allowed_depth,
                "score": match.get("score") if match else top.get("score"),
                "rankings": rankings[:5],
            }

        context_allowed, context_info = self._context_aligned_allowance(
            setup_type, match, rankings, context
        )
        if context_allowed:
            return True, context_info

        if top.get("insufficient_data"):
            return True, {"allowed": True, "reason": "top_insufficient_fallback", "rankings": rankings[:5]}

        return False, {
            "allowed": False,
            "reason": "not_top_ranked",
            "top_setup": top["setup_type"],
            "top_score": top["score"],
            "allowed_depth": allowed_depth,
            "rankings": rankings[:5],
        }

    def _context_aligned_allowance(
        self,
        setup_type: str,
        match: dict[str, Any] | None,
        rankings: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        """Allow a ranked setup that matches the live market move when strict top-1 blocks it."""
        if not self.ranking_context_align or not match:
            return False, {}

        move_type = context.get("move_type", "")
        aligned_setup = MOVE_TYPE_SETUP_MAP.get(move_type)
        if not aligned_setup or setup_type != aligned_setup:
            return False, {}

        depth = min(self.ranking_flex_top_n, len(rankings))
        top_slice = rankings[:depth]
        if setup_type not in {r["setup_type"] for r in top_slice}:
            return False, {}

        return True, {
            "allowed": True,
            "reason": "context_aligned",
            "rank": match["rank"],
            "allowed_depth": depth,
            "move_type": move_type,
            "score": match.get("score"),
            "rankings": rankings[:5],
        }

    def _allowed_rank_depth(
        self,
        rankings: list[dict[str, Any]],
        params: dict[str, Any] | None = None,
    ) -> int:
        """Return how many ranked setups may trade (1 = strict leader only)."""
        p = params or {}
        require_top = p.get("require_top", self.require_top_rank)
        if not require_top:
            return len(rankings)

        flex_enabled = p.get("flex_enabled", self.ranking_flex_enabled)
        flex_top_n = p.get("flex_top_n", self.ranking_flex_top_n)
        flex_min_wr = p.get("flex_min_win_rate", self.ranking_flex_min_win_rate)
        flex_min_samples = p.get("flex_min_samples", self.ranking_flex_min_samples)

        if not flex_enabled or not rankings:
            return 1

        top = rankings[0]
        if top.get("insufficient_data"):
            return min(flex_top_n, len(rankings))

        win_rate = float(top.get("win_rate_pct", 0))
        samples = int(top.get("total", 0))
        if win_rate < flex_min_wr or samples < flex_min_samples:
            return min(flex_top_n, len(rankings))

        return 1

    def _default_rankings(self, primary: str, *, symbol: str | None = None) -> list[dict[str, Any]]:
        """Prioritize setups compatible with regime when no history exists."""
        rule = symbol_rule(self.config, symbol) if symbol else {}
        regime_setup_map = {
            "strong_trend": ["trend_continuation", "pullback", "breakout"],
            "weak_trend": ["pullback", "trend_continuation"],
            "range": ["range_fade", "mean_reversion", "liquidity_sweep"],
            "compression": ["compression_breakout", "breakout", "trend_continuation", "pullback"],
            "expansion": ["breakout", "trend_continuation"],
            "transitional": ["pullback", "trend_continuation", "breakout", "liquidity_sweep"],
            "volatility_spike": [],
        }
        preferred = regime_setup_map.get(primary, list(SETUP_LIBRARY.keys()))
        if rule.get("preferred_setups"):
            preferred = [s for s in rule["preferred_setups"] if s in SETUP_LIBRARY] + [
                s for s in preferred if s not in rule["preferred_setups"] and s in SETUP_LIBRARY
            ]
        return [
            {
                "setup_type": s,
                "score": 50 - i * 5,
                "win_rate_pct": 50,
                "total": 0,
                "insufficient_data": True,
            }
            for i, s in enumerate(preferred[:5])
        ]
