"""
Fetch job listings from Levels.fyi (not supported by upstream python-jobspy).

Uses the site's internal JSON API (encrypted payloads) plus optional detail
enrichment for full HTML descriptions.

Returns normalized dicts compatible with job_sources.upsert_job_listing_from_fetch.
"""
from __future__ import annotations

import base64
import hashlib
import html as html_lib
import json
import logging
import re
import time
import zlib
from typing import Any, List, Optional
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.conf import settings
from django.utils import timezone

from .job_sources import _parse_date_posted

logger = logging.getLogger(__name__)

DEFAULT_LEVELS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

LEVELS_API_ROOT = "https://api.levels.fyi/v1"
LEVELS_SEARCH_URL = f"{LEVELS_API_ROOT}/job/search"
LEVELS_JOB_URL_TEMPLATE = "https://www.levels.fyi/jobs?jobId={job_id}"
LEVELS_PAYLOAD_SECRET = b"levelstothemoon!!"
LEVELS_DEFAULT_LOCATION_SLUG = "united-states"
LEVELS_DEFAULT_OFFSET_STEP = 10
LEVELS_DEFAULT_PAGE_DELAY = 0.35
LEVELS_DEFAULT_RESULTS_WANTED = 20
LEVELS_DEFAULT_MAX_SCAN_PAGES = 30
LEVELS_SUMMARY_ENRICH_THRESHOLD = 1200

_LEVELS_KNOWN_FAMILY_SLUGS = frozenset({
    "software-engineer",
    "software-engineering-manager",
    "product-manager",
    "data-scientist",
    "product-designer",
    "mechanical-engineer",
    "civil-engineer",
    "business-analyst",
    "financial-analyst",
    "accountant",
    "human-resources",
    "chief-of-staff",
    "biomedical-engineer",
    "chemical-engineer",
    "venture-capitalist",
})

_SENIORITY_PREFIXES = (
    "staff-",
    "senior-",
    "sr-",
    "principal-",
    "lead-",
    "junior-",
    "jr-",
    "associate-",
    "entry-level-",
    "mid-",
    "intern-",
)

_TITLE_FILTER_STOPWORDS = frozenset({
    "a", "an", "and", "at", "for", "in", "of", "or", "the", "to", "with",
})

# Common user inputs → Levels.fyi locationSlug values.
_LEVELS_LOCATION_ALIASES: dict[str, str] = {
    "us": "united-states",
    "usa": "united-states",
    "u-s": "united-states",
    "u-s-a": "united-states",
    "america": "united-states",
    "united-states": "united-states",
    "united-states-of-america": "united-states",
    "uk": "united-kingdom",
    "gb": "united-kingdom",
    "great-britain": "united-kingdom",
    "united-kingdom": "united-kingdom",
    "england": "united-kingdom",
    "ca": "canada",
    "canada": "canada",
    "in": "india",
    "ind": "india",
    "india": "india",
    "ie": "ireland",
    "ireland": "ireland",
    "de": "germany",
    "germany": "germany",
    "fr": "france",
    "france": "france",
    "jp": "japan",
    "japan": "japan",
    "eu": "europe",
    "europe": "europe",
    "sf": "san-francisco-bay-area",
    "san-francisco": "san-francisco-bay-area",
    "san-francisco-bay-area": "san-francisco-bay-area",
    "bay-area": "san-francisco-bay-area",
    "nyc": "new-york-city-area",
    "new-york": "new-york-city-area",
    "new-york-city": "new-york-city-area",
    "new-york-city-area": "new-york-city-area",
    "seattle": "greater-seattle-area",
    "greater-seattle-area": "greater-seattle-area",
}

# Post-filter markers when API returns promoted/global rows for bad location slugs.
_LEVELS_COUNTRY_LOCATION_MARKERS: dict[str, tuple[str, ...]] = {
    "united-kingdom": (
        "united kingdom",
        ", uk",
        "england",
        "scotland",
        "wales",
        "northern ireland",
    ),
    "canada": ("canada", ", ca", "ontario", "quebec", "british columbia", "alberta"),
    "india": ("india", ", ind", "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad"),
    "ireland": ("ireland", "dublin", "cork", "galway"),
    "germany": ("germany", "berlin", "munich", "frankfurt"),
    "france": ("france", "paris", "lyon"),
    "japan": ("japan", "tokyo", "osaka"),
}

# When searching united-states, drop obvious non-US rows the API still returns.
_LEVELS_US_FOREIGN_EXCLUDE: tuple[str, ...] = (
    "india",
    ", ind",
    "bangalore",
    "bengaluru",
    "mumbai",
    "hyderabad",
    "delhi",
    "chennai",
    "pune",
    "ireland",
    "dublin",
    "cork",
    "galway",
    "united kingdom",
    ", uk",
    "england",
    "scotland",
    "london",
    "germany",
    "berlin",
    "munich",
    "france",
    "paris",
    "japan",
    "tokyo",
    "singapore",
    "canada",
    "toronto",
    "vancouver",
    "montreal",
    "australia",
    "sydney",
    "melbourne",
    "china",
    "beijing",
    "shanghai",
    "spain",
    "mexico",
    "brazil",
    "israel",
    "tel aviv",
    "netherlands",
    "amsterdam",
    "switzerland",
    "zurich",
)

_LEVELS_JOB_ID_RE = re.compile(
    r"(?:levels\.fyi/[^?]*\?[^#]*jobId=|levels:)(\d+)",
    re.I,
)


def _levels_api_key() -> bytes:
    """AES-128 key derived the same way as Levels.fyi browser JS."""
    digest = hashlib.md5(LEVELS_PAYLOAD_SECRET).digest()
    return base64.b64encode(digest)[:16]


def _decrypt_payload(payload_b64: str) -> Any:
    """Decrypt a Levels.fyi encrypted API payload to JSON."""
    raw = base64.b64decode(payload_b64)
    cipher = Cipher(algorithms.AES(_levels_api_key()), modes.ECB())
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(raw) + decryptor.finalize()
    pad = decrypted[-1]
    if 1 <= pad <= 16 and decrypted.endswith(bytes([pad]) * pad):
        decrypted = decrypted[:-pad]
    return json.loads(zlib.decompress(decrypted))


def _parse_levels_response(data: Any) -> Any:
    """Return decrypted JSON when the body uses the encrypted payload wrapper."""
    if isinstance(data, dict) and "payload" in data:
        return _decrypt_payload(str(data["payload"]))
    return data


def _slugify(value: str) -> str:
    """Convert free text to Levels.fyi URL/API slug form."""
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _levels_location_slug(location: Optional[str]) -> str:
    """Map free-text location input to a Levels.fyi locationSlug."""
    text = (location or "").strip()
    if not text:
        return LEVELS_DEFAULT_LOCATION_SLUG
    slug = _slugify(text)
    if slug in _LEVELS_LOCATION_ALIASES:
        return _LEVELS_LOCATION_ALIASES[slug]
    # Also try the raw lowercased token (e.g. "US" before slugify).
    lowered = text.lower()
    if lowered in _LEVELS_LOCATION_ALIASES:
        return _LEVELS_LOCATION_ALIASES[lowered]
    return slug


def _locations_match_country_slug(locations: Any, location_slug: str) -> bool:
    """Return True when job locations appear to belong to the requested country slug."""
    if isinstance(locations, list):
        parts = [str(loc).strip() for loc in locations if str(loc).strip()]
    else:
        parts = [str(locations or "").strip()]
    if not parts:
        return False
    haystack = " ".join(parts).lower()

    if location_slug == "united-states":
        return not any(marker in haystack for marker in _LEVELS_US_FOREIGN_EXCLUDE)

    markers = _LEVELS_COUNTRY_LOCATION_MARKERS.get(location_slug)
    if not markers:
        return True
    return any(marker in haystack for marker in markers)


def _resolve_levels_search_params(search_term: str) -> dict[str, Any]:
    """
    Map a free-text search to Levels.fyi API params.

    Prefer ``jobFamilySlug`` for broader results; use client-side title matching
    when the user's query is more specific than the family slug.
    """
    slug = _slugify(search_term)
    if not slug:
        return {"job_title_slug": None, "job_family_slug": None, "filter_title_client_side": False}

    if slug in _LEVELS_KNOWN_FAMILY_SLUGS:
        return {
            "job_title_slug": None,
            "job_family_slug": slug,
            "filter_title_client_side": False,
        }

    for prefix in _SENIORITY_PREFIXES:
        if not slug.startswith(prefix):
            continue
        remainder = slug[len(prefix):]
        if remainder in _LEVELS_KNOWN_FAMILY_SLUGS:
            return {
                "job_title_slug": None,
                "job_family_slug": remainder,
                "filter_title_client_side": True,
            }
        normalized = remainder.replace("-development-", "-").replace("-engineering-", "-engineer-")
        if normalized in _LEVELS_KNOWN_FAMILY_SLUGS:
            return {
                "job_title_slug": None,
                "job_family_slug": normalized,
                "filter_title_client_side": True,
            }
        if normalized.endswith("-engineer") and "software-engineer" in _LEVELS_KNOWN_FAMILY_SLUGS:
            return {
                "job_title_slug": None,
                "job_family_slug": "software-engineer",
                "filter_title_client_side": True,
            }

    if "engineering-manager" in slug or ("manager" in slug and "engineer" in slug):
        return {
            "job_title_slug": None,
            "job_family_slug": "software-engineering-manager",
            "filter_title_client_side": True,
        }
    if any(token in slug for token in ("engineer", "developer", "swe", "sde")) and "manager" not in slug:
        return {
            "job_title_slug": None,
            "job_family_slug": "software-engineer",
            "filter_title_client_side": True,
        }
    if "product-manager" in slug or ("product" in slug and "manager" in slug):
        return {
            "job_title_slug": None,
            "job_family_slug": "product-manager",
            "filter_title_client_side": True,
        }
    if "data-scientist" in slug or "data-science" in slug:
        return {
            "job_title_slug": None,
            "job_family_slug": "data-scientist",
            "filter_title_client_side": True,
        }

    return {
        "job_title_slug": slug,
        "job_family_slug": None,
        "filter_title_client_side": False,
    }


def _title_matches_search(title: str, search_term: str) -> bool:
    """Loose client-side title filter when API search uses a broad family slug."""
    title_tokens = set(re.findall(r"\w+", (title or "").lower()))
    search_tokens = {
        token
        for token in re.findall(r"\w+", (search_term or "").lower())
        if token not in _TITLE_FILTER_STOPWORDS and len(token) > 1
    }
    if not search_tokens:
        return True
    matches = sum(
        1
        for token in search_tokens
        if token in title_tokens or any(token in title_token for title_token in title_tokens)
    )
    return matches >= max(1, len(search_tokens) // 2)


def _levels_standard_levels() -> List[str]:
    raw = (getattr(settings, "LEVELS_FYI_STANDARD_LEVELS", "") or "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _levels_offset_step() -> int:
    step = int(getattr(settings, "LEVELS_FYI_OFFSET_STEP", LEVELS_DEFAULT_OFFSET_STEP) or LEVELS_DEFAULT_OFFSET_STEP)
    return max(1, step)


def _levels_page_delay() -> float:
    delay = float(getattr(settings, "LEVELS_FYI_PAGE_DELAY", LEVELS_DEFAULT_PAGE_DELAY) or LEVELS_DEFAULT_PAGE_DELAY)
    return max(0.0, delay)


def _levels_max_scan_pages() -> int:
    pages = int(
        getattr(settings, "LEVELS_FYI_MAX_SCAN_PAGES", LEVELS_DEFAULT_MAX_SCAN_PAGES)
        or LEVELS_DEFAULT_MAX_SCAN_PAGES
    )
    return max(1, pages)


def _levels_external_id(job_id: str) -> str:
    return f"levels:{job_id}"


def extract_levels_job_id(value: str) -> Optional[str]:
    """Extract Levels.fyi job id from external_id or URL."""
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("levels:"):
        job_id = raw.split(":", 1)[1].strip()
        return job_id or None
    match = _LEVELS_JOB_ID_RE.search(raw)
    return match.group(1) if match else None


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


def _format_salary(min_val: Any, max_val: Any, currency: str) -> str:
    cur = (currency or "USD").strip().upper()
    try:
        lo = int(min_val) if min_val is not None else None
        hi = int(max_val) if max_val is not None else None
    except (TypeError, ValueError):
        lo = hi = None
    if lo is not None and hi is not None and lo > 0 and hi > 0:
        return f"{cur} {lo:,} - {hi:,}"
    if lo is not None and lo > 0:
        return f"{cur} {lo:,}+"
    if hi is not None and hi > 0:
        return f"Up to {cur} {hi:,}"
    return ""


def _build_list_snippet(job: dict, company_name: str) -> str:
    """Short description for list rows before detail enrichment."""
    parts: List[str] = []
    locations = job.get("locations") or []
    if isinstance(locations, list) and locations:
        parts.append(", ".join(str(loc).strip() for loc in locations if str(loc).strip()))
    arrangement = str(job.get("workArrangement") or "").strip()
    if arrangement:
        parts.append(arrangement.title())
    salary = _format_salary(
        job.get("minTotalSalary") or job.get("minBaseSalary"),
        job.get("maxTotalSalary") or job.get("maxBaseSalary"),
        str(job.get("baseSalaryCurrency") or "USD"),
    )
    if salary:
        parts.append(f"Salary: {salary}")
    company = (company_name or "").strip()
    if company:
        parts.append(f"Company: {company}")
    return ". ".join(parts)


def _job_to_dict(job: dict, company: dict) -> dict:
    job_id = str(job.get("id") or "").strip()
    title = str(job.get("title") or "").strip() or "Untitled"
    company_name = str(company.get("companyName") or company.get("name") or "").strip() or "Unknown"
    locations = job.get("locations") or []
    if isinstance(locations, list):
        location = ", ".join(str(loc).strip() for loc in locations if str(loc).strip())
    else:
        location = str(locations or "").strip()
    application_url = str(job.get("applicationUrl") or "").strip()
    job_url = application_url or LEVELS_JOB_URL_TEMPLATE.format(job_id=job_id)
    description = _build_list_snippet(job, company_name)
    out = {
        "title": title,
        "company_name": company_name,
        "location": location,
        "description": description,
        "job_url": job_url,
        "source": "levels",
        "external_id": _levels_external_id(job_id),
        "raw_json": {
            "companySlug": company.get("companySlug"),
            "workArrangement": job.get("workArrangement"),
            "minBaseSalary": job.get("minBaseSalary"),
            "maxBaseSalary": job.get("maxBaseSalary"),
            "minTotalSalary": job.get("minTotalSalary"),
            "maxTotalSalary": job.get("maxTotalSalary"),
            "baseSalaryCurrency": job.get("baseSalaryCurrency"),
        },
    }
    posted = _parse_date_posted(job.get("postingDate"))
    if posted is not None:
        out["date_posted"] = posted
    return out


def _flatten_search_results(payload: dict) -> List[dict]:
    rows: List[dict] = []
    for company in payload.get("results") or []:
        if not isinstance(company, dict):
            continue
        for job in company.get("jobs") or []:
            if isinstance(job, dict):
                rows.append(_job_to_dict(job, company))
    return rows


def _levels_request(
    session: requests.Session,
    url: str,
    *,
    timeout_seconds: float,
) -> Any:
    headers = {
        "User-Agent": DEFAULT_LEVELS_USER_AGENT,
        "Accept": "application/json",
        "Origin": "https://www.levels.fyi",
        "Referer": "https://www.levels.fyi/",
    }
    response = session.get(url, headers=headers, timeout=timeout_seconds)
    response.raise_for_status()
    return _parse_levels_response(response.json())


def _build_search_url(
    *,
    offset: int,
    location_slug: str,
    job_title_slug: Optional[str],
    job_family_slug: Optional[str],
    standard_levels: List[str],
) -> str:
    params: List[tuple[str, str]] = [
        ("offset", str(offset)),
        ("locationSlug", location_slug),
    ]
    if job_family_slug:
        params.append(("jobFamilySlug", job_family_slug))
    elif job_title_slug:
        params.append(("jobTitleSlug", job_title_slug))
    for level in standard_levels:
        params.append(("standardLevels", level))
    return f"{LEVELS_SEARCH_URL}?{urlencode(params)}"


def fetch_levels_jobs(
    search_term: str,
    location: Optional[str] = None,
    results_wanted: int = 20,
    *,
    hours_old: Optional[int] = None,
    timeout_seconds: float = 10.0,
) -> List[dict]:
    """
    Query Levels.fyi job search API and return normalized rows.

    ``hours_old`` is applied by the orchestrator via ``filter_rows_by_max_age``.
    """
    del hours_old  # post-filtered centrally in job_sources.fetch_jobs
    wanted = max(1, int(results_wanted or LEVELS_DEFAULT_RESULTS_WANTED))
    search_params = _resolve_levels_search_params(search_term)
    job_title_slug = search_params["job_title_slug"]
    job_family_slug = search_params["job_family_slug"]
    filter_title_client_side = bool(search_params["filter_title_client_side"])
    location_slug = _levels_location_slug(location)
    standard_levels = _levels_standard_levels()
    offset_step = _levels_offset_step()
    page_delay = _levels_page_delay()
    max_scan_pages = _levels_max_scan_pages()

    session = requests.Session()
    merged: List[dict] = []
    seen_ids: set[str] = set()
    offset = 0
    pages_scanned = 0
    stale_pages = 0

    while len(merged) < wanted and pages_scanned < max_scan_pages:
        url = _build_search_url(
            offset=offset,
            location_slug=location_slug,
            job_title_slug=job_title_slug,
            job_family_slug=job_family_slug,
            standard_levels=standard_levels,
        )
        try:
            payload = _levels_request(session, url, timeout_seconds=timeout_seconds)
        except requests.RequestException as e:
            logger.warning("[levels_client] search request failed at offset=%s: %s", offset, e)
            break

        pages_scanned += 1
        if not isinstance(payload, dict):
            break

        page_rows = _flatten_search_results(payload)
        if not page_rows:
            break

        new_on_page = 0
        for row in page_rows:
            ext_id = str(row.get("external_id") or "")
            if ext_id in seen_ids:
                continue
            if not _locations_match_country_slug([row.get("location") or ""], location_slug):
                continue
            if filter_title_client_side and not _title_matches_search(
                str(row.get("title") or ""),
                search_term,
            ):
                continue
            seen_ids.add(ext_id)
            merged.append(row)
            new_on_page += 1
            if len(merged) >= wanted:
                break

        if new_on_page == 0:
            stale_pages += 1
            if stale_pages >= 6:
                break
        else:
            stale_pages = 0

        offset += offset_step
        if page_delay > 0:
            time.sleep(page_delay)

    logger.info(
        "[levels_client] Fetched %d Levels.fyi job(s) (wanted %d, family=%s, title=%s, pages=%d)",
        len(merged),
        wanted,
        job_family_slug or "-",
        job_title_slug or "-",
        pages_scanned,
    )
    return merged[:wanted]


def fetch_levels_job_detail(
    job_id: str,
    *,
    timeout_seconds: float = 15.0,
) -> dict:
    """Fetch a single Levels.fyi job by id and return a normalized detail dict."""
    jid = extract_levels_job_id(job_id) or str(job_id or "").strip()
    if not jid:
        raise ValueError("Levels.fyi job id is required")

    session = requests.Session()
    url = f"{LEVELS_API_ROOT}/job/{jid}"
    try:
        payload = _levels_request(session, url, timeout_seconds=timeout_seconds)
    except requests.RequestException as e:
        raise RuntimeError(f"Levels.fyi job-detail fetch failed: {e}") from e

    if not isinstance(payload, dict):
        raise RuntimeError("Levels.fyi job-detail response was not a JSON object")

    company_info = payload.get("companyInfo") or {}
    if not isinstance(company_info, dict):
        company_info = {}
    locations = payload.get("locations") or []
    if isinstance(locations, list):
        location = ", ".join(str(loc).strip() for loc in locations if str(loc).strip())
    else:
        location = str(locations or "").strip()

    description_html = str(payload.get("description") or "")
    description = _html_to_text(description_html)
    if not description:
        raise RuntimeError("Levels.fyi job-detail did not include a description")

    application_url = str(payload.get("applicationUrl") or "").strip()
    job_url = application_url or LEVELS_JOB_URL_TEMPLATE.format(job_id=jid)
    title = str(payload.get("title") or "").strip() or "Untitled"
    company_name = str(company_info.get("name") or "").strip() or "Unknown"

    out = {
        "title": title,
        "company_name": company_name,
        "location": location,
        "description": description,
        "job_url": job_url,
        "source": "levels",
        "external_id": _levels_external_id(jid),
    }
    posted = _parse_date_posted(payload.get("postingDate"))
    if posted is not None:
        out["date_posted"] = posted
    return out


def enrich_levels_job_listing_description(job, *, timeout_seconds: float = 15.0) -> str:
    """
    If ``job`` is a Levels.fyi listing with a short/empty description, fetch the
    full job-detail text, persist it, and return the enriched description.
    """
    from .models import JobListing

    if not isinstance(job, JobListing):
        return ""
    current = (job.description or "").strip()
    if (job.source or "").strip().lower() != "levels":
        return current
    if len(current) >= LEVELS_SUMMARY_ENRICH_THRESHOLD:
        return current

    job_id = extract_levels_job_id(job.external_id or "") or extract_levels_job_id(job.url or "")
    if not job_id:
        return current

    try:
        detail = fetch_levels_job_detail(job_id, timeout_seconds=timeout_seconds)
    except Exception as e:
        logger.warning("[levels_client] enrich failed for job_id=%s: %s", job.id, e)
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
        "[levels_client] Enriched Levels.fyi job_id=%s description %d -> %d chars",
        job.id,
        len(current),
        len(full),
    )
    return full
