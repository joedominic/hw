"""
Playwright session wrapper for the Apply Agent.

Guarantees the safety properties the plan requires:
- A fresh, isolated ``browser.new_context()`` per task (no shared profile dir),
  so concurrent workers never leak cookies/sessions into one another.
- A short default page timeout (30s) so a hung page fails fast instead of
  occupying a scarce browser worker slot.
- Best-effort stealth via ``playwright-stealth`` when installed.
- Deterministic teardown (context + browser closed in ``finally``) plus an
  orphan-Chromium sweep, so a crashed step does not leak processes.

All heavy imports are lazy so the module is importable without Playwright.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any, Iterator, Optional

logger = logging.getLogger("huey")

DEFAULT_PAGE_TIMEOUT_MS = 30_000


def apply_browser_headless() -> bool:
    """Return True unless env or profile settings request a visible browser window."""
    from django.conf import settings as django_settings

    from ..models import AppAutomationSettings

    if not getattr(django_settings, "APPLY_BROWSER_HEADLESS", True):
        return False
    solo = AppAutomationSettings.get_solo()
    if getattr(solo, "apply_browser_show_window", False):
        return False
    return True


class BrowserUnavailable(RuntimeError):
    """Raised when Playwright (or its browser binary) is not installed."""


@contextlib.contextmanager
def browser_session(
    *,
    headless: bool = True,
    cookies: Optional[list] = None,
    page_timeout_ms: int = DEFAULT_PAGE_TIMEOUT_MS,
) -> Iterator[Any]:
    """Yield a Playwright ``Page`` in a fresh isolated context.

    The browser, context, and page are always torn down on exit. ``cookies``,
    when provided, are injected explicitly into this context only (never via a
    shared persistent profile directory).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise BrowserUnavailable(
            "Playwright is not installed. Run: pip install playwright && playwright install chromium"
        ) from e

    playwright = None
    browser = None
    context = None
    try:
        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.launch(headless=headless)
        except Exception as e:  # browser binary missing, etc.
            raise BrowserUnavailable(
                "Chromium is not available. Run: playwright install chromium"
            ) from e

        context = browser.new_context()
        if cookies:
            with contextlib.suppress(Exception):
                context.add_cookies(cookies)

        page = context.new_page()
        page.set_default_timeout(page_timeout_ms)
        _apply_stealth(page)
        yield page
    finally:
        for closer in (context, browser):
            if closer is not None:
                with contextlib.suppress(Exception):
                    closer.close()
        if playwright is not None:
            with contextlib.suppress(Exception):
                playwright.stop()


def _apply_stealth(page: Any) -> None:
    """Apply playwright-stealth if available; otherwise no-op (best effort)."""
    try:
        from playwright_stealth import stealth_sync  # type: ignore
    except ImportError:
        return
    with contextlib.suppress(Exception):
        stealth_sync(page)


def kill_orphan_chromium() -> int:
    """Best-effort sweep of leaked headless Chromium processes after a crash/timeout.

    Returns the number of processes terminated. Requires ``psutil``; if it is not
    installed this is a no-op (Playwright normally cleans up on context close).
    """
    try:
        import psutil  # type: ignore
    except ImportError:
        return 0

    killed = 0
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if "chrom" in name and "--headless" in cmdline and "--type=" not in cmdline:
                proc.kill()
                killed += 1
        except Exception:
            continue
    if killed:
        logger.warning("[apply_agent.browser] killed %s orphan chromium process(es)", killed)
    return killed
