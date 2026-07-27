"""Review dashboard API population against state files."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.utils import read_json_state
from dashboard.server import aggregate_state


def main() -> int:
    d = aggregate_state()
    account = read_json_state("account.json", default={})
    features = read_json_state("features.json", default={})
    positions = read_json_state("paper_positions.json", default={})

    issues: list[str] = []
    ok: list[str] = []

    feat_syms = list((features.get("symbols") or {}).keys())
    card_syms = [c["symbol"] for c in d.get("symbol_cards", [])]
    if len(card_syms) == len(feat_syms) and set(card_syms) == set(feat_syms):
        ok.append(f"symbol_cards: {len(card_syms)} symbols match features.json")
    else:
        issues.append(f"symbol_cards mismatch: cards={card_syms} features={feat_syms}")

    live = d.get("live_portfolio", {})
    if live.get("equity") == account.get("equity"):
        ok.append(f"live_portfolio.equity matches account.json: ${live.get('equity')}")
    else:
        issues.append(
            f"equity mismatch: live={live.get('equity')} account={account.get('equity')}"
        )

    pos_count = len(positions.get("positions", []))
    if live.get("position_count") == pos_count:
        ok.append(f"live_portfolio.position_count: {pos_count}")
    else:
        issues.append(f"position_count: live={live.get('position_count')} actual={pos_count}")

    unrealized = sum(float(p.get("profit", 0)) for p in positions.get("positions", []))
    if abs(live.get("unrealized_pnl", 0) - unrealized) < 0.02:
        ok.append(f"unrealized_pnl: ${live.get('unrealized_pnl')} (sum of position profit)")
    else:
        issues.append(f"unrealized_pnl: live={live.get('unrealized_pnl')} actual={unrealized}")

    for card in d.get("symbol_cards", []):
        sym = card["symbol"]
        feat = features.get("symbols", {}).get(sym, {})
        if card.get("price") != feat.get("price"):
            issues.append(f"{sym}: price card={card.get('price')} feat={feat.get('price')}")
        elif feat:
            ok.append(f"{sym}: price={card.get('price')} trend={card.get('trend')} vol={card.get('volatility_regime')}")

    if d.get("symbols") == features.get("symbols"):
        ok.append("top-level symbols alias matches features.symbols")
    else:
        issues.append("symbols alias does not match features.symbols")

    curve = d.get("equity_curve", {})
    if curve.get("current_equity") == live.get("equity"):
        ok.append(f"equity_curve.current_equity: ${curve.get('current_equity')}")
    else:
        issues.append(
            f"equity_curve vs live: curve={curve.get('current_equity')} live={live.get('equity')}"
        )

    print("=" * 60)
    print("DASHBOARD POPULATION REVIEW")
    print("=" * 60)
    print("\nPASS:")
    for line in ok:
        print(f"  ✓ {line}")
    if issues:
        print("\nISSUES:")
        for line in issues:
            print(f"  ✗ {line}")
    else:
        print("\nNo issues found.")

    print("\nLIVE PORTFOLIO:")
    print(json.dumps(live, indent=2))
    print("\nSAMPLE SYMBOL CARD (XAUUSDm):")
    xau = next((c for c in d.get("symbol_cards", []) if c["symbol"] == "XAUUSDm"), None)
    print(json.dumps(xau, indent=2) if xau else "  missing")

    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())