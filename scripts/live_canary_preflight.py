"""Fail-closed readiness check for the real-account live canary.

This script NEVER enables execution. It only reports whether the current
runtime is safe to remain unarmed or, when invoked with --armed, whether an
operator has independently enabled every required authority flag and supplied
an exact account/commit confirmation.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, asdict
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


def _bool(value: Any) -> bool:
    return value is True


def evaluate_preflight(
    config: dict[str, Any],
    account: dict[str, Any],
    market_feed: dict[str, Any],
    m1_state: dict[str, Any],
    *,
    expected_account: int,
    expected_commit: str,
    actual_commit: str,
    branch: str,
    dirty: bool,
    armed: bool,
    confirmation: str,
) -> list[Check]:
    execution = config.get("execution") or {}
    mt5_cfg = config.get("mt5") or {}
    fast = config.get("fast_mode") or {}
    learning = config.get("learning") or {}
    trading = config.get("trading") or {}
    decisions = m1_state.get("decisions") or {}
    xau = decisions.get("XAUUSDm") or {}

    account_login = int(account.get("login") or 0)
    account_mode = str(account.get("account_mode") or "").lower()
    expected_phrase = f"LIVE {expected_account} {expected_commit}"

    checks = [
        Check("profile", config.get("active_profile") == "live-canary", f"active={config.get('active_profile')!r}"),
        Check("real_account_config", mt5_cfg.get("account_mode") == "real", f"configured={mt5_cfg.get('account_mode')!r}"),
        Check("real_account_runtime", account_mode == "real", f"runtime={account_mode!r}"),
        Check("expected_account", account_login == expected_account and expected_account > 0, f"runtime={account_login} expected={expected_account}"),
        Check("single_symbol", list(mt5_cfg.get("symbols") or []) == ["XAUUSDm"], f"symbols={mt5_cfg.get('symbols')!r}"),
        Check("one_position_max", int(trading.get("max_open_per_symbol") or 0) == 1, f"max_open_per_symbol={trading.get('max_open_per_symbol')!r}"),
        Check("no_pyramiding", trading.get("allow_pyramiding") is False, f"allow_pyramiding={trading.get('allow_pyramiding')!r}"),
        Check("fast_live_off", fast.get("live_enabled") is False, f"live_enabled={fast.get('live_enabled')!r}"),
        Check("learning_observe_only", learning.get("mode") == "observe_only", f"mode={learning.get('mode')!r}"),
        Check("feed_worker_alive", market_feed.get("worker_alive") is True, f"worker_alive={market_feed.get('worker_alive')!r}"),
        Check("m1_present", bool(xau), "XAUUSDm decision present" if xau else "XAUUSDm decision missing"),
        Check("m1_fresh", xau.get("data_fresh") is True and float(xau.get("data_age_seconds") or 999999) <= 90.0, f"fresh={xau.get('data_fresh')!r} age={xau.get('data_age_seconds')!r}"),
        Check("commit_match", bool(expected_commit) and actual_commit == expected_commit, f"actual={actual_commit!r} expected={expected_commit!r}"),
        Check("safety_branch", branch == "agent/live-canary-readiness", f"branch={branch!r}"),
        Check("clean_worktree", not dirty, f"dirty={dirty}"),
    ]

    authority_enabled = (
        _bool(execution.get("live_trading_enabled"))
        and _bool(execution.get("mt5_trading_enabled"))
        and _bool(execution.get("explicit_opt_in_danger_zone"))
    )

    if armed:
        checks.extend(
            [
                Check("live_authority", authority_enabled, f"flags={execution}"),
                Check("operator_confirmation", confirmation == expected_phrase, f"expected={expected_phrase!r}"),
            ]
        )
    else:
        checks.append(
            Check(
                "unarmed_by_default",
                not authority_enabled,
                "execution authority remains disabled" if not authority_enabled else "execution authority unexpectedly enabled",
            )
        )

    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-account", type=int, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--armed", action="store_true", help="Require all live authority gates and exact confirmation")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = load_config()
    account = read_json_state("account.json", default={}) or {}
    market_feed = read_json_state("market_data_feed.json", default={}) or {}
    m1_state = read_json_state("m1_structure_decisions.json", default={}) or {}
    actual_commit = _git("rev-parse", "HEAD")
    branch = _git("branch", "--show-current")
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    confirmation = os.environ.get("QUANT_LIVE_CONFIRM", "")

    checks = evaluate_preflight(
        config,
        account,
        market_feed,
        m1_state,
        expected_account=args.expected_account,
        expected_commit=args.expected_commit,
        actual_commit=actual_commit,
        branch=branch,
        dirty=dirty,
        armed=args.armed,
        confirmation=confirmation,
    )
    passed = all(check.ok for check in checks)

    if args.json:
        print(json.dumps({"passed": passed, "armed": args.armed, "checks": [asdict(c) for c in checks]}, indent=2))
    else:
        for check in checks:
            print(f"{'PASS' if check.ok else 'FAIL'} | {check.name} | {check.detail}")
        print(f"RESULT | {'PASS' if passed else 'FAIL'} | armed={args.armed}")

    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
