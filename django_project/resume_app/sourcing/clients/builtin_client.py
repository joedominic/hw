"""
Fetch job listings from BuiltIn.com and its regional subdomains.
Returns normalized dicts compatible with job_sources.upsert_job_listing_from_fetch.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

DEFAULT_BUILTIN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

BUILTIN_SUMMARY_ENRICH_THRESHOLD = 1000


def _builtin_user_agent() -> str:
    return (getattr(settings, "BUILTIN_USER_AGENT", "") or "").strip() or DEFAULT_BUILTIN_USER_AGENT


def _builtin_proxies() -> Optional[dict]:
    return getattr(settings, "BUILTIN_PROXIES", None)


def _builtin_api_key() -> str:
    return (getattr(settings, "BUILTIN_API_KEY", "") or "").strip()


def _builtin_page_delay() -> float:
    return float(getattr(settings, "BUILTIN_PAGE_DELAY", 0.35) or 0.35)


def _resolve_builtin_base_url(location: Optional[str]) -> str:
    """Map location keywords to corresponding BuiltIn subdomains."""
    loc = (location or "").strip().lower()
    if not loc:
        return "https://builtin.com"

    # Check for NYC
    if any(alias in loc for alias in ("new york", "nyc", "manhattan", "brooklyn")):
        return "https://www.builtinnyc.com"
    # Chicago
    if "chicago" in loc:
        return "https://www.builtinchicago.org"
    # San Francisco / Bay Area
    if any(alias in loc for alias in ("san francisco", "sf", "bay area", "silicon valley")):
        return "https://www.builtinsf.com"
    # Boston
    if "boston" in loc:
        return "https://www.builtinboston.com"
    # Los Angeles
    if any(alias in loc for alias in ("los angeles", "la ", "santa monica")):
        return "https://www.builtinla.com"
    # Seattle
    if "seattle" in loc:
        return "https://www.builtinseattle.com"
    # Austin
    if "austin" in loc:
        return "https://www.builtinaustin.com"
    # Colorado / Denver
    if any(alias in loc for alias in ("colorado", "denver", "boulder")):
        return "https://www.builtincolorado.com"

    return "https://builtin.com"


def _parse_builtin_relative_date(text: str) -> Optional[datetime]:
    """Parse relative time strings from BuiltIn cards (e.g. '16 Minutes Ago') to aware datetime."""
    text = (text or "").strip().lower()
    if not text:
        return None
    if text.startswith("reposted "):
        text = text[len("reposted "):].strip()
    if text.startswith("posted "):
        text = text[len("posted "):].strip()

    now = timezone.now()
    try:
        if "today" in text or "just now" in text:
            return now
        elif "yesterday" in text:
            return now - timedelta(days=1)
        elif "minute" in text:
            m = re.search(r"(\d+)", text)
            if m:
                return now - timedelta(minutes=int(m.group(1)))
        elif "hour" in text:
            m = re.search(r"(\d+)", text)
            if m:
                return now - timedelta(hours=int(m.group(1)))
        elif "day" in text:
            m = re.search(r"(\d+)", text)
            if m:
                return now - timedelta(days=int(m.group(1)))
        elif "week" in text:
            m = re.search(r"(\d+)", text)
            if m:
                return now - timedelta(weeks=int(m.group(1)))
        elif "month" in text:
            m = re.search(r"(\d+)", text)
            if m:
                return now - timedelta(days=int(m.group(1)) * 30)
    except Exception as e:
        logger.warning("Error parsing relative date '%s': %s", text, e)
    return None


def _html_to_text(value: str) -> str:
    """Convert HTML job description markup to plain text."""
    text = html_lib.unescape(value or "")
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*p\s*>", "\n\n", text)
    text = re.sub(r"(?i)</\s*li\s*>", "\n", text)
    text = re.sub(r"(?i)<\s*li[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def extract_builtin_job_id(url_or_id: str) -> Optional[str]:
    """Extract a BuiltIn job ID from a URL, external_id, or raw ID."""
    raw = (url_or_id or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return raw
    if raw.lower().startswith("builtin:"):
        return raw.split(":", 1)[1].strip()
    match = re.search(r"/(\d+)(?:\?|$)", raw)
    return match.group(1) if match else None


def fetch_builtin_jobs(
    search_term: str,
    location: Optional[str] = None,
    results_wanted: int = 20,
    *,
    timeout_seconds: float = 10.0,
) -> List[dict]:
    """
    Query BuiltIn job search on resolved base subdomains.
    Returns normalized job listings.
    """
    if not search_term or not search_term.strip():
        raise ValueError("search_term is required for BuiltIn")

    base_url = _resolve_builtin_base_url(location)
    results_wanted = max(1, int(results_wanted))

    params = {"search": search_term.strip()}
    if location:
        params["location"] = location.strip()

    headers = {
        "User-Agent": _builtin_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    }

    url = f"{base_url}/jobs"
    try:
        response = requests.get(
            url,
            params=params,
            headers=headers,
            proxies=_builtin_proxies(),
            timeout=timeout_seconds,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        logger.warning("[builtin_client] fetch failed for url=%s: %s", url, e)
        raise RuntimeError(f"BuiltIn job fetch failed: {e}") from e

    soup = BeautifulSoup(response.text, "html.parser")

    # Step 1: Parse the JSON-LD schema for description/snippet, URL and title fallbacks.
    ld_jobs = {}
    ld_script = soup.find("script", type="application/ld+json")
    if ld_script:
        try:
            ld_data = json.loads(ld_script.string or "{}")
            for graph_item in ld_data.get("@graph", []):
                if graph_item.get("@type") == "ItemList":
                    for item in graph_item.get("itemListElement", []):
                        item_url = item.get("url", "")
                        jid = extract_builtin_job_id(item_url)
                        if jid:
                            ld_jobs[jid] = {
                                "title": item.get("name") or "Untitled",
                                "url": item_url,
                                "description": item.get("description", ""),
                            }
        except Exception as e:
            logger.warning("[builtin_client] Failed to parse JSON-LD schema: %s", e)

    # Step 2: Parse the HTML job cards to extract metadata.
    collected: List[dict] = []
    cards = soup.find_all(True, class_=lambda x: x and "job-bounded-responsive" in x)

    for i, card in enumerate(cards):
        if len(collected) >= results_wanted:
            break

        # Extract Job ID
        card_id = card.get("id", "")
        jid = card_id.replace("job-card-", "") if card_id else None

        # Title & URL
        title_el = card.find("a", attrs={"data-id": "job-card-title"}) or card.find("a", class_=lambda x: x and "card-alias-after-overlay" in x)
        title = title_el.text.strip() if title_el else "Untitled"
        job_url = title_el.get("href", "") if title_el else ""
        if job_url and not job_url.startswith("http"):
            job_url = urljoin(base_url, job_url)

        if not jid:
            jid = extract_builtin_job_id(job_url)
        if not jid:
            continue

        # Company
        company_el = card.find("a", attrs={"data-id": "company-title"}) or card.find("a", href=lambda x: x and "/company/" in x)
        company_name = company_el.text.strip() if company_el else "Unknown"

        # Attributes
        location_parts = []
        date_posted = None
        work_mode = ""

        attr_section = card.find(class_=lambda x: x and "bounded-attribute-section" in x)
        if attr_section:
            spans = [s.text.strip() for s in attr_section.find_all("span") if s.text.strip()]
            for span in spans:
                if any(mode in span.lower() for mode in ("remote", "hybrid", "onsite", "on-site", "office")):
                    work_mode = span
                elif any(time_unit in span.lower() for time_unit in ("minute", "hour", "day", "week", "month", "today", "yesterday")):
                    date_posted = _parse_builtin_relative_date(span)
                elif "saved" not in span.lower() and "level" not in span.lower() and "annually" not in span.lower() and "hourly" not in span.lower():
                    location_parts.append(span)

        location_str = ", ".join(location_parts) if location_parts else ""
        if work_mode:
            location_str = f"{location_str} ({work_mode})" if location_str else work_mode

        # Description snippet fallback from LD-JSON or general summary text
        description = ""
        if jid in ld_jobs:
            description = ld_jobs[jid].get("description") or ""
            if not job_url:
                job_url = ld_jobs[jid].get("url") or ""
            if title == "Untitled":
                title = ld_jobs[jid].get("title") or "Untitled"

        collected.append({
            "title": title,
            "company_name": company_name,
            "location": location_str or "Remote",
            "description": description.strip(),
            "job_url": job_url,
            "source": "builtin",
            "external_id": f"builtin:{jid}",
            "date_posted": date_posted or timezone.now(),
        })

    logger.info(
        "[builtin_client] Fetched %d BuiltIn job(s) (wanted %d, base=%s)",
        len(collected),
        results_wanted,
        base_url,
    )
    return collected[:results_wanted]


def fetch_builtin_job_detail(
    url_or_id: str,
    *,
    timeout_seconds: float = 15.0,
) -> dict:
    """
    Fetch details for a single BuiltIn job listing.
    Uses JSON-LD from detail page.
    """
    jid = extract_builtin_job_id(url_or_id)
    if not jid:
        raise ValueError("Not a valid BuiltIn URL or Job ID")

    url = url_or_id
    if not url.startswith("http"):
        url = f"https://builtin.com/job/placeholder/{jid}"

    headers = {
        "User-Agent": _builtin_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            proxies=_builtin_proxies(),
            timeout=timeout_seconds,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"BuiltIn job-detail fetch failed: {e}") from e

    soup = BeautifulSoup(response.text, "html.parser")
    ld_script = soup.find("script", type="application/ld+json")
    if not ld_script:
        raise RuntimeError("BuiltIn detail page did not include structured JSON-LD data")

    try:
        data = json.loads(ld_script.string or "{}")
        job_posting = None
        for item in data.get("@graph", []):
            if item.get("@type") == "JobPosting":
                job_posting = item
                break
        if not job_posting and data.get("@type") == "JobPosting":
            job_posting = data

        if not job_posting:
            raise RuntimeError("BuiltIn detail page did not include a JobPosting graph item")

        title = job_posting.get("title") or "Untitled"
        raw_desc = job_posting.get("description") or ""
        description = _html_to_text(raw_desc)

        org = job_posting.get("hiringOrganization") or {}
        company_name = org.get("name") if isinstance(org, dict) else str(org)
        company_name = (company_name or "").strip() or "Unknown"

        loc = job_posting.get("jobLocation") or {}
        location = ""
        if isinstance(loc, dict):
            addr = loc.get("address") or loc
            if isinstance(addr, dict):
                parts = [
                    str(addr.get("addressLocality") or "").strip(),
                    str(addr.get("addressRegion") or "").strip(),
                    str(addr.get("addressCountry") or "").strip(),
                ]
                location = ", ".join(p for p in parts if p)

        out = {
            "title": title,
            "company_name": company_name,
            "location": location or "Remote",
            "description": description,
            "job_url": url,
            "source": "builtin",
            "external_id": f"builtin:{jid}",
        }
        posted = job_posting.get("datePosted")
        if posted:
            try:
                out["date_posted"] = datetime.fromisoformat(posted.replace("Z", "+00:00"))
            except Exception:
                pass
        return out
    except Exception as e:
        raise RuntimeError(f"Failed to parse BuiltIn JobPosting data: {e}") from e


def enrich_builtin_job_listing_description(job, *, timeout_seconds: float = 15.0) -> str:
    """
    Fetch full detail job text for a BuiltIn job, persist it, and return.
    """
    from ...models import JobListing

    if not isinstance(job, JobListing):
        return ""
    current = (job.description or "").strip()
    if (job.source or "").strip().lower() != "builtin":
        return current
    if len(current) >= BUILTIN_SUMMARY_ENRICH_THRESHOLD:
        return current

    url = (job.url or "").strip() or (job.external_id or "")
    try:
        detail = fetch_builtin_job_detail(url, timeout_seconds=timeout_seconds)
    except Exception as e:
        logger.warning("[builtin_client] enrich failed for job_id=%s: %s", job.id, e)
        return current

    full = (detail.get("description") or "").strip()
    if not full or len(full) <= len(current):
        return current

    updates = {"description": full}
    title = (detail.get("title") or "").strip()
    if title and title != "Untitled" and (
        not (job.title or "").strip() or (job.title or "").strip() == "Untitled"
    ):
        updates["title"] = title
    company = (detail.get("company_name") or "").strip()
    if company and company != "Unknown" and (
        not (job.company_name or "").strip() or (job.company_name or "").strip() == "Unknown"
    ):
        updates["company_name"] = company
    location = (detail.get("location") or "").strip()
    if location and not (job.location or "").strip():
        updates["location"] = location
    if detail.get("date_posted") is not None and job.posted_at is None:
        updates["posted_at"] = detail["date_posted"]
    detail_url = (detail.get("job_url") or "").strip()
    if detail_url and not (job.url or "").strip():
        updates["url"] = detail_url

    JobListing.objects.filter(pk=job.pk).update(**updates)
    for key, value in updates.items():
        setattr(job, key, value)
    logger.info(
        "[builtin_client] Enriched BuiltIn job_id=%s description %d -> %d chars",
        job.id,
        len(current),
        len(full),
    )
    return full
