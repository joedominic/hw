"""
Resolve a JobListing URL to its real apply URL and detect the ATS.

URL resolution and ATS detection are deeply coupled (you often cannot classify
the ATS until client-side redirects finish), so they live in one looping pass.

v1 is mock-first: a deterministic URL map drives full state-machine end-to-end
tests without live-network volatility. The live Playwright path (following
aggregator redirects, clicking "Apply on company site") is used only when
mock mode is disabled.
"""
from __future__ import annotations

import dataclasses
import logging
from urllib.parse import urlparse

from django.conf import settings

from . import ats_detect

logger = logging.getLogger("huey")

# Aggregators rarely link directly to a fillable form; their URLs are redirects.
_AGGREGATOR_HOSTS = (
    "indeed.com",
    "linkedin.com",
    "dice.com",
    "glassdoor.com",
    "ziprecruiter.com",
)

# Default deterministic map: source URL -> (final apply URL, ats slug).
# Extendable via settings.APPLY_MOCK_URL_MAP for local development and tests.
DEFAULT_MOCK_URL_MAP: dict[str, tuple[str, str]] = {
    "https://www.indeed.com/viewjob?jk=mock-greenhouse": (
        "https://boards.greenhouse.io/acme/jobs/123456",
        "greenhouse",
    ),
    "https://www.linkedin.com/jobs/view/mock-lever": (
        "https://jobs.lever.co/acme/00000000-0000-0000-0000-000000000000",
        "lever",
    ),
}

MAX_REDIRECT_HOPS = 6


@dataclasses.dataclass
class ResolveResult:
    ok: bool
    apply_url: str = ""
    ats_type: str = ats_detect.ATS_UNKNOWN
    aggregator_easy_apply: bool = False
    error_code: str = ""
    message: str = ""


def _is_aggregator(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _AGGREGATOR_HOSTS)


def _mock_map() -> dict:
    base = dict(DEFAULT_MOCK_URL_MAP)
    override = getattr(settings, "APPLY_MOCK_URL_MAP", None)
    if isinstance(override, dict):
        for key, value in override.items():
            if isinstance(value, (list, tuple)) and len(value) == 2:
                base[key] = (value[0], value[1])
    return base


def use_mock_resolver() -> bool:
    return bool(getattr(settings, "APPLY_USE_MOCK_RESOLVER", True))


def resolve_and_detect(url: str, *, use_mock: bool | None = None) -> ResolveResult:
    """Resolve ``url`` to an apply URL and detect the ATS.

    Returns ``ok=False`` with ``unresolved_url`` when the URL is an aggregator
    redirect that cannot be resolved (the user can supply an override URL).
    """
    url = (url or "").strip()
    if not url:
        return ResolveResult(ok=False, error_code="unresolved_url", message="Job has no URL")

    if use_mock is None:
        use_mock = use_mock_resolver()

    if use_mock:
        return _resolve_mock(url)
    return _resolve_live(url)


def _resolve_mock(url: str) -> ResolveResult:
    mapping = _mock_map()
    if url in mapping:
        final_url, ats = mapping[url]
        return ResolveResult(ok=True, apply_url=final_url, ats_type=ats or ats_detect.ATS_UNKNOWN)

    ats = ats_detect.detect_ats_from_url(url)
    if ats != ats_detect.ATS_UNKNOWN:
        return ResolveResult(ok=True, apply_url=url, ats_type=ats)

    if _is_aggregator(url):
        return ResolveResult(
            ok=False,
            error_code="unresolved_url",
            message="Aggregator URL has no mock mapping; supply an override apply URL.",
        )

    # Direct company page with an unknown ATS — generic fallback can try it.
    return ResolveResult(ok=True, apply_url=url, ats_type=ats_detect.ATS_UNKNOWN)


def _resolve_live(url: str) -> ResolveResult:
    """Follow redirects with Playwright and classify the final apply page.

    Used only when mock mode is disabled (build step 8). Imports Playwright
    lazily so the module stays importable without the browser installed.
    """
    from .browser import BrowserUnavailable, apply_browser_headless, browser_session

    try:
        with browser_session(headless=apply_browser_headless()) as page:
            current = url
            ats = ats_detect.ATS_UNKNOWN
            for _hop in range(MAX_REDIRECT_HOPS):
                page.goto(current, wait_until="domcontentloaded")
                final_url = page.url
                html = ""
                try:
                    html = page.content()
                except Exception:
                    html = ""
                ats = ats_detect.detect_ats(final_url, html)
                if ats != ats_detect.ATS_UNKNOWN:
                    return ResolveResult(ok=True, apply_url=final_url, ats_type=ats)

                # Try to follow an "Apply on company site" style external link.
                next_url = _find_external_apply_link(page)
                if not next_url or next_url == current:
                    break
                current = next_url

            final_url = page.url
            if _is_aggregator(final_url):
                return ResolveResult(
                    ok=False,
                    error_code="unresolved_url",
                    message="Could not follow aggregator redirect to a company apply page.",
                )
            return ResolveResult(ok=True, apply_url=final_url, ats_type=ats)
    except BrowserUnavailable as e:
        return ResolveResult(ok=False, error_code="fill_failed", message=str(e))
    except Exception as e:  # noqa: BLE001 - any browser failure is non-fatal here
        logger.warning("[resolve_and_detect] live resolve failed for %s: %s", url, e)
        return ResolveResult(ok=False, error_code="unresolved_url", message=str(e))


def _find_external_apply_link(page) -> str:
    """Best-effort: locate an 'Apply' / 'Apply on company site' link and return its href."""
    selectors = [
        "a:has-text('Apply on company site')",
        "a:has-text('Apply on company website')",
        "a:has-text('Apply externally')",
        "a[rel='nofollow'][target='_blank']:has-text('Apply')",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0:
                href = loc.get_attribute("href")
                if href:
                    return href
        except Exception:
            continue
    return ""
