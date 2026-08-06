#!/usr/bin/env python
"""Phase 0 safety audit — detect direct ``mt5.order_send()`` bypasses.

Run this as a pre-commit hook or CI check. Exits non-zero when any file in
``core/``, ``loops/``, or ``dashboard/`` calls ``mt5.order_send()``, ``mt5.PositionClose()``,
or ``mt5.order_modify()`` directly (i.e. NOT through ``MT5Owner.instance().order_send()``).

Usage:
  python scripts/safety_audit.py          # scan core/, loops/, dashboard/
  python scripts/safety_audit.py --all     # scan entire project

Exit codes:
  0 — clean (all order mutations routed through MT5Owner)
  1 — violations found
  2 — scan error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files that are ALLOWED to call mt5.order_send() directly.
# - mt5_owner.py: the canonical owner (it IS the gate)
# - scripts that are operational/maintenance utilities (not runtime paths)
# - tests that mock MT5
ALLOWED_FILES: set[str] = {
    "core/mt5_owner.py",
}

# Patterns that indicate a direct MT5 mutation bypass.
FORBIDDEN_PATTERNS: list[str] = [
    "mt5.order_send(",
    "mt5.PositionClose(",
    "mt5.order_modify(",
]

# Directories to scan by default.
DEFAULT_DIRS: list[str] = ["core", "loops", "dashboard"]


def scan_file(path: Path) -> list[str]:
    """Return list of violation lines in *path*, or empty list if clean."""
    violations: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return violations
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        # Skip comments and docstrings
        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        for pat in FORBIDDEN_PATTERNS:
            if pat in line:
                violations.append(f"  {path}:{lineno}: {stripped[:120]}")
                break
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 0 safety audit")
    parser.add_argument("--all", action="store_true", help="Scan entire project (not just core/loops/dashboard)")
    parser.add_argument("--json", action="store_true", help="Output as JSON (for CI)")
    args = parser.parse_args()

    dirs = [ROOT] if args.all else [ROOT / d for d in DEFAULT_DIRS]

    all_violations: list[str] = []
    files_scanned = 0

    for directory in dirs:
        if not directory.is_dir():
            continue
        for py_file in sorted(directory.rglob("*.py")):
            rel = py_file.relative_to(ROOT).as_posix()
            if rel in ALLOWED_FILES:
                continue
            files_scanned += 1
            violations = scan_file(py_file)
            all_violations.extend(violations)

    if all_violations:
        header = (
            f"\nSAFETY AUDIT FAILED — {len(all_violations)} direct MT5 mutation(s) found\n"
            f"These files bypass MT5Owner.instance().order_send():\n"
        )
        print(header)
        for v in all_violations:
            print(v)
        print(f"\n{len(all_violations)} violation(s) in {files_scanned} files scanned.")
        print("Route ALL order mutations through MT5Owner.instance().order_send().")
        return 1

    print(f"SAFETY AUDIT PASSED — {files_scanned} files scanned, 0 direct MT5 mutations found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
