"""
Splice a clean replacement for the broken `_build_cli_overrides` block in
dashboard/server.py. Identify the broken section by finding the comment header
"# ---- /api/cli_overrides ----" and the next def-after-def boundary, replace
that range with the verified-clean source from tmp/_cli_overrides_clean.py.
"""
import re
import sys
import os
import ast

sys.stdout.reconfigure(encoding="utf-8")

agent_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
project_root = os.path.dirname(agent_root) if os.path.basename(agent_root).startswith("tmp") else agent_root
# tmp/_splice_cli_overrides.py lives under <project>/tmp/, so project root is parent
project_root = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(project_root, "mt5_quant_agent")):
    new_root = os.path.dirname(project_root)
    if new_root == project_root:
        print(f"FATAL: cannot find mt5_quant_agent/ starting from {__file__}")
        sys.exit(2)
    project_root = new_root

p = os.path.join(project_root, "mt5_quant_agent", "dashboard", "server.py")
clean_path = os.path.join(project_root, "tmp", "_cli_overrides_clean.py")
smoke_path = os.path.join(project_root, "tmp", "_smoke_cli_overrides.py")

if not os.path.isfile(p):
    print(f"FATAL: dashboard/server.py not found at {p}")
    sys.exit(2)
if not os.path.isfile(clean_path):
    print(f"FATAL: clean source not found at {clean_path}")
    sys.exit(2)

src = open(p, "r", encoding="utf-8").read()
clean_src = open(clean_path, "r", encoding="utf-8").read()
print(f"server.py size: {len(src)} bytes")

# Extract clean text between BEGIN/END markers
m = re.search(
    r"# === BEGIN BLOCK:.*?# === END BLOCK ===",
    clean_src,
    re.DOTALL,
)
if not m:
    print("FATAL: BEGIN/END markers missing from clean source")
    sys.exit(2)
clean = m.group(0)
# Drop the markers themselves; keep the leading '# ---- /api/cli_overrides ---' heading
clean = re.sub(
    r"^# === BEGIN BLOCK:.*?# === END BLOCK ===\n?",
    "",
    clean,
    flags=re.MULTILINE,
)
# If the markers left leading/trailing whitespace issues, strip them
clean = clean.strip("\n")
print(f"clean section: {len(clean)} chars; first 80: {clean[:80]!r}")

# Locate the broken section in server.py.
# Start: the first line beginning with `# ---- /api/cli_overrides `
start_re = re.compile(r"^# ---- /api/cli_overrides ", re.MULTILINE)
m_start = start_re.search(src)
if not m_start:
    # Maybe the heading has em-dash or different spacing. Try a wider net.
    m_start = re.search(r"^# ---- /api/cli_overrides", src, re.MULTILINE)
if not m_start:
    print("FATAL: could not locate '# ---- /api/cli_overrides' section header in server.py")
    sys.exit(2)
start = m_start.start()
print(f"start at offset {start}; context: {src[start:start+120]!r}")

# End: scan forward to find a top-level definition/header boundary AFTER _build_cli_overrides
# Find `def _build_cli_overrides`
def_idx = src.index("def _build_cli_overrides", start)
# Scan forward
end = None
i = def_idx
# We need to find the END of the `_build_cli_overrides` function body. Easiest:
# locate the FIRST `# === ` comment-line OR a top-level `def ` AFTER _build_cli_overrides
# that is NOT another nested def. Top-level def-start lines have ZERO leading whitespace
# before `def `.
while i < len(src):
    # Look at next anchor: either `\ndef ` with this line starting at column 0
    if src[i:i+5] == "\ndef ":
        end = i + 1  # include the leading newline in the slice we replace
        # Verify this def isn't the same function (recursion impossible)
        candidate_def_start = i + 1
        candidate_def = src[candidate_def_start:].split("\n", 1)[0].strip()
        if candidate_def.startswith("def _build_cli_overrides"):
            # not the end, skip past
            i += 5
            continue
        end = i + 1
        break
    if src[i:i+8] == "\n# === " or src[i:i+8] == "\n# ---- ":
        end = i + 1
        break
    i += 1
if end is None:
    end = len(src)
print(f"end at offset {end}; context: {src[end:end+80]!r}")
print(f"replacing {end - start} bytes with {len(clean)} bytes of clean source")

new_src = src[:start] + clean + "\n\n" + src[end:].lstrip("\n")
open(p, "w", encoding="utf-8").write(new_src)
print(f"WROTE {len(new_src)} bytes to {p}")

# Verify parse
try:
    ast.parse(new_src)
    print("AST PARSE: OK")
except (SyntaxError, IndentationError) as exc:
    print(f"AST PARSE: FAILED at line {exc.lineno}: {exc.msg}")
    print(f"   text: {exc.text!r}")
    lines = new_src.splitlines()
    ln = exc.lineno - 1
    for j in range(max(0, ln - 2), min(len(lines), ln + 4)):
        marker = " <<<" if j == ln else ""
        print(f"   {j+1:>5}: {lines[j]}{marker}")
    sys.exit(1)

# Run smoke test
print("---SMOKE---")
import subprocess
proc = subprocess.run(
    ["python", smoke_path],
    cwd=project_root + "/mt5_quant_agent",
    capture_output=True,
    text=True,
    encoding="utf-8",
    timeout=120,
)
print("STDOUT:")
print(proc.stdout)
print("STDERR:")
print(proc.stderr)
print(f"EXIT: {proc.returncode}")
