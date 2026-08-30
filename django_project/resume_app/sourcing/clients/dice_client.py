"""
Fetch job listings from Dice.com (not supported by upstream python-jobspy).

Primary path: Dice job-search JSON API used by the public site.
Fallback: HTML scrape of /jobs when the API is unavailable.

Returns normalized dicts compatible with job_sources.upsert_job_listing_from_fetch.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import logging
import re
import time
from datetime import date, datetime, time as time_cls, timedelta
from typing import Any, List, Optional
from urllib.parse import unquote, urljoin

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

logger = logging.getLogger(__name__)

DEFAULT_DICE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Public browser key used by dice.com front-end (overridable via DICE_API_KEY).
DEFAULT_DICE_API_KEY = "1YAt0R9wBg4WfsF9VB2778F5CHLAPMVW3WAZcKd8"
DICE_SEARCH_API_URL = "https://job-search-api.svc.dhigroupinc.com/v1/dice/jobs/search"
DICE_SEARCH_URL = "https://www.dice.com/jobs"
DICE_JOB_DETAIL_URL = "https://www.dice.com/job-detail/{guid}"
DICE_JOBS_PER_PAGE = 20
DICE_PAGE_DELAY_SECONDS = 0.35
DICE_HTML_PAGE_DELAY_SECONDS = 2.0
# Search API summaries are short; treat shorter stored descriptions as incomplete.
DICE_SUMMARY_ENRICH_THRESHOLD = 1200

_DICE_GUID_RE = re.compile(
    r"(?:dice\.com/(?:job-detail|direct-apply)/|dice:)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I,
)
_LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)


def _external_id(title: str, company: str, job_url: str) -> str:
    raw = f"{title}|{company}|{job_url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


def _dice_external_id(job_id: Optional[str], title: str, company: str, job_url: str) -> str:
    jid = (job_id or "").strip()
    if jid:
        return f"dice:{jid}"
    return _external_id(title, company, job_url)


def _parse_date_posted(val) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        val = datetime.combine(val, time_cls.min)
    if isinstance(val, datetime):
        if timezone.is_naive(val):
            return timezone.make_aware(val, timezone.get_current_timezone())
        return val
    s = str(val).strip()
    if not s:
        return None
    parsed = parse_datetime(s)
    if parsed is None:
        d = parse_date(s)
        if d is not None:
            parsed = datetime.combine(d, time_cls.min)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _dice_api_key() -> str:
    return (getattr(settings, "DICE_API_KEY", "") or "").strip() or DEFAULT_DICE_API_KEY


def _dice_country_code() -> str:
    raw = (getattr(settings, "DICE_COUNTRY_CODE", "US") or "US").strip().upper()
    return raw[:2] or "US"


def _dice_posted_date_filter(hours_old: Optional[int]) -> Optional[str]:
    """Map hours_old to Dice API filters.postedDate values."""
    if not hours_old or hours_old <= 0:
        return None
    if hours_old <= 24:
        return "ONE"
    if hours_old <= 72:
        return "THREE"
    if hours_old <= 168:
        return "SEVEN"
    if hours_old <= 336:
        return "FOURTEEN"
    return "THIRTY"


def _dice_posted_date_param(hours_old: Optional[int]) -> Optional[str]:
    """Legacy HTML query param for postedDate."""
    if not hours_old or hours_old <= 0:
        return None
    if hours_old <= 24:
        return "Today"
    if hours_old <= 72:
        return "Last 3 Days"
    return "Last 7 Days"


def _normalize_dice_api_query(search_term: str) -> str:
    """
    Dice's JSON API treats quote characters as literals and often returns 0 hits
    for phrase queries like `"financial crimes" technology`. Strip quotes/punctuation
    noise while preserving keywords.
    """
    q = (search_term or "").strip()
    q = q.replace('"', " ").replace("'", " ")
    q = re.sub(r"\s+", " ", q).strip()
    return q


def _clean_dice_title(title: str) -> str:
    """Normalize Dice card/API titles (HTML entities, trailing hash suffixes)."""
    cleaned = html_lib.unescape(title or "").strip()
    if cleaned.lower().startswith("view details for "):
        cleaned = cleaned[17:].strip()
    # Card aria-labels append " (<32+ hex id>)"
    cleaned = re.sub(r"\s*\([0-9a-f]{16,}\)\s*$", "", cleaned, flags=re.I).strip()
    return cleaned


def _parse_dice_relative_date(text: str) -> Optional[date]:
    text = (text or "").strip()
    if not text:
        return None
    if text.lower() == "today":
        return timezone.now().date()
    m = re.search(r"(\d+)\s*days?\s*ago", text, re.I)
    if m:
        return (timezone.now() - timedelta(days=int(m.group(1)))).date()
    return None


def _location_display(city: Optional[str], state: Optional[str]) -> str:
    parts = [p for p in (city, state) if p]
    return ", ".join(parts)


def _location_from_api(job_location: Any) -> str:
    if isinstance(job_location, dict):
        display = str(job_location.get("displayName") or "").strip()
        if display:
            return display
        return _location_display(
            str(job_location.get("city") or "").strip() or None,
            str(job_location.get("state") or job_location.get("region") or "").strip() or None,
        )
    return str(job_location or "").strip()


def _job_url_from_api(item: dict) -> str:
    for key in ("detailsPageUrl", "redirectUrl"):
        url = str(item.get(key) or "").strip()
        if url:
            return url
    guid = str(item.get("guid") or "").strip()
    if guid:
        return f"https://www.dice.com/job-detail/{guid}"
    job_id = str(item.get("id") or item.get("jobId") or "").strip()
    if job_id:
        return f"https://www.dice.com/jobs?q={job_id}"
    return ""


def _dice_job_to_dict(
    *,
    title: str,
    company_name: Optional[str],
    city: Optional[str] = None,
    state: Optional[str] = None,
    location: Optional[str] = None,
    job_url: str,
    date_posted: Optional[date | datetime] = None,
    description: str = "",
    job_id: Optional[str] = None,
) -> dict:
    loc = (location or "").strip() or _location_display(city, state)
    company = (company_name or "").strip() or "Unknown"
    title = _clean_dice_title(title) or (title or "").strip() or "Untitled"
    out = {
        "title": title,
        "company_name": company,
        "location": loc,
        "description": (description or "").strip(),
        "job_url": job_url,
        "source": "dice",
        "external_id": _dice_external_id(job_id, title, company, job_url),
    }
    if date_posted is not None:
        if isinstance(date_posted, date) and not isinstance(date_posted, datetime):
            date_posted = datetime.combine(date_posted, datetime.min.time())
        parsed = _parse_date_posted(date_posted)
        if parsed is not None:
            out["date_posted"] = parsed
    return out


def _api_item_to_dict(item: dict) -> Optional[dict]:
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    title = _clean_dice_title(title) or title
    job_url = _job_url_from_api(item)
    if not job_url:
        return None
    job_id = str(item.get("id") or item.get("jobId") or "").strip() or None
    return _dice_job_to_dict(
        title=title,
        company_name=str(item.get("companyName") or "").strip() or None,
        location=_location_from_api(item.get("jobLocation")),
        job_url=job_url,
        date_posted=item.get("postedDate") or item.get("firstActiveDate"),
        description=str(item.get("summary") or "").strip(),
        job_id=job_id,
    )


_CARD_LINK_RE = re.compile(
    r'aria-label="View Details for (?P<title>[^"]+?)"\s+'
    r'data-testid="job-search-job-card-link"\s+'
    r'href="(?P<href>[^"]+)"',
    re.I,
)


def _company_from_html_context(context: str) -> Optional[str]:
    company_match = re.search(r"companyname=([^\"&]+)", context, re.I)
    if company_match:
        return unquote(company_match.group(1).replace("+", " ")).strip() or None
    img_alt = re.search(r'<img[^>]+alt="([^"]+)"', context, re.I)
    if img_alt:
        alt = html_lib.unescape(img_alt.group(1)).strip()
        if alt and alt.lower() not in ("company logo", "logo"):
            return alt
    return None


def _parse_jobs_from_html(html: str, seen_urls: set[str]) -> List[dict]:
    """
    Best-effort HTML fallback when the JSON API is unavailable.

    Prefer card-link aria-labels (`job-search-job-card-link`); the older
    `job-search-job-detail-link` titles are incomplete on current Dice markup.
    """
    jobs: List[dict] = []
    seen_ids: set[str] = set()

    for match in _CARD_LINK_RE.finditer(html or ""):
        raw_url = match.group("href")
        title = _clean_dice_title(match.group("title"))
        job_url = urljoin("https://www.dice.com", raw_url)
        uuid = job_url.rstrip("/").split("/")[-1]
        short_id = uuid[:8]
        if short_id in seen_ids or job_url in seen_urls:
            continue
        if not title:
            continue
        seen_ids.add(short_id)
        seen_urls.add(job_url)

        start_pos = match.start()
        context = html[start_pos : min(len(html), start_pos + 2500)]
        company_name = _company_from_html_context(context)

        city, state = None, None
        loc_patterns = [
            r'">([A-Z][a-z]+(?: [A-Z][a-z]+)*),\s*([A-Z][a-z]{2,})(?:•|<)',
            r'">([A-Za-z\s]+),\s*([A-Z]{2})\s*•',
        ]
        for pattern in loc_patterns:
            loc_match = re.search(pattern, context)
            if loc_match:
                city = loc_match.group(1).strip()
                state = loc_match.group(2).strip()
                break

        date_posted = None
        date_match = re.search(r"•(Today|\d+\s*days?\s*ago)", context, re.I)
        if date_match:
            date_posted = _parse_dice_relative_date(date_match.group(1))

        jobs.append(
            _dice_job_to_dict(
                title=title,
                company_name=company_name,
                city=city,
                state=state,
                job_url=job_url,
                date_posted=date_posted,
                job_id=uuid if re.fullmatch(r"[0-9a-f-]{8,}", uuid, re.I) else None,
            )
        )

    if jobs:
        return jobs

    # Legacy fallback for older Dice markup snapshots used in tests.
    detail_aria_pattern = (
        r'data-testid="job-search-job-detail-link"[^>]*aria-label="([^"]+)"'
    )
    titles = re.findall(detail_aria_pattern, html or "")
    url_pattern = (
        r'href="((?:https://www\.dice\.com)?/(?:job-detail|direct-apply)/[0-9a-f-]+)"'
    )
    urls = re.findall(url_pattern, html or "", flags=re.I)

    for i, raw_url in enumerate(urls):
        job_url = urljoin("https://www.dice.com", raw_url)
        uuid = job_url.rstrip("/").split("/")[-1]
        short_id = uuid[:8]
        if short_id in seen_ids or job_url in seen_urls:
            continue
        title = _clean_dice_title(titles[i] if i < len(titles) else "")
        if not title:
            continue
        seen_ids.add(short_id)
        seen_urls.add(job_url)

        url_anchor = 'href="' + raw_url + '"'
        anchor_match = re.search(re.escape(url_anchor), html or "")
        if not anchor_match:
            continue
        start_pos = anchor_match.start()
        context = (html or "")[max(0, start_pos - 500) : min(len(html or ""), start_pos + 2000)]
        company_name = _company_from_html_context(context)
        jobs.append(
            _dice_job_to_dict(
                title=title,
                company_name=company_name,
                job_url=job_url,
                job_id=uuid if re.fullmatch(r"[0-9a-f-]{8,}", uuid, re.I) else None,
            )
        )
    return jobs


def _fetch_dice_jobs_api(
    search_term: str,
    location: Optional[str],
    results_wanted: int,
    hours_old: Optional[int],
    *,
    timeout_seconds: float,
) -> List[dict]:
    page_size = min(DICE_JOBS_PER_PAGE, max(1, results_wanted))
    max_pages = max(1, (results_wanted + page_size - 1) // page_size) + 1
    collected: List[dict] = []
    seen_ids: set[str] = set()
    headers = {
        "User-Agent": DEFAULT_DICE_USER_AGENT,
        "Accept": "application/json",
        "x-api-key": _dice_api_key(),
    }

    with requests.Session() as session:
        session.headers.update(headers)
        page = 1
        while len(collected) < results_wanted and page <= max_pages:
            params: dict[str, Any] = {
                "q": _normalize_dice_api_query(search_term),
                "page": page,
                "pageSize": page_size,
                "countryCode2": _dice_country_code(),
            }
            loc = (location or "").strip()
            if loc:
                params["location"] = loc
                params["radius"] = str(getattr(settings, "DICE_RADIUS_MILES", 30) or 30)
                params["radiusUnit"] = "mi"
            posted = _dice_posted_date_filter(hours_old)
            if posted:
                params["filters.postedDate"] = posted

            try:
                response = session.get(
                    DICE_SEARCH_API_URL,
                    params=params,
                    timeout=timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as e:
                logger.warning("Dice API fetch failed page=%s: %s", page, e)
                if page == 1:
                    raise
                break

            items = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(items, list) or not items:
                logger.info("[dice_client] API returned no jobs on page %s", page)
                break

            added = 0
            for item in items:
                row = _api_item_to_dict(item)
                if not row:
                    continue
                eid = row["external_id"]
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                collected.append(row)
                added += 1
                if len(collected) >= results_wanted:
                    break

            if added == 0:
                break
            page += 1
            if page <= max_pages and len(collected) < results_wanted:
                time.sleep(DICE_PAGE_DELAY_SECONDS)

    return collected[:results_wanted]


def _fetch_dice_jobs_html(
    search_term: str,
    location: Optional[str],
    results_wanted: int,
    hours_old: Optional[int],
    *,
    timeout_seconds: float,
) -> List[dict]:
    seen_urls: set[str] = set()
    collected: List[dict] = []
    page = 1
    max_pages = max(1, (results_wanted + DICE_JOBS_PER_PAGE - 1) // DICE_JOBS_PER_PAGE) + 2

    with requests.Session() as session:
        session.headers.update({"User-Agent": DEFAULT_DICE_USER_AGENT})

        while len(collected) < results_wanted and page <= max_pages:
            params: dict = {
                "q": search_term.strip(),
                "location": (location or "").strip(),
                "page": page,
                "pageSize": DICE_JOBS_PER_PAGE,
            }
            posted = _dice_posted_date_param(hours_old)
            if posted:
                params["postedDate"] = posted

            page_jobs: List[dict] = []
            try:
                with session.get(
                    DICE_SEARCH_URL,
                    params=params,
                    timeout=timeout_seconds,
                ) as response:
                    response.raise_for_status()
                    page_jobs = _parse_jobs_from_html(response.text, seen_urls)
            except requests.RequestException as e:
                logger.warning("Dice HTML fetch failed page=%s: %s", page, e)
                break
            if not page_jobs:
                logger.info("[dice_client] No HTML jobs on page %s", page)
                break
            collected.extend(page_jobs)
            page += 1
            if page <= max_pages and len(collected) < results_wanted:
                time.sleep(DICE_HTML_PAGE_DELAY_SECONDS)

    return collected[:results_wanted]


def fetch_dice_jobs(
    search_term: str,
    location: Optional[str] = None,
    results_wanted: int = 20,
    hours_old: Optional[int] = None,
    *,
    timeout_seconds: float = 10.0,
) -> List[dict]:
    """
    Fetch Dice jobs via JSON search API, falling back to HTML scrape.
    Returns list of normalized job dicts.
    """
    if not search_term or not search_term.strip():
        raise ValueError("search_term is required for Dice")

    results_wanted = max(1, int(results_wanted))
    collected: List[dict] = []

    try:
        collected = _fetch_dice_jobs_api(
            search_term,
            location,
            results_wanted,
            hours_old,
            timeout_seconds=timeout_seconds,
        )
    except Exception as e:
        logger.warning("[dice_client] API path failed, trying HTML fallback: %s", e)

    if not collected:
        collected = _fetch_dice_jobs_html(
            search_term,
            location,
            results_wanted,
            hours_old,
            timeout_seconds=timeout_seconds,
        )

    logger.info(
        "[dice_client] Fetched %d Dice job(s) (wanted %d)",
        len(collected),
        results_wanted,
    )
    return collected[:results_wanted]


def extract_dice_guid(url_or_id: str) -> Optional[str]:
    """Extract a Dice job-detail GUID from a URL, external_id, or raw UUID."""
    raw = (url_or_id or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", raw, re.I):
        return raw.lower()
    m = _DICE_GUID_RE.search(raw)
    return m.group(1).lower() if m else None


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


def _iter_ld_json_objects(html: str):
    for match in _LD_JSON_RE.finditer(html or ""):
        raw = (match.group(1) or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    yield item
        elif isinstance(data, dict):
            # Some pages wrap entities in @graph
            graph = data.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    if isinstance(item, dict):
                        yield item
            yield data


def _parse_jobposting_from_html(html: str) -> Optional[dict]:
    for item in _iter_ld_json_objects(html):
        types = item.get("@type")
        type_list = types if isinstance(types, list) else [types]
        type_list = [str(t or "") for t in type_list]
        if "JobPosting" not in type_list:
            continue
        title = _clean_dice_title(str(item.get("title") or ""))
        description = _html_to_text(str(item.get("description") or ""))
        if not description:
            continue
        company = ""
        org = item.get("hiringOrganization")
        if isinstance(org, dict):
            company = str(org.get("name") or "").strip()
        location = ""
        loc = item.get("jobLocation")
        if isinstance(loc, list) and loc:
            loc = loc[0]
        if isinstance(loc, dict):
            addr = loc.get("address") if isinstance(loc.get("address"), dict) else loc
            if isinstance(addr, dict):
                parts = [
                    str(addr.get("addressLocality") or "").strip(),
                    str(addr.get("addressRegion") or "").strip(),
                    str(addr.get("addressCountry") or "").strip(),
                ]
                location = ", ".join(p for p in parts if p)
        return {
            "title": title or "Untitled",
            "company_name": company or "Unknown",
            "location": location,
            "description": description,
            "date_posted": item.get("datePosted"),
        }
    return None


def fetch_dice_job_detail(
    url_or_guid: str,
    *,
    timeout_seconds: float = 15.0,
) -> dict:
    """
    Fetch the full Dice job-detail page and return normalized fields.

    Uses JSON-LD JobPosting (includes full description). Raises ValueError/RuntimeError
    on invalid URL or fetch/parse failure.
    """
    guid = extract_dice_guid(url_or_guid)
    if not guid:
        raise ValueError("Not a Dice job-detail URL")

    detail_url = DICE_JOB_DETAIL_URL.format(guid=guid)
    try:
        response = requests.get(
            detail_url,
            headers={"User-Agent": DEFAULT_DICE_USER_AGENT, "Accept": "text/html"},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Dice job-detail fetch failed: {e}") from e

    parsed = _parse_jobposting_from_html(response.text)
    if not parsed:
        raise RuntimeError("Dice job-detail page did not include a JobPosting description")

    out = {
        "title": parsed["title"],
        "company_name": parsed["company_name"],
        "location": parsed["location"],
        "description": parsed["description"],
        "job_url": detail_url,
        "source": "dice",
        "external_id": f"dice:{guid}",
    }
    posted = _parse_date_posted(parsed.get("date_posted"))
    if posted is not None:
        out["date_posted"] = posted
    return out


def enrich_dice_job_listing_description(job, *, timeout_seconds: float = 15.0) -> str:
    """
    If ``job`` is a Dice listing with a short/empty description, fetch the full
    job-detail text, persist it, and return the enriched description.
    """
    from ...models import JobListing

    if not isinstance(job, JobListing):
        return ""
    current = (job.description or "").strip()
    if (job.source or "").strip().lower() != "dice":
        return current
    if len(current) >= DICE_SUMMARY_ENRICH_THRESHOLD:
        return current

    url = (job.url or "").strip() or (job.external_id or "")
    try:
        detail = fetch_dice_job_detail(url, timeout_seconds=timeout_seconds)
    except Exception as e:
        logger.warning("[dice_client] enrich failed for job_id=%s: %s", job.id, e)
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
        "[dice_client] Enriched Dice job_id=%s description %d -> %d chars",
        job.id,
        len(current),
        len(full),
    )
    return full
