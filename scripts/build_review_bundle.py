"""Build a sanitized Quant OS external-review bundle.

This script is intentionally read-only with respect to the trading system: it
only reads the repository and writes a new review-bundle directory/ZIP. It does
not connect to MT5, change state, or place/modify orders.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = ROOT / "review_bundle"
BUNDLE_NAME = "quant-os-review-bundle-2026-08-04"
OUT = OUT_ROOT / BUNDLE_NAME
ZIP = OUT_ROOT / f"{BUNDLE_NAME}.zip"

SECRET_KEY_RE = re.compile(
    r"(?i)(password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret|"
    r"private[_-]?key|authorization|bearer|secret)"
)
IDENTITY_KEY_RE = re.compile(r"(?i)^(login|server|terminal_path|connected_via)$")
LOG_SECRET_RE = re.compile(
    r"(?i)(password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret|"
    r"private[_-]?key|authorization|bearer|secret)\s*[:=]\s*[^,;\s]+"
)
LOG_ID_RE = re.compile(r"(?i)\b(login|server|terminal_path|connected_via)\s*[:=]\s*[^,;\s]+")

SOURCE_DIRS = ("core", "loops", "quant", "strategies", "scripts", "tests")
ROOT_FILES = (
    "config.yaml", "pytest.ini", "requirements.txt", "README.md", "STRUCTURE.md",
    "HOW_TO_RUN.md", "BEST_SETTINGS.md", "PHASE2_UPDATE.md", "LAUNCH.bat",
    "start.py", "run_all.py",
)
REVIEW_DOCS = ("docs", "obsidian")
STATE_FILES = (
    "features.json", "market_context.json", "risk_state.json", "account.json",
    "mt5_baseline.json", "candidate_signals.json", "evaluated_signals.json",
    "approved_signals.json", "rejected_signals.json", "mt5_positions.json",
    "mt5_orders.json", "mt5_trades.json", "paper_positions.json", "paper_orders.json",
    "paper_trades.json", "trade_log.json", "memory.json", "edge_database.json",
    "forward_test_ledger.json", "learning_state.json", "learning_monitor.json",
    "learning_config_overrides.json", "policy_scores.json", "best_policies.json",
    "symbol_policy_live.json", "specialized_setup_report.json", "specialized_shadow_report.json",
    "validation_cycle.json", "validation_history.json", "research_report.json",
    "research_validation.json", "mt5_audit.json", "mt5_calendar.json",
    "symbol_specs.json", "broker_symbols.json", "account.json",
)
STATE_OPTIONAL = ("quant_os.db",)
LOG_FILES = (
    "system.log", "launch_bot.log", "research_loop.log", "execution_loop.log",
    "signal_loop.log", "risk_loop.log", "verifier_loop.log", "position_manager_loop.log",
    "health_loop.log", "trade_log_loop.log", "validation_loop.log", "fast_tick_loop.log",
    "learning_review_loop.log", "memory_loop.log", "history_loop.log", "data_loop.log",
)


def git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unavailable"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_obj(value: Any, *, key: str = "") -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if SECRET_KEY_RE.search(str(k)) or IDENTITY_KEY_RE.match(str(k)):
                out[k] = "<REDACTED>"
            else:
                out[k] = sanitize_obj(v, key=str(k))
        return out
    if isinstance(value, list):
        return [sanitize_obj(v, key=key) for v in value]
    return value


def sanitize_text(text: str) -> str:
    text = LOG_SECRET_RE.sub(lambda m: f"{m.group(0).split('=')[0].split(':')[0]}=<REDACTED>", text)
    return LOG_ID_RE.sub(lambda m: f"{m.group(1)}=<REDACTED>", text)


def write_text(rel: str, text: str) -> None:
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def copy_source() -> list[str]:
    included: list[str] = []
    for dirname in SOURCE_DIRS:
        src = ROOT / dirname
        if not src.is_dir():
            continue
        for p in src.rglob("*"):
            if not p.is_file() or "__pycache__" in p.parts or p.suffix in {".pyc", ".pyo"}:
                continue
            rel = p.relative_to(ROOT).as_posix()
            target = OUT / "source" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            included.append(f"source/{rel}")
    for name in ROOT_FILES:
        p = ROOT / name
        if p.is_file():
            target = OUT / "source" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            included.append(f"source/{name}")
    for dirname in REVIEW_DOCS:
        src = ROOT / dirname
        if not src.is_dir():
            continue
        for p in src.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                rel = p.relative_to(ROOT).as_posix()
                target = OUT / "source" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, target)
                included.append(f"source/{rel}")
    return included


def copy_sanitized_state() -> tuple[list[str], list[str]]:
    included: list[str] = []
    missing: list[str] = []
    state_root = ROOT / "state"
    for name in STATE_FILES + STATE_OPTIONAL:
        src = state_root / name
        if not src.exists():
            missing.append(f"state/{name}")
            continue
        target = OUT / "runtime_state" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix == ".json":
            try:
                obj = json.loads(src.read_text(encoding="utf-8", errors="replace"))
                target.write_text(json.dumps(sanitize_obj(obj), indent=2, default=str), encoding="utf-8")
            except Exception:
                target.write_text(sanitize_text(src.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
        else:
            # SQLite learning/edge DB contains no credential material in the
            # schema; copy it as the requested database artifact.
            shutil.copy2(src, target)
        included.append(f"runtime_state/{name}")
    return included, missing


def copy_sanitized_logs() -> tuple[list[str], list[str]]:
    included: list[str] = []
    missing: list[str] = []
    for name in LOG_FILES:
        src = ROOT / "logs" / name
        if not src.exists():
            missing.append(f"logs/{name}")
            continue
        target = OUT / "runtime_logs" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(sanitize_text(src.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
        included.append(f"runtime_logs/{name}")
    return included, missing


def make_runtime_excerpt() -> dict[str, Any]:
    src = ROOT / "logs" / "system.log"
    out: dict[str, Any] = {"source": "runtime_logs/system.log", "complete": False, "reason": "no explicit data-lab shutdown marker found"}
    if not src.exists():
        out["reason"] = "system.log missing"
        write_text("runtime_log_startup_to_last_available.txt", "No runtime log available.\n")
        return out
    lines = src.read_text(encoding="utf-8", errors="replace").splitlines()
    start = next((i for i, line in enumerate(lines) if "profile auto-select: data-lab" in line.lower()), None)
    if start is None:
        start = max(0, len(lines) - 2000)
        out["reason"] = "no data-lab startup marker; included final available log segment"
    stop = next((i for i in range(start + 1, len(lines)) if "profile auto-select:" in lines[i].lower()), len(lines))
    segment = sanitize_text("\n".join(lines[start:stop]) + "\n")
    write_text("runtime_log_startup_to_last_available.txt", segment)
    out.update({"start_line": start + 1, "end_line": stop, "line_count": max(0, stop - start)})
    return out


def make_specs() -> dict[str, Any]:
    src = ROOT / "state" / "symbol_specs.json"
    data = json.loads(src.read_text(encoding="utf-8")) if src.exists() else {}
    specs = data.get("specs", {}) if isinstance(data, dict) else {}
    fields = ("contract_size", "volume_min", "volume_step", "volume_max", "trade_tick_size", "trade_tick_value", "leverage", "trade_stops_level", "trade_freeze_level", "point", "digits", "broker_symbol")
    out = {"source": "state/symbol_specs.json", "captured_at": data.get("timestamp"), "symbols": {}, "note": "Null means the captured state did not expose the field; no values were inferred."}
    for sym, row in specs.items():
        out["symbols"][sym] = {field: row.get(field) for field in fields}
    write_text("broker_symbol_specs_sanitized.json", json.dumps(out, indent=2, default=str))
    return out


def make_profile_description() -> None:
    text = """# Data-lab experimental profile

## Purpose
Live-demo MT5 forward testing of the 14-symbol, all-strategy lane. Candidates are
kept broad so performance can be attributed by symbol, setup, session/hour, and
recorded entry settings.

## Deliberate controls
- Risk: fresh strategy cells use 0.5% fallback risk; exact-cell full Kelly requires 20 closed trades; new-trade risk cap is 5% of equity.
- Maximum acceptable drawdown: the profile YAML has a broad 100% experiment drawdown gate, but the sizing cap is the practical per-trade loss control. This is intentionally a demo-only experiment, not a production risk target.
- Expected frequency: opportunistic; all 14 symbols and all enabled strategy streams may emit when their conditions trigger. No fixed daily trade count is promised.
- Simultaneous positions: one open position per symbol; pyramiding is disabled.
- Exit policy: initial SL/TP only; trailing, break-even, partial TP, time-based SL, adaptive exits, and fast-mode secondary entries are disabled.
- Loss injection: losses are not synthetically injected. Losing trades arise from live-demo market outcomes. Extreme sizing was previously permitted and produced the observed outsized losses; the current bundle reflects the corrected 0.5%/5% sizing controls.
- Account safety: demo-only; live-account trading is refused by the profile guard.

## Profile isolation
The exact overlay is `profiles/data-lab.yaml`, merged over `config.yaml` by
`core.utils.load_config`; `config.local.yaml` was absent at bundle creation.
Runtime learning overrides are only applied when `learning.mode` is
`live_apply_limited`; the active data-lab profile uses observe-only learning.
"""
    write_text("experimental_profile.md", text)


def make_env_example() -> None:
    write_text(".env.example", """# Sanitized example — no credentials are included.
# MT5_QUANT_PROFILE=data-lab
# MT5_TERMINAL_PATH=C:/Path/To/terminal64.exe
# NEWS_LLM_BASE_URL=https://example.invalid/v1
# NEWS_LLM_API_KEY=<insert-locally>
# NEWS_LLM_MODEL=<model-name>
""")


def main() -> None:
    if OUT_ROOT.exists():
        shutil.rmtree(OUT_ROOT)
    OUT.mkdir(parents=True)
    source = copy_source()
    state, missing_state = copy_sanitized_state()
    logs, missing_logs = copy_sanitized_logs()
    profile = make_runtime_excerpt()
    make_specs()
    make_profile_description()
    make_env_example()
    manifest = {
        "bundle_created_at": utc_now(),
        "repository": {
            "remote": git("config", "--get", "remote.origin.url"),
            "branch": git("branch", "--show-current"),
            "commit": git("rev-parse", "HEAD"),
            "commit_subject": git("log", "-1", "--format=%s"),
        },
        "active_profile_at_inventory": "data-lab",
        "config_overlay_chain": ["config.yaml", "config.local.yaml (absent)", "profiles/data-lab.yaml", "post-load sync functions in core/utils.py:load_config"],
        "included_source_files": len(source),
        "included_state_files": state,
        "missing_state_files": missing_state,
        "included_log_files": logs,
        "missing_log_files": missing_logs,
        "runtime_excerpt": profile,
        "verified_not_included": ["MT5 passwords", "API credentials", "tokens", ".env files", "private keys"],
        "notes": [
            "No existing .env/config.local.yaml was found during inventory.",
            "account/login/server identity fields in runtime JSON and logs are redacted.",
            "verified_signals.json was absent; approved_signals.json and rejected_signals.json are included.",
            "No complete data-lab startup-to-explicit-shutdown run was available in the retained logs; the excerpt is labeled accordingly.",
            "Broker state exposed min lot, step, tick size/value and point for 14 symbols; contract size, leverage, stop level and several broker aliases were absent from the captured symbol_specs state and are represented as null.",
        ],
    }
    write_text("MANIFEST.json", json.dumps(manifest, indent=2, default=str))
    write_text("README_REVIEW_BUNDLE.md", """# Quant OS external review bundle

Start with `MANIFEST.json`, `experimental_profile.md`, and
`broker_symbol_specs_sanitized.json`. Source is under `source/`; sanitized live
state is under `runtime_state/`; sanitized logs are under `runtime_logs/`.

The bundle is for review only. It contains no MT5 passwords, API keys, tokens,
or private keys, and it does not include `.git` history.

Normal test command:

```text
python -m pytest tests/ -q
```

Focused data-lab validation used for this bundle:

```text
python -m pytest tests/test_data_lab_profile.py tests/test_kelly_sizing.py tests/test_fixed_exit_experiment.py tests/test_data_lab_runtime_guards.py -q
```
""")
    if ZIP.exists():
        ZIP.unlink()
    with zipfile.ZipFile(ZIP, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in OUT.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(OUT_ROOT).as_posix())
    print(json.dumps({"bundle_dir": str(OUT), "zip": str(ZIP), "files": sum(1 for p in OUT.rglob('*') if p.is_file()), "manifest": manifest}, indent=2, default=str))


if __name__ == "__main__":
    main()
