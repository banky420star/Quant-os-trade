"""Probe dashboard/index.html — find: tile template structure, existing fetchLivingParams implementation,
and the relevant CSS palette variables for chip colors."""
import re
from pathlib import Path
src = Path("mt5_quant_agent/dashboard/index.html").read_text(encoding="utf-8")
lines = src.splitlines()
print(f"TOTAL LINES: {len(lines)}")

print()
print("--- fetchLivingParams implementation ---")
m = re.search(r"(async function fetchLivingParams.*?^})", src, re.M | re.S)
if m:
    print(m.group(0))

print()
print("--- renderLivingParams / payday / tile-style references ---")
for kw in ["renderLivingParams", "renderLearningTimeline",
          "let _tlTimer", "let _lpTimer",
          "new learning tile", "tile-body", "tile-content",
          "tile-render", "class=\"tile\"", "class=\"card\"",
          "addChild("]:
    hits = [(i+1, l[:140]) for i, l in enumerate(lines) if kw in l]
    if hits:
        print(f"  KW {kw!r}: {len(hits)} hits")
        for i, l in hits[:3]:
            print(f"    [{i}] {l!r}")

print()
print("--- learning / tile markup blocks (search for class= or id=) ---")
count = 0
for i, l in enumerate(lines):
    if re.search(r'class\s*=\s*["\'][^"\']*(?i:tile|tl-|grid|card)', l):
        print(f"[{i+1}]: {l.strip()[:160]!r}")
        count += 1
        if count >= 40: break

print()
print("--- :root theme vars (palette) ---")
m = re.search(r":root\s*\{([^}]+)\}", src)
if m: print(m.group(0))
print()
m = re.search(r"--bg\s*:.*?--orange[^;]*;", src)
if m: print("palette:", m.group(0)[:500])
