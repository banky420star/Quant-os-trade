"""Consensus gates — subsystem vetoes before signal approval."""

from __future__ import annotations

import logging
from typing import Any

from core.setup_library import is_regime_compatible


class ConsensusGates:
    """Hard vetoes from risk, memory, and regime compatibility."""

    def __init__(self, config: dict[str, Any], logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("consensus_gates")
        intel = config.get("intelligence", {})
        trading = config.get("trading", {})
        aggressive = bool(trading.get("aggressive_mode", False))
        self.enabled = bool(intel.get("consensus_gates", True))
        self.risk_veto = int(intel.get("risk_veto_threshold", 25 if aggressive else 40))
        self.regime_veto_enabled = bool(intel.get("regime_veto_enabled", not aggressive))
        self.trend_structure_veto_enabled = bool(intel.get("trend_structure_veto_enabled", not aggressive))
        self.memory_veto_wr = float(intel.get("memory_veto_win_rate", 40))
        self.memory_min_trades = int(intel.get("memory_veto_min_trades", 10))
        self.memory_cell_wr = float(intel.get("memory_veto_cell_win_rate", max(35.0, self.memory_veto_wr)))
        self.memory_cell_min_trades = int(intel.get("memory_veto_cell_min_trades", max(8, self.memory_min_trades // 2)))

    def evaluate(
        self,
        signal: dict[str, Any],
        votes: dict[str, int],
        edge_scores: dict[str, Any] | None = None,
        market_regime: dict[str, Any] | None = None,
    ) -> tuple[bool, list[str]]:
        """Return (passed, veto_reasons)."""
        if not self.enabled:
            return True, []

        vetoes: list[str] = []
        edge_scores = edge_scores or {}
        market_regime = market_regime or {}

        if votes.get("risk_engine", 100) < self.risk_veto:
            vetoes.append(f"risk_veto:{votes.get('risk_engine')}")

        if self.trend_structure_veto_enabled:
            if votes.get("trend_engine", 100) < 35 and votes.get("structure_engine", 100) < 35:
                vetoes.append("trend_structure_veto")

        setup = signal.get("setup_type", "unknown")
        symbol = signal.get("symbol", "")
        primary = market_regime.get("primary", "")
        if self.regime_veto_enabled and primary and not is_regime_compatible(setup, primary):
            vetoes.append(f"regime_veto:{setup}_in_{primary}")

        stats = edge_scores.get("setup_stats", {})
        session = signal.get("market_context", {}).get("session") or market_regime.get("session") or "unknown"
        cell_key = f"{symbol}|{setup}|{primary or 'unknown'}|{session}"
        cell_stats = stats.get("by_cell", {}).get(cell_key, {}).get(setup, {})
        sym_stats = stats.get("by_symbol", {}).get(symbol, {}).get(setup, {})
        global_stats = stats.get("global", {}).get(setup, {})

        s: dict[str, Any] = {}
        threshold = self.memory_veto_wr
        veto_label = "memory_veto"
        min_trades = self.memory_min_trades
        if cell_stats and cell_stats.get("total", 0) >= self.memory_cell_min_trades:
            s = cell_stats
            threshold = self.memory_cell_wr
            veto_label = "cell_memory_veto"
            min_trades = self.memory_cell_min_trades
        elif sym_stats and sym_stats.get("total", 0) >= self.memory_min_trades:
            s = sym_stats
        elif global_stats and global_stats.get("total", 0) >= self.memory_min_trades:
            s = global_stats

        if s and s.get("total", 0) >= min_trades:
            wr = s.get("win_rate_pct", 50)
            if wr < threshold:
                vetoes.append(f"{veto_label}:win_rate_{wr}%")

        if vetoes:
            self.logger.info(
                "Consensus veto %s %s %s — %s",
                symbol,
                signal.get("side"),
                setup,
                "; ".join(vetoes),
            )
        return len(vetoes) == 0, vetoes
