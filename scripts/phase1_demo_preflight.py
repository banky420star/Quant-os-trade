"""Fail-closed Phase 1 preflight for fresh-market demo validation.

The script never enables execution. In normal mode it certifies that the
single-symbol demo runtime is fresh and safely UNARMED. In --armed mode it
verifies that an operator has independently enabled the existing MT5 authority
gates for a demo-only routing rehearsal and supplied an exact account/commit
confirmation. Real accounts are always rejected.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import load_config, read_json_state


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return ""


def _iso_epoch(value: Any) -> float:
    if not isinstance(value, str) or not value.strip():
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def evaluate_preflight(
    config: dict[str, Any],
    account: dict[str, Any],
    market_feed: dict[str, Any],
    m1_state: dict[str, Any],
    *,
    expected_account: int,
    expected_commit: str,
    expected_branch: str,
    actual_commit: str,
    branch: str,
    dirty: bool,
    armed: bool,
    confirmation: str,
    kill_switch: dict[str, Any] | None = None,
    now_epoch: float | None = None,
) -> list[Check]:
    execution = config.get("execution") or {}
    mt5_cfg = config.get("mt5") or {}
    fast = config.get("fast_mode") or {}
    learning = config.get("learning") or {}
    trading = config.get("trading") or {}
    m1_cfg = config.get("m1_structure") or {}
    decisions = m1_state.get("decisions") or {}
    xau = decisions.get("XAUUSDm") or {}
    kill = kill_switch or {}

    login = int(account.get("login") or 0)
    runtime_mode = str(account.get("account_mode") or "").lower()
    phrase = f"DEMO {expected_account} {expected_commit}"

    now = float(now_epoch if now_epoch is not None else time.time())
    decision_epoch = _iso_epoch(xau.get("updated_at"))
    decision_age = max(0.0, now - decision_epoch) if decision_epoch > 0 else 999999.0
    loop_interval = float(m1_cfg.get("loop_interval_seconds") or 10.0)
    # A fresh market bar inside a frozen decision file is not fresh evidence.
    # Allow three service intervals for Windows scheduling jitter; beyond that
    # the shadow decision itself is stale and Phase 1 must fail closed.
    max_decision_age = max(30.0, loop_interval * 3.0)

    checks = [
        Check("profile", config.get("active_profile") == "phase1-demo", f"active={config.get('active_profile')!r}"),
        Check("demo_account_config", mt5_cfg.get("account_mode") == "demo", f"configured={mt5_cfg.get('account_mode')!r}"),
        Check("demo_account_runtime", runtime_mode == "demo", f"runtime={runtime_mode!r}"),
        Check("real_account_rejected", runtime_mode != "real", f"runtime={runtime_mode!r}"),
        Check("account_connected", account.get("connected") is True, f"connected={account.get('connected')!r}"),
        Check("expected_account", login == expected_account and expected_account > 0, f"runtime={login} expected={expected_account}"),
        Check("kill_switch_clear", kill.get("kill_switch") is not True, f"kill_switch={kill.get('kill_switch')!r}"),
        Check("single_symbol", list(mt5_cfg.get("symbols") or []) == ["XAUUSDm"], f"symbols={mt5_cfg.get('symbols')!r}"),
        Check("one_position_max", int(trading.get("max_open_per_symbol") or 0) == 1, f"max_open_per_symbol={trading.get('max_open_per_symbol')!r}"),
        Check("no_pyramiding", trading.get("allow_pyramiding") is False, f"allow_pyramiding={trading.get('allow_pyramiding')!r}"),
        Check("fast_live_off", fast.get("live_enabled") is False, f"live_enabled={fast.get('live_enabled')!r}"),
        Check("learning_observe_only", learning.get("mode") == "observe_only", f"mode={learning.get('mode')!r}"),
        Check("feed_worker_alive", market_feed.get("worker_alive") is True, f"worker_alive={market_feed.get('worker_alive')!r}"),
        Check("feed_no_failures", int(market_feed.get("refresh_failures_consecutive") or 0) == 0, f"refresh_failures={market_feed.get('refresh_failures_consecutive')!r}"),
        Check("m1_present", bool(xau), "XAUUSDm decision present" if xau else "XAUUSDm decision missing"),
        Check("m1_decision_current", decision_age <= max_decision_age, f"decision_age={decision_age:.1f}s max={max_decision_age:.1f}s updated_at={xau.get('updated_at')!r}"),
        Check("m1_fresh", xau.get("data_fresh") is True and float(xau.get("data_age_seconds") or 999999) <= 90.0, f"fresh={xau.get('data_fresh')!r} age={xau.get('data_age_seconds')!r}"),
        Check("commit_match", bool(expected_commit) and actual_commit == expected_commit, f"actual={actual_commit!r} expected={expected_commit!r}"),
        Check("expected_branch", bool(expected_branch) and branch == expected_branch, f"actual={branch!r} expected={expected_branch!r}"),
        Check("clean_worktree", not dirty, f"dirty={dirty}"),
    ]

    authority_enabled = (
        execution.get("live_trading_enabled") is True
        and execution.get("mt5_trading_enabled") is True
        and execution.get("explicit_opt_in_danger_zone") is True
    )

    if armed:
        checks.extend([
            Check("demo_route_authority", authority_enabled, f"flags={execution}"),
            Check("operator_confirmation", confirmation == phrase, f"expected={phrase!r}"),
        ])
    else:
        checks.append(Check(
            "unarmed_by_default",
            not authority_enabled,
            "execution authority remains disabled" if not authority_enabled else "execution authority unexpectedly enabled",
        ))

    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-account", type=int, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-branch", required=True)
    parser.add_argument("--armed", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = load_config()
    account = read_json_state("account.json", default={}) or {}
    market_feed = read_json_state("market_data_feed.json", default={}) or {}
    m1_state = read_json_state("m1_structure_decisions.json", default={}) or {}
    kill_switch = read_json_state("kill_switch.json", default={}) or {}

    checks = evaluate_preflight(
        config,
        account,
        market_feed,
        m1_state,
        expected_account=args.expected_account,
        expected_commit=args.expected_commit,
        expected_branch=args.expected_branch,
        actual_commit=_git("rev-parse", "HEAD"),
        branch=_git("branch", "--show-current"),
        dirty=bool(_git("status", "--porcelain", "--untracked-files=no")),
        armed=args.armed,
        confirmation=os.environ.get("QUANT_DEMO_CONFIRM", ""),
        kill_switch=kill_switch,
    )
    passed = all(c.ok for c in checks)

    if args.json:
        print(json.dumps({"passed": passed, "armed": args.armed, "checks": [asdict(c) for c in checks]}, indent=2))
    else:
        for check in checks:
            print(f"{'PASS' if check.ok else 'FAIL'} | {check.name} | {check.detail}")
        print(f"RESULT | {'PASS' if passed else 'FAIL'} | armed={args.armed}")

    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
