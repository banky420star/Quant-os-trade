"""Fix loops/trade_log_loop.py: trade_log.json is now a root-level LIST
written by build_log, so the prior pattern of stuffing _paper_trades_mtime /
_paper_orders_mtime into the payload collapses — read-back sees a LIST,
defaults the mtimes to 0, and forces a rebuild EVERY cycle.

Fix: write the mtimes to a separate state/trade_log_meta.json (always DICT)
and read from there next cycle. The trade_log.json payload stays as-is
(a LIST, the canonical shape consumed by _build_profit_quality +
learning_review_loop + adaptive_gates + regime_evolution).
"""
import pathlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "loops" / "trade_log_loop.py"


def main() -> int:
    src = TARGET.read_text(encoding="utf-8", errors="replace")
    old = (
        "    trades_m, orders_m = _source_mtime()\n"
        "    prev = read_json_state(\"trade_log.json\", default={}) or {}\n"
        "    _prev_dict = prev if isinstance(prev, dict) else {}\n"
        "    last_trades_m = float(_prev_dict.get(\"_paper_trades_mtime\", 0.0) or 0.0)\n"
        "    last_orders_m = float(_prev_dict.get(\"_paper_orders_mtime\", 0.0) or 0.0)\n"
        "\n"
        "    # Rebuild when a trade closed OR a new order filled (richer signal_meta).\n"
        "    if prev and trades_m <= last_trades_m and orders_m <= last_orders_m:\n"
        "        return prev\n"
        "\n"
        "    try:\n"
        "        payload = build_log(config, days=30, log=logger)\n"
        "        payload[\"_paper_trades_mtime\"] = trades_m\n"
        "        payload[\"_paper_orders_mtime\"] = orders_m\n"
        "        write_json_state(\"trade_log.json\", payload)\n"
    )
    new = (
        "    trades_m, orders_m = _source_mtime()\n"
        "    # Mtimes live in a separate state/trade_log_meta.json (always DICT).\n"
        "    # trade_log.json itself is now a root-level LIST written by build_log,\n"
        "    # so we cannot rely on prev's _paper_*_mtime keys (they're absent).\n"
        "    prev_meta = read_json_state(\"trade_log_meta.json\", default={}) or {}\n"
        "    last_trades_m = float(prev_meta.get(\"paper_trades_mtime\", 0.0) or 0.0)\n"
        "    last_orders_m = float(prev_meta.get(\"paper_orders_mtime\", 0.0) or 0.0)\n"
        "\n"
        "    # Rebuild when a trade closed OR a new order filled (richer signal_meta).\n"
        "    if trades_m <= last_trades_m and orders_m <= last_orders_m:\n"
        "        # Source files unchanged since the last rebuild — return cached.\n"
        "        cached = read_json_state(\"trade_log.json\", default=[]) or []\n"
        "        return cached\n"
        "\n"
        "    try:\n"
        "        payload = build_log(config, days=30, log=logger)\n"
        "        write_json_state(\"trade_log.json\", payload)\n"
        "        # Side-car: mtimes persisted in a separate DICT file so the LIST\n"
        "        # shape of trade_log.json doesn't collapse the rebuild-skip check.\n"
        "        write_json_state(\"trade_log_meta.json\", {\n"
        "            \"paper_trades_mtime\": trades_m,\n"
        "            \"paper_orders_mtime\": orders_m,\n"
        "            \"ts\": __import__(\"datetime\").datetime.utcnow().isoformat() + \"Z\",\n"
        "        })\n"
    )
    if old not in src:
        print("  WARN: old pattern not found; skipping")
        return 1
    patched = src.replace(old, new, 1)
    # Also fix the tail of run() — the `return prev or {}` after exception.
    tail_old = "        logger.error(\"Trade log rebuild failed: %s\", exc)\n        return prev or {}"
    tail_new = "        logger.error(\"Trade log rebuild failed: %s\", exc)\n        return read_json_state(\"trade_log.json\", default=[]) or []"
    if tail_old in patched:
        patched = patched.replace(tail_old, tail_new, 1)
        print("  TAIL: replaced 'return prev or {}' with cached-list fallback")
    # Update the success log line — payload has no .get("total") anymore (it's a LIST).
    log_old = (
        "        logger.info(\n"
        "            \"Trade log rebuilt: %d trades (paper_trades mtime %.0f, orders mtime %.0f).\",\n"
        "            payload.get(\"total\", 0),\n"
        "            trades_m,\n"
        "            orders_m,\n"
        "        )"
    )
    log_new = (
        "        n = len(payload) if isinstance(payload, list) else payload.get(\"total\", 0)\n"
        "        logger.info(\n"
        "            \"Trade log rebuilt: %d trades (paper_trades mtime %.0f, orders mtime %.0f).\",\n"
        "            n,\n"
        "            trades_m,\n"
        "            orders_m,\n"
        "        )"
    )
    if log_old in patched:
        patched = patched.replace(log_old, log_new, 1)
        print("  LOG: replaced payload.get('total') with len(payload) for LIST payload")
    TARGET.write_text(patched, encoding="utf-8")
    print(f"  PATCH {TARGET}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
