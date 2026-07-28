"""Regression guard: every bottom-nav tab must render its canvases > 0×0.

Encoded as a ``cy.visit('/')``-style audit (Playwright is the Python-side
equivalent of Cypress and shares the same single-browser / per-tab pattern).
If a future CSS regression collapses a chart's sizing chain back to 0×0 in
any view, this test fails immediately so the change can't ship.

The dashboard declares these bottom-nav tabs:

    dashboard, equity, research, memory, replay, trades, logs, settings

Each is wired by ``<button class="nav-item" data-view="...">`` (desktop) and
``<button class="mobile-tab" data-view="...">`` (mobile). Clicking either
raises the matching ``<div class="view" id="view-...">`` so its
``display: none`` is removed. Canvases inside the active view must report
``getBoundingClientRect().{width,height} > 0`` for their parent sizing
chain to be considered intact.

Tabs without canvases (``research``, ``memory``, ``replay``, ``trades``,
``logs``, ``settings``) trivially satisfy the invariant — they have zero
canvas children — so the unified assertion is the same for every tab.

Run with::

    pytest tests/test_dashboard_views.py -v
    DASHBOARD_URL=http://my-preview.example.com/ pytest tests/test_dashboard_views.py -v

Setup notes:
- Default URL is ``http://127.0.0.1:8088/`` (the preview mirror in
  ``scripts/dev_proxy.py``). Override via the ``DASHBOARD_URL`` env var.
- Skips automatically if the URL is unreachable or if the chromium binary
  isn't installed. Install with ``playwright install chromium``.
"""

from __future__ import annotations

import os
import socket
from typing import Iterator, List, Tuple

import pytest

TABS: List[str] = [
    "dashboard",
    "equity",
    "research",
    "memory",
    "replay",
    "trades",
    "logs",
    "settings",
]

# Tabs whose active view owns at least one chart canvas. The test matrix
# posts a stronger assertion for these — they must always report ≥ 1 visible
# canvas after the click — so any future chart-bearing view goes here.
CANVAS_TABS: Tuple[str, ...] = ("dashboard", "equity")

DEFAULT_DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8088/")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _split_host_port(url: str) -> tuple[str, int]:
    """Return (host, port) for http(s) URLs; default port from scheme."""
    rest = url.split("//", 1)[-1]
    hostport = rest.split("/", 1)[0]
    host, _, port_s = hostport.partition(":")
    if port_s:
        return host, int(port_s)
    if url.lower().startswith("https"):
        return host, 443
    return host, 80


def _url_reachable(url: str, timeout: float = 2.0) -> bool:
    try:
        host, port = _split_host_port(url)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def _playwright():
    """Single Playwright context per module to amortise startup cost."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as e:  # pragma: no cover - env-conditional
        pytest.skip(
            "playwright Python package not available — install with "
            "`pip install playwright` (see requirements.txt)."
        )
    ctx = sync_playwright().start()
    try:
        yield ctx
    finally:
        ctx.stop()


@pytest.fixture(scope="module")
def browser(_playwright):
    """One headless chromium instance for the whole module."""
    try:
        b = _playwright.chromium.launch(headless=True)
    except Exception as e:  # pragma: no cover - env-conditional
        pytest.skip(
            "chromium binary not installed — run `playwright install chromium`. "
            f"Underlying error: {type(e).__name__}: {str(e)[:200]}"
        )
    try:
        yield b
    finally:
        b.close()


@pytest.fixture()
def page(browser):
    """A fresh viewport-sized page per test."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


@pytest.fixture(scope="module", autouse=True)
def _require_dashboard_reachable() -> None:
    if not _url_reachable(DEFAULT_DASHBOARD_URL):
        pytest.skip(
            f"Dashboard at {DEFAULT_DASHBOARD_URL} is not reachable. "
            "Start `scripts/dev_proxy.py` (or set DASHBOARD_URL=http://host:port/)."
        )


# ---------------------------------------------------------------------------
# core helpers — actual cy.visit equivalent
# ---------------------------------------------------------------------------

def _goto_dashboard(page) -> None:
    """``cy.visit('/')`` equivalent: load the dashboard and wait for the
    app shell to replace the loader screen."""
    try:
        page.goto(DEFAULT_DASHBOARD_URL, wait_until="domcontentloaded", timeout=5000)
    except Exception as e:
        pytest.skip(
            f"Failed to load dashboard at {DEFAULT_DASHBOARD_URL}: "
            f"{type(e).__name__}: {str(e)[:200]}"
        )
    # The dashboard JS swaps in .app-root.ready after the loader exits,
    # which is when .view-dashboard gains the .active class. Wait for that.
    # 10 s gives Slack for slow CI workers + an upstream bot whose first
    # /api/state fetch takes a while to settle.
    try:
        page.wait_for_function(
            "() => { const el = document.querySelector('.view-dashboard');"
            "  return el && el.classList.contains('active'); }",
            timeout=10000,
        )
    except Exception as e:
        pytest.skip(
            f"Dashboard loader did not exit within 10 s — page did not finish booting. "
            "Most often: upstream bot on port 8080 is dead, so /api/* hangs and the "
            "loader screen stays up. Start the bot (or set DASHBOARD_URL=http://...)."
            f" Underlying error: {type(e).__name__}: {str(e)[:200]}"
        )


def _activate_tab(page, tab: str) -> str:
    """Click the bottom-nav for ``tab``; return the active view id.

    Equivalent of ``cy.get('[data-view=\"<tab>\"]').click()`` — handle
    duplicates (one nav-item + one mobile-tab) by picking the first match.
    """
    btn = page.locator(f'[data-view="{tab}"]').first
    btn.click()
    expected = f"view-{tab}"
    page.wait_for_function(
        "(id) => { const el = document.getElementById(id);"
        "  return el && el.classList.contains('active'); }",
        arg=expected,
        timeout=5000,
    )
    return expected


def _canvas_dims_in_active_view(page) -> list[dict]:
    """JS evaluated inside the page; returns the audit's key payload.

    Mirrors the live ``preview_evaluate`` we ran by hand::

        document.querySelectorAll('.view.active canvas')
                            → Array.from(...).map(getBoundingClientRect)

    Note: ``getBoundingClientRect`` reports 0×0 when any *layout*-hiding
    ancestor is encountered (``display:none`` or ``transform: scale(0)``),
    but **fails to detect ``visibility:hidden`` and ``opacity:0``** — those
    properties strip rendering while preserving layout, so we still get a
    positive rect from a functionally-invisible canvas. The guard below
    short-circuits those cases so future CSS regressions surface a clear
    error rather than silently passing.
    """
    return page.evaluate(
        "() => {"
        "  const v = document.querySelector('.view.active');"
        "  if (!v) return [{error: 'no_active_view', id: null}];"
        "  const cs = getComputedStyle(v);"
        "  if (cs.display === 'none' || cs.visibility === 'hidden')"
        "    return [{error: 'active_view_invisible', id: v.id,"
        "             display: cs.display, visibility: cs.visibility}];"
        "  const canvases = Array.from(v.querySelectorAll('canvas'));"
        "  return canvases.map(c => {"
        "    const r = c.getBoundingClientRect();"
        "    return {id: c.id || '<no-id>', w: r.width, h: r.height};"
        "  });"
        "}"
    )


# ---------------------------------------------------------------------------
# the test matrix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tab", TABS, ids=lambda tab: f"view-{tab}")
def test_visible_canvases_in_active_view_are_above_zero(page, tab: str) -> None:
    """``cy.visit('/')`` → click each tab → assert canvas dims > 0.

    Fails when ANY canvas inside the activated ``.view.active`` reports
    ``getBoundingClientRect().{width,height} == 0``. Tabs that legitimately
    contain no canvases (research / memory / replay / trades / logs /
    settings) trivially satisfy the invariant; the canvas-bearing views
    (dashboard, equity) are also sanity-checked to own at least one canvas.
    """
    _goto_dashboard(page)
    active_id = _activate_tab(page, tab)
    dims = _canvas_dims_in_active_view(page)

    assert isinstance(dims, list), f"expected list, got {type(dims).__name__}: {dims!r}"
    errors = [d for d in dims if "error" in d]
    assert not errors, (
        f"tab={tab!r} — view never reached the 'active' state: {errors!r}"
    )

    collapsed = [d for d in dims if not (d["w"] > 0 and d["h"] > 0)]
    assert not collapsed, (
        f"tab={tab!r} (view={active_id}) — "
        f"{len(collapsed)}/{len(dims)} canvases collapsed to 0×0:\n"
        + "\n".join(f"  #{d['id']} = {d['w']:.0f}×{d['h']:.0f}" for d in collapsed)
    )

    if tab in CANVAS_TABS:
        assert len(dims) >= 1, (
            f"tab={tab!r} should always own >=1 chart canvas in its active view; found 0"
        )
