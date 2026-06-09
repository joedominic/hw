"""
Fetch job listings from Dice.com (not supported by upstream python-jobspy).
Returns normalized dicts compatible with job_sources.upsert_job_listing_from_fetch.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import date, datetime, time as time_cls, timedelta
from typing import List, Optional

import requests
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

logger = logging.getLogger(__name__)

DEFAULT_DICE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _external_id(title: str, company: str, job_url: str) -> str:
    raw = f"{title}|{company}|{job_url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


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

DICE_SEARCH_URL = "https://www.dice.com/jobs"
DICE_JOBS_PER_PAGE = 20
DICE_PAGE_DELAY_SECONDS = 2.0


def _dice_posted_date_param(hours_old: Optional[int]) -> Optional[str]:
    if not hours_old or hours_old <= 0:
        return None
    if hours_old <= 24:
        return "Today"
    if hours_old <= 72:
        return "Last 3 Days"
    return "Last 7 Days"


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


def _dice_job_to_dict(
    *,
    title: str,
    company_name: Optional[str],
    city: Optional[str],
    state: Optional[str],
    job_url: str,
    date_posted: Optional[date],
) -> dict:
    location = _location_display(city, state)
    company = (company_name or "").strip() or "Unknown"
    title = (title or "").strip() or "Untitled"
    out = {
        "title": title,
        "company_name": company,
        "location": location,
        "description": "",
        "job_url": job_url,
        "source": "dice",
        "external_id": _external_id(title, company, job_url),
    }
    if date_posted is not None:
        dt = datetime.combine(date_posted, datetime.min.time())
        parsed = _parse_date_posted(dt)
        if parsed is not None:
            out["date_posted"] = parsed
    return out


def _parse_jobs_from_html(html: str, seen_urls: set[str]) -> List[dict]:
    jobs: List[dict] = []
    detail_aria_pattern = (
        r'data-testid="job-search-job-detail-link"[^>]*aria-label="([^"]+)"'
    )
    titles = re.findall(detail_aria_pattern, html)
    url_pattern = r'href="(https://www\.dice\.com/job-detail/[0-9a-f-]+)"'
    urls = re.findall(url_pattern, html)
    seen_ids: set[str] = set()

    for i, job_url in enumerate(urls):
        uuid = job_url.split("/")[-1]
        short_id = uuid[:8]
        if short_id in seen_ids or job_url in seen_urls:
            continue
        seen_ids.add(short_id)
        seen_urls.add(job_url)

        title = titles[i] if i < len(titles) else ""
        if title and title[0].isdigit():
            for j, c in enumerate(title):
                if c.isalpha():
                    title = title[j:]
                    break

        url_anchor = 'href="' + job_url + '"'
        match = re.search(re.escape(url_anchor), html)
        if not match:
            continue
        start_pos = match.start()
        context = html[max(0, start_pos - 2000) : min(len(html), start_pos + 2000)]

        company_name = None
        company_match = re.search(r"companyname=([^\"&]+)", context)
        if company_match:
            company_name = company_match.group(1).replace("%20", " ")

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
            )
        )
    return jobs


def fetch_dice_jobs(
    search_term: str,
    location: Optional[str] = None,
    results_wanted: int = 20,
    hours_old: Optional[int] = None,
    *,
    timeout_seconds: float = 10.0,
) -> List[dict]:
    """
    Scrape Dice job search HTML. Returns list of normalized job dicts.
    """
    if not search_term or not search_term.strip():
        raise ValueError("search_term is required for Dice")

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_DICE_USER_AGENT})
    seen_urls: set[str] = set()
    collected: List[dict] = []
    page = 1
    max_pages = max(1, (results_wanted + DICE_JOBS_PER_PAGE - 1) // DICE_JOBS_PER_PAGE) + 2

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

        try:
            response = session.get(
                DICE_SEARCH_URL,
                params=params,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as e:
            logger.warning("Dice fetch failed page=%s: %s", page, e)
            break

        page_jobs = _parse_jobs_from_html(response.text, seen_urls)
        if not page_jobs:
            logger.info("[dice_client] No jobs on page %s", page)
            break
        collected.extend(page_jobs)
        page += 1
        if page <= max_pages and len(collected) < results_wanted:
            time.sleep(DICE_PAGE_DELAY_SECONDS)

    logger.info("[dice_client] Fetched %d Dice job(s) (wanted %d)", len(collected), results_wanted)
    return collected[:results_wanted]
