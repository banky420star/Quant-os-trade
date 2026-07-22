"""Smoke test for /api/cli_overrides endpoint.

Imports dashboard.server, calls the builder, and prints a verification
matrix: schema keys, instance counts, sample shadowed keys, line-number
provenance, hot-conflict count, and cache invariants.
"""
import json
import sys
from pathlib import Path

ROOT = Path(".").resolve()
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from dashboard.server import _build_cli_overrides  # noqa: E402


def _hr(title):
    print(f"\n--- {title} ---")


def main():
    out = _build_cli_overrides(force=True)

    _hr("Top-level schema")
    for k in sorted(out.keys()):
        v = out[k]
        if isinstance(v, list):
            print(f"  {k:<22} list[{len(v)}]")
        elif isinstance(v, dict):
            print(f"  {k:<22} dict[{len(v)} keys]")
        else:
            print(f"  {k:<22} {type(v).__name__}={v!r:.80}")

    _hr("Layers (input chain summary)")
    for layer in out.get("layers", []):
        line = f"  {layer['name']:<48}"
        line += f"  kc={layer.get('key_count', 0):>4}"
        line += f"  in_mem={layer.get('in_memory')}"
        line += f"  src={layer.get('source_path') or '-'}"
        line += f"  ts={layer.get('override_time') or '-'}"
        print(line)

    _hr("Summary block")
    print(json.dumps(out.get("summary", {}), indent=2))

    _hr("First 8 shadowed overrides (key + provenance)")
    shadowed = [o for o in out.get("overrides", []) if o.get("shadowed")]
    for o in shadowed[:8]:
        bv = o.get("base_value")
        fv = o.get("final_value")
        bv_str = json.dumps(bv)[:40] if bv is not None else "None"
        fv_str = json.dumps(fv)[:40] if fv is not None else "None"
        print(
            f"  {o['key']:<48}"
            f"  status={o.get('shadowed_status', '-'):<24}"
            f"  base={bv_str:<40}"
            f"  final={fv_str:<40}"
        )
        print(
            f"    winning={o.get('winning_layer', '-')}"
            f"  line={o.get('line_number', '-')}"
            f"  src={o.get('source_path', '-')}"
        )
        h_layers = " -> ".join(
            h["layer"] for h in o.get("history", [])
        )
        print(f"    history: {h_layers}")

    _hr("Hot-conflict keys (>=3 distinct YAML values)")
    hot = out.get("hot_conflicts", [])
    print(f"  count={len(hot)}")
    for h in hot[:5]:
        distinct = set()
        for entry in h.get("history", []):
            if entry.get("in_memory"):
                continue
            try:
                distinct.add(repr(entry["value"]))
            except Exception:
                distinct.add(str(entry["value"]))
        print(
            f"  {h['key']:<48}"
            f"  distinct={len(distinct)}"
            f"  layers={sum(1 for e in h.get('history',[]) if not e.get('in_memory'))}"
        )

    _hr("by_layer rollup")
    bl = out.get("by_layer", {})
    for ln, keys in sorted(bl.items(), key=lambda x: -len(x[1])):
        print(f"  {ln:<48}  {len(keys)} keys")

    # ------------------------------------------------------------
    # Probes for invariants
    # ------------------------------------------------------------
    _hr("Invariant probes")
    # 1. Every override entry has a 'key'
    miss_key = sum(1 for o in out.get("overrides", []) if not o.get("key"))
    print(f"  overrides missing 'key': {miss_key}  (expect 0)")

    # 2. At least one shadowed entry has a non-null line_number
    line_proven = sum(
        1 for o in out.get("overrides", [])
        if o.get("shadowed") and o.get("line_number") is not None
    )
    print(f"  shadowed overrides w/ line_number: {line_proven}  (expect >= 1)")

    # 3. config.yaml MUST appear in layers as 'base'
    has_base = any(l.get("name") == "base" for l in out.get("layers", []))
    print(f"  'base' layer present: {has_base}  (expect True)")

    # 4. Local layer IF config.local.yaml exists
    local_path = ROOT / "config.local.yaml"
    has_local = any(l.get("name") == "local" for l in out.get("layers", []))
    print(
        f"  'local' layer present: {has_local}"
        f"  (config.local.yaml exists={local_path.is_file()})"
    )

    # 5. Cache test: second call within TTL returns same payload id
    out2 = _build_cli_overrides()
    print(
        f"  cache hit (within 5s): {out.get('updated_at') == out2.get('updated_at')}"
    )
    out3 = _build_cli_overrides(force=True)
    print(
        f"  force=True bypasses cache: {out.get('updated_at') != out3.get('updated_at')}"
    )

    # 6. Spot-check: 'signals.min_risk_reward' is one of the most disputed
    target_key = "signals.min_risk_reward"
    target_overrides = [
        o for o in out.get("overrides", []) if o.get("key") == target_key
    ]
    if target_overrides:
        o = target_overrides[0]
        print(f"\n  Spot-check '{target_key}':")
        print(
            f"    base={o.get('base_value')}  final={o.get('final_value')}"
            f"  status={o.get('shadowed_status')}"
        )
        for h in o.get("history", []):
            print(
                f"    - layer={h.get('layer')}  value={h.get('value')}  "
                f"line={h.get('line_number')}  src={h.get('source_path')}"
            )


if __name__ == "__main__":
    main()
