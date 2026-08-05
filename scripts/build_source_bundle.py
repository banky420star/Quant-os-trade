"""Build a complete source-only MT5 Quant OS bundle.

The bundle includes every Python file and every launcher/support script needed
for source review or a clean checkout: core, loops, dashboards, scripts,
strategies, quant, profiles, tests, docs, and root launch/config files.
Runtime state, logs, market-history data, archives, caches, credentials, and
nested duplicate worktrees are deliberately excluded.
"""
from __future__ import annotations

import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
OUT_ROOT = ROOT / f"mt5_quant_os_source_bundle_{STAMP}"
ZIP_PATH = ROOT / f"mt5_quant_os_source_bundle_{STAMP}.zip"

SOURCE_DIRS = (
    "core",
    "loops",
    "dashboard",
    "scripts",
    "strategies",
    "quant",
    "profiles",
    "tests",
    "docs",
    "obsidian",
)
ROOT_FILES = (
    "config.yaml",
    "requirements.txt",
    "pytest.ini",
    "README.md",
    "LAUNCH.bat",
    "START_AGENT.bat",
    "start.py",
    "run_all.py",
    "start.bat",
    "start100.bat",
    "start30.bat",
    "start30-c2.bat",
    "start30-c3.bat",
    "start30-real.bat",
    "start_growth.bat",
    "run-live.bat",
    "run-live-auto.bat",
    "mt5_quant_os_icon.ico",
    ".gitignore",
)
EXCLUDED_DIRS = {
    ".git",
    ".agents",
    "__pycache__",
    "state",
    "logs",
    "data",
    "terminals",
    "tmp",
    "agent-tools",
    "review_bundle",
    "profile_kelly_review_20260805_164909",
    "MT5_Quant_Agent_P0_Repaired_2026-08-04",
    "mt5_quant_agent",
    "source_bundle",
    "mt5_quant_os_source_bundle_20260805_184336",
    "mt5_quant_os_source_bundle_20260805_184556",
}
# Prevent a rerun from copying an older generated package into a new package.
EXCLUDED_DIR_PREFIXES = ("mt5_quant_os_source_bundle_",)

EXCLUDED_NAMES = {
    ".env",
    ".env.local",
    "config.local.yaml",
    "credentials.json",
    "secrets.json",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".db", ".db-shm", ".db-wal", ".parquet", ".lock"}


def should_copy(path: Path) -> bool:
    if any(part in EXCLUDED_DIRS for part in path.parts):
        return False
    if any(part.startswith(prefix) for part in path.parts for prefix in EXCLUDED_DIR_PREFIXES):
        return False
    if path.name in EXCLUDED_NAMES or path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    return path.is_file()


def copy_one(src: Path, rel: Path, included: list[str]) -> None:
    if not should_copy(src):
        return
    target = OUT_ROOT / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    included.append(rel.as_posix())


def write_bundle_readme(included: list[str]) -> None:
    python_files = [p for p in included if p.endswith(".py")]
    script_files = [p for p in included if p.startswith("scripts/")]
    text = f"""# MT5 Quant OS complete source bundle

Canonical source copied from:
`C:\\Users\\Administrator\\Desktop\\new task`

This is a source/deployment bundle. It includes every Python file, all scripts,
launchers, profiles, dashboard files, strategies, quant modules, tests, and
operator documentation present in the canonical project at build time.

## Included

- `{len(python_files)}` Python files total
- `{len(script_files)}` files under `scripts/` (Python, BAT, and PowerShell)
- `core/`, `loops/`, `dashboard/`, `scripts/`, `strategies/`, `quant/`
- `profiles/`, `tests/`, `docs/`, and `obsidian/`
- Root launchers, configuration, requirements, README, and the Quant OS icon

## Start the full stack

Double-click `LAUNCH.bat`, or run from this folder:

```text
scripts\\select_mt5_python.bat
scripts\\launch_all.py
```

The launcher starts:

- `start.py` and the main dashboard on port 8080
- `scripts/nojs_dashboard.py` on port 8081
- `scripts/terminal_view_server.py` on port 8083

Stop the stack with:

```text
scripts\\kill_agent.bat
```

## Install dependencies

```text
python -m pip install -r requirements.txt
```

## Deliberately excluded

Runtime state, logs, live databases, market-history files, lock files, compiled
Python caches, archives, backup worktrees, `.env` files, local config overrides,
credentials, and tokens are not included. Copy those only deliberately when a
runtime migration is intended.
"""
    (OUT_ROOT / "BUNDLE_README.md").write_text(text, encoding="utf-8")


def main() -> None:
    if OUT_ROOT.exists():
        shutil.rmtree(OUT_ROOT)
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    OUT_ROOT.mkdir(parents=True)

    included: list[str] = []
    for dirname in SOURCE_DIRS:
        source = ROOT / dirname
        if not source.is_dir():
            continue
        for path in sorted(source.rglob("*")):
            if path.is_file():
                copy_one(path, path.relative_to(ROOT), included)

    for name in ROOT_FILES:
        path = ROOT / name
        if path.is_file():
            copy_one(path, Path(name), included)

    write_bundle_readme(included)
    included.append("BUNDLE_README.md")

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(ROOT),
        "bundle_directory": OUT_ROOT.name,
        "zip_file": ZIP_PATH.name,
        "file_count": len(included),
        "python_count": sum(p.endswith(".py") for p in included),
        "script_count": sum(p.startswith("scripts/") for p in included),
        "excluded_categories": [
            "runtime state and logs",
            "databases and market-history data",
            "compiled caches and lock files",
            "archives, backups, and duplicate worktrees",
            "credentials, .env files, and local config overrides",
        ],
    }
    included.append("BUNDLE_MANIFEST.json")
    manifest["file_count"] = len(included)
    manifest["python_count"] = sum(p.endswith(".py") for p in included)
    manifest["script_count"] = sum(p.startswith("scripts/") for p in included)
    manifest["included_top_level"] = sorted({p.split("/", 1)[0] for p in included})
    (OUT_ROOT / "BUNDLE_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    with zipfile.ZipFile(
        ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for path in sorted(OUT_ROOT.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(OUT_ROOT.parent).as_posix())

    print(json.dumps({
        "bundle_directory": str(OUT_ROOT),
        "zip": str(ZIP_PATH),
        "file_count": len(included),
        "python_count": sum(p.endswith(".py") for p in included),
        "script_count": sum(p.startswith("scripts/") for p in included),
        "top_level": manifest["included_top_level"],
    }, indent=2))


if __name__ == "__main__":
    main()
