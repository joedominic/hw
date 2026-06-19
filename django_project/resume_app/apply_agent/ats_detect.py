"""
ATS classification from a resolved apply URL (and optional page DOM).

Host-based detection is pure and unit-testable. DOM-based detection is a
best-effort fallback used by the live resolver when the host is ambiguous.
"""
from __future__ import annotations

from urllib.parse import urlparse

ATS_UNKNOWN = "unknown"

# Ordered (substring, ats_id). First host match wins.
_HOST_MARKERS: list[tuple[str, str]] = [
    ("boards.greenhouse.io", "greenhouse"),
    ("job-boards.greenhouse.io", "greenhouse"),
    ("greenhouse.io", "greenhouse"),
    ("jobs.lever.co", "lever"),
    ("lever.co", "lever"),
    ("jobs.ashbyhq.com", "ashby"),
    ("ashbyhq.com", "ashby"),
    ("myworkdayjobs.com", "workday"),
    ("workday.com", "workday"),
    ("icims.com", "icims"),
]

# DOM substring markers (lowercased) used only when host is inconclusive.
_DOM_MARKERS: list[tuple[str, str]] = [
    ("greenhouse.io/embed", "greenhouse"),
    ("grnhse", "greenhouse"),
    ("lever.co", "lever"),
    ("ashbyhq", "ashby"),
    ("myworkdayjobs", "workday"),
    ("icims", "icims"),
]


def detect_ats_from_url(url: str) -> str:
    """Return an ATS slug from the URL host, or ``unknown``."""
    if not url:
        return ATS_UNKNOWN
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ATS_UNKNOWN
    for marker, ats_id in _HOST_MARKERS:
        if marker in host:
            return ats_id
    return ATS_UNKNOWN


def detect_ats_from_dom(html: str) -> str:
    """Best-effort ATS detection from page HTML when the host is ambiguous."""
    if not html:
        return ATS_UNKNOWN
    lowered = html.lower()
    for marker, ats_id in _DOM_MARKERS:
        if marker in lowered:
            return ats_id
    return ATS_UNKNOWN


def detect_ats(url: str, html: str = "") -> str:
    """Detect ATS from the URL first, falling back to DOM markers."""
    ats = detect_ats_from_url(url)
    if ats != ATS_UNKNOWN:
        return ats
    return detect_ats_from_dom(html)
