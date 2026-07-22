"""Probe the dashboard HTML for natural insertion point + theme palette."""
import re
from pathlib import Path
src = Path("mt5_quant_agent/dashboard/index.html").read_text(encoding="utf-8")
lines = src.splitlines()
print(f"TOTAL LINES: {len(lines)}")
print()
print("--- tile-section keywords ---")
seen_lines = set()
for kw in ['Learning Loop', 'Living Parameters', 'cli_overrides', 'Profit Quality',
           'Payoff Paradox', 'payoff_paradox', 'cli_overrides_grid',
           'living_params', 'dashboard-tile', 'data-tile',
           'config_overrides', 'config-source', 'configTree',
           'cli-overrides', 'Matrix', 'matrix']:
    for i, l in enumerate(lines, start=1):
        if kw.lower() in l.lower() and i not in seen_lines:
            seen_lines.add(i)
            print(f"[{i}] kw={kw!r}: {l[:130]!r}")
print()
print("--- look for various tile/grid structures ---")
# Find typical tile wrapper patterns
for pat in [r'class=["\']tile', r'id=["\']tile', r'class="tile-grid"',
            r'class="matrix"', r'cliOverrides', r'living',
            r'data-target=["\'/api/living_params', r'data-target=["\'/api/cli_overrides']:
    found = re.findall(pat, src, re.IGNORECASE)
    if found:
        print(f"  PATTERN {pat!r}: {len(found)} occurrences")
print()
print("--- :root theme vars ---")
m = re.search(r":root\s*\{([^}]+)\}", src)
if m:
    print(m.group(0))
print()
print("--- tile-list outline (search for 'tile' occurrences) ---")
for i, l in enumerate(lines, start=1):
    if 'tile' in l.lower() and ('id=' in l or 'class=' in l):
        print(f"[{i}]: {l[:140]!r}")
