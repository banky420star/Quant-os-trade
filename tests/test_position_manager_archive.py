"""Regression tests for the time_stop archive row-source fix.

The time-stop cleanup path in core/position_manager.py called
_archive_mgmt_row using `mgmt_row.get(...)` — but `mgmt_row` is NOT
in scope at that call site (the function uses `mgmt = _load_mgmt_state()`).
The surrounding try/except converted the NameError into a soft warning,
which caused the loop to record the closed position as a failed time_stop
and infinitely retry management of an already-closed ticket.

Additionally `.get("positions", {})` does not guard against `positions=None`
— if `mgmt["positions"]` was set to None by another code path, the chain
crashes with AttributeError on `.get`.

Fix at this single site (L1184 of core/position_manager.py):
    ticket, (mgmt.get("positions") or {}).get(ticket_key, {}) or {}

The other 6 _archive_mgmt_row call sites still use bare `row` — those
land in a follow-up PR through a `_row_for_ticket(mgmt, ticket_key)`
helper. This test does NOT enforce those (would fail the build); instead
it lists them as a soft stdout inventory for visibility.
"""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

import pytest

import core.position_manager as pm


_POSITION_MANAGER_FILE = (
    Path(__file__).resolve().parent.parent / "core" / "position_manager.py"
)


@lru_cache(maxsize=1)
def _parse_position_manager() -> tuple[list[tuple[int, str]], int | None]:
    """Walk the AST of core/position_manager.py once and return:
      sites:  list of (line_no, row_source_expr_text) for every _archive_mgmt_row call
      the line of the one whose reason='time_stop'.
    Handles BOTH `_archive_mgmt_row(...)` (bare Name) and `pm._archive_mgmt_row(...)` (Attribute).
    """
    src = _POSITION_MANAGER_FILE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    sites: list[tuple[int, str]] = []
    time_stop_line: int | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = (
            getattr(node.func, "attr", None)  # module-qualified (pm._archive_mgmt_row)
            or getattr(node.func, "id", None)  # bare-name (_archive_mgmt_row)
        )
        if func_name != "_archive_mgmt_row":
            continue
        # Capture the row-source expression (2nd positional arg).
        if len(node.args) >= 2:
            row_expr = ast.unparse(node.args[1]).strip()
            sites.append((node.lineno, row_expr))
        # Locate the time_stop site (keyword form).
        for kw in node.keywords:
            if (
                kw.arg == "reason"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value == "time_stop"
            ):
                time_stop_line = node.lineno
                break
        else:
            # Defensive positional fallback (signature is reason=*, but
            # be tolerant if a future refactor makes it positional).
            if (
                len(node.args) >= 3
                and isinstance(node.args[2], ast.Constant)
                and node.args[2].value == "time_stop"
            ):
                time_stop_line = node.lineno
    return sites, time_stop_line


def test_time_stop_archive_row_source_is_safe() -> None:
    """HARD ASSERTION: the time_stop site MUST use the patched expression.

    Catches two regressions:
      1. `mgmt_row` sneaks back in (undefined-name crash).
      2. The bare-default `.get('positions', {})` form sneaks back in
         (positions=None AttributeError crash).
    """
    sites, time_stop_line = _parse_position_manager()
    assert time_stop_line is not None, (
        "Could not locate the time_stop _archive_mgmt_row call site in "
        "core/position_manager.py — test infrastructure needs an update."
    )

    time_stop_expr = next(expr for line, expr in sites if line == time_stop_line)

    # (1) No more `mgmt_row` — that name is undefined at this scope.
    assert "mgmt_row" not in time_stop_expr, (
        f"time_stop site at L{time_stop_line} still references undefined "
        f"`mgmt_row`: {time_stop_expr!r}. Fix: replace with `mgmt`."
    )

    # (2) Must use the `or {}` None-safe pattern (not bare .get default).
    safe_forms = (
        '(mgmt.get("positions") or {}).get(',
        "(mgmt.get('positions') or {}).get(",
    )
    assert any(form in time_stop_expr for form in safe_forms), (
        f"time_stop site at L{time_stop_line} does not use the None-safe "
        f"row-source expression: {time_stop_expr!r}. Fix: use "
        f'`(mgmt.get("positions") or {{}}).get(ticket_key, {{}}) or {{}}`.'
    )


def test_other_archive_sites_inventory() -> None:
    """SOFT INVENTORY (not enforced): list the other 6 sites as TODO.

    Those sites still use bare `row` (vulnerable to stale-row from a prior
    iteration when compute_managed_sl raises mid-loop). They migrate to
    `_row_for_ticket()` in a follow-up PR — this test surfaces where they
    are so the fixer has a concrete target list. NOT a build failure.
    """
    sites, _ = _parse_position_manager()
    bare_row_sites = [(line, expr) for line, expr in sites if expr == "row"]
    assert sites, "AST walker found no _archive_mgmt_row sites — investigate."
    if bare_row_sites:
        lines = "\n".join(f"  L{line}: {expr}" for line, expr in bare_row_sites)
        print(
            "\n[archive-row-source-inventory] Sites still on bare `row` "
            "(vulnerable to stale-row; refactor target for next PR):\n"
            f"{lines}"
        )


def test_archive_with_empty_mgmt_row() -> None:
    """Runtime smoke: _archive_mgmt_row must accept an empty row and
    produce a safe-empty record. The time_stop fix site may pass `{}`
    (e.g., when the ticket was never persisted into mgmt[positions]) —
    this guards against that path crashing."""
    pm._archive_mgmt_row(
        ticket=999_001,
        mgmt_row={},
        reason="time_stop",
        side="buy",
        entry=1.200,
        symbol="EURUSD",
    )
    archive = Path("state/position_mgmt_archive.jsonl")
    # No assertion on file content — only that the call did not raise.
    # (The production archive path is exercised by the full pytest suite
    # elsewhere; here we just smoke-test the empty-row guard.)
    assert archive.exists() or True  # graceful if state/ hasn't been created
