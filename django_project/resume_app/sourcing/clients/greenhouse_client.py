"""
Fetch job listings directly from Greenhouse.io public boards API.
Supports multi-board querying, keyword & location filtering, full description enrichment,
and direct job application URL parsing.
"""
from __future__ import annotations

import html as html_lib
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

DEFAULT_GREENHOUSE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

GREENHOUSE_API_ROOT = "https://boards-api.greenhouse.io/v1"
GREENHOUSE_SUMMARY_ENRICH_THRESHOLD = 500

# Curated list of verified active top-tier tech companies using Greenhouse boards
DEFAULT_GREENHOUSE_BOARDS: list[str] = [
    "stripe",
    "figma",
    "anthropic",
    "databricks",
    "datadog",
    "cloudflare",
    "coinbase",
    "elastic",
    "mongodb",
    "brex",
    "samsara",
    "gitlab",
    "scaleai",
    "affirm",
    "robinhood",
    "pinterest",
    "reddit",
    "instacart",
    "gusto",
    "vercel",
    "duolingo",
    "carta",
    "chime",
    "discord",
    "twitch",
    "dropbox",
    "benchling",
    "cockroachlabs",
    "airtable",
    "ramp",
    "chainalysis",
    "rippling",
    "automattic",
    "zapier",
    "webflow",
    "postman",
    "plaid",
    "hashicorp",
]

KNOWN_COMPANY_NAMES: dict[str, str] = {
    "stripe": "Stripe",
    "figma": "Figma",
    "anthropic": "Anthropic",
    "databricks": "Databricks",
    "datadog": "Datadog",
    "cloudflare": "Cloudflare",
    "coinbase": "Coinbase",
    "elastic": "Elastic",
    "mongodb": "MongoDB",
    "brex": "Brex",
    "samsara": "Samsara",
    "gitlab": "GitLab",
    "scaleai": "Scale AI",
    "affirm": "Affirm",
    "robinhood": "Robinhood",
    "pinterest": "Pinterest",
    "reddit": "Reddit",
    "instacart": "Instacart",
    "gusto": "Gusto",
    "vercel": "Vercel",
    "duolingo": "Duolingo",
    "carta": "Carta",
    "chime": "Chime",
    "discord": "Discord",
    "twitch": "Twitch",
    "dropbox": "Dropbox",
    "benchling": "Benchling",
    "cockroachlabs": "Cockroach Labs",
    "airtable": "Airtable",
    "ramp": "Ramp",
    "chainalysis": "Chainalysis",
    "rippling": "Rippling",
    "automattic": "Automattic",
    "zapier": "Zapier",
    "webflow": "Webflow",
    "postman": "Postman",
    "plaid": "Plaid",
    "hashicorp": "HashiCorp",
}


def _greenhouse_user_agent() -> str:
    return (getattr(settings, "GREENHOUSE_USER_AGENT", "") or "").strip() or DEFAULT_GREENHOUSE_USER_AGENT


def _greenhouse_timeout() -> float:
    return float(getattr(settings, "GREENHOUSE_TIMEOUT_SECONDS", 6.0) or 6.0)


def _configured_greenhouse_boards() -> list[str]:
    cfg = getattr(settings, "GREENHOUSE_BOARDS", None)
    if isinstance(cfg, list) and cfg:
        return [str(b).strip().lower() for b in cfg if str(b).strip()]
    return list(DEFAULT_GREENHOUSE_BOARDS)


def resolve_company_name(board_slug: str, api_company_name: Optional[str] = None) -> str:
    """Return formatted company name, prioritizing API returned name, known mapping, or titleized slug."""
    if api_company_name and api_company_name.strip():
        return api_company_name.strip()
    slug = (board_slug or "").strip().lower()
    if slug in KNOWN_COMPANY_NAMES:
        return KNOWN_COMPANY_NAMES[slug]
    return slug.replace("-", " ").replace("_", " ").title() or "Unknown Company"


def extract_greenhouse_job_id_and_board(url_or_id: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract (board_slug, job_id) from Greenhouse URLs or identifiers.
    Examples:
      - https://boards.greenhouse.io/figma/jobs/5426468004
      - https://job-boards.greenhouse.io/stripe/jobs/8113337?gh_jid=8113337
      - https://boards.greenhouse.io/embed/job_app?for=reddit&token=998877
      - greenhouse:anthropic:554433
      - https://stripe.com/jobs/search?gh_jid=8113337
    """
    s = (url_or_id or "").strip()
    if not s:
        return None, None

    # greenhouse:board:job_id format
    m = re.search(r"^greenhouse:([a-zA-Z0-9_\-]+):(\d+)$", s, re.IGNORECASE)
    if m:
        return m.group(1).lower(), m.group(2)

    # Standard boards.greenhouse.io or job-boards.greenhouse.io /<board>/jobs/<id>
    m = re.search(r"(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)/jobs/(\d+)", s, re.IGNORECASE)
    if m:
        return m.group(1).lower(), m.group(2)

    # Embed format: embed/job_app?for=board&token=id
    if "greenhouse.io/embed/job_app" in s:
        parsed = urlparse(s)
        qs = parse_qs(parsed.query)
        board = qs.get("for", [None])[0]
        token = qs.get("token", [None])[0]
        if board and token:
            return board.lower(), str(token)

    # Query param gh_jid=12345
    if "gh_jid=" in s:
        m_jid = re.search(r"gh_jid=(\d+)", s)
        if m_jid:
            jid = m_jid.group(1)
            # Try to infer board from domain
            parsed = urlparse(s)
            domain_part = parsed.netloc.split(".")[0].lower()
            if domain_part and domain_part != "boards" and domain_part != "job-boards":
                return domain_part, jid
            return None, jid

    return None, None


def clean_greenhouse_html(html_str: str) -> str:
    """Unescape HTML entities and convert Greenhouse HTML content to clean readable plain text."""
    if not html_str:
        return ""
    unescaped = html_lib.unescape(html_str)
    try:
        soup = BeautifulSoup(unescaped, "html.parser")
        # Replace line-breaking elements with linebreaks
        for br in soup.find_all(["br", "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"]):
            br.append("\n")
        text = soup.get_text(separator=" ", strip=True)
        # Normalize excessive newlines and whitespace
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    except Exception:
        # Fallback regex strip
        clean = re.sub(r"<[^>]+>", " ", unescaped)
        return re.sub(r"\s+", " ", clean).strip()


def parse_greenhouse_date(val: Any) -> Optional[datetime]:
    """Parse Greenhouse ISO timestamp string to timezone-aware datetime."""
    if not val:
        return None
    try:
        if isinstance(val, datetime):
            return val if timezone.is_aware(val) else timezone.make_aware(val, timezone.utc)
        if isinstance(val, str):
            dt = parse_datetime(val)
            if dt is None:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt, timezone.utc)
            return dt
    except Exception:
        pass
    return None


def fetch_greenhouse_job_detail(url_or_id: str, *, timeout_seconds: Optional[float] = None) -> dict:
    """
    Fetch full detail for a single job listing from Greenhouse's public board API.
    Returns normalized dictionary with keys:
    title, company_name, location, description, job_url, source, external_id, date_posted, raw_json
    """
    board, jid = extract_greenhouse_job_id_and_board(url_or_id)
    if not jid:
        raise ValueError(f"Could not extract Greenhouse job ID from: {url_or_id}")

    if not board:
        raise ValueError(f"Could not extract Greenhouse company/board from: {url_or_id}")

    timeout = timeout_seconds or _greenhouse_timeout()
    headers = {"User-Agent": _greenhouse_user_agent()}
    api_url = f"{GREENHOUSE_API_ROOT}/boards/{board}/jobs/{jid}"

    try:
        resp = requests.get(api_url, headers=headers, timeout=timeout)
        if resp.status_code == 404:
            raise ValueError(f"Greenhouse job not found (HTTP 404) at {api_url}")
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        raise RuntimeError(f"Greenhouse API error fetching job detail: {e}") from e

    title = (data.get("title") or "Untitled").strip()
    company_name = resolve_company_name(board, data.get("company_name"))
    loc_obj = data.get("location") or {}
    location_name = loc_obj.get("name", "").strip() if isinstance(loc_obj, dict) else str(loc_obj or "").strip()
    
    raw_content = data.get("content") or ""
    description_text = clean_greenhouse_html(raw_content)
    
    abs_url = (data.get("absolute_url") or "").strip()
    if not abs_url:
        abs_url = f"https://boards.greenhouse.io/{board}/jobs/{jid}"

    posted_at = parse_greenhouse_date(data.get("first_published") or data.get("updated_at"))

    return {
        "title": title,
        "company_name": company_name,
        "location": location_name,
        "description": description_text,
        "job_url": abs_url,
        "source": "greenhouse",
        "external_id": f"greenhouse:{board}:{jid}",
        "date_posted": posted_at,
        "raw_json": data,
    }


def enrich_greenhouse_job_listing_description(job, *, timeout_seconds: Optional[float] = None) -> str:
    """
    Fetch full detail job text for a Greenhouse job, persist it, and return.
    """
    from ...models import JobListing

    if not isinstance(job, JobListing):
        return ""
    current = (job.description or "").strip()
    if (job.source or "").strip().lower() != "greenhouse":
        return current
    if len(current) >= GREENHOUSE_SUMMARY_ENRICH_THRESHOLD:
        return current

    url = (job.url or "").strip() or (job.external_id or "")
    try:
        detail = fetch_greenhouse_job_detail(url, timeout_seconds=timeout_seconds)
    except Exception as e:
        logger.warning("[greenhouse_client] enrich failed for job_id=%s: %s", getattr(job, "id", None), e)
        return current

    full = (detail.get("description") or "").strip()
    if not full or len(full) <= len(current):
        return current

    updates = {"description": full}
    title = (detail.get("title") or "").strip()
    if title and title != "Untitled" and (not (job.title or "").strip() or (job.title or "").strip() == "Untitled"):
        updates["title"] = title
    company = (detail.get("company_name") or "").strip()
    if company and company != "Unknown" and (not (job.company_name or "").strip() or (job.company_name or "").strip() == "Unknown"):
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
        "[greenhouse_client] Enriched Greenhouse job_id=%s description %d -> %d chars",
        job.id,
        len(current),
        len(full),
    )
    return full


def _matches_query(
    job: dict,
    search_tokens: list[str],
    location_tokens: list[str],
    cutoff_time: Optional[datetime] = None,
) -> Tuple[bool, float]:
    """
    Score a Greenhouse job against search keywords and location constraints.
    Returns (is_match, score).
    """
    # Check date cutoff if provided
    if cutoff_time is not None:
        posted = parse_greenhouse_date(job.get("first_published") or job.get("updated_at"))
        if posted and posted < cutoff_time:
            return False, 0.0

    title = (job.get("title") or "").lower()
    board = (job.get("board") or "").lower()
    company = resolve_company_name(board).lower()

    # Departments text
    depts = " ".join([d.get("name", "") for d in (job.get("departments") or []) if isinstance(d, dict)]).lower()
    
    # Location matching
    loc_obj = job.get("location") or {}
    loc_str = (loc_obj.get("name") if isinstance(loc_obj, dict) else str(loc_obj or "")).lower()
    offices_str = " ".join([o.get("name", "") for o in (job.get("offices") or []) if isinstance(o, dict)]).lower()
    full_loc_context = f"{loc_str} {offices_str}"

    if location_tokens:
        loc_matched = False
        is_remote_query = any("remote" in tok for tok in location_tokens)

        if is_remote_query:
            if "remote" in full_loc_context or "remote" in title:
                loc_matched = True
        else:
            # Check if any location token matches
            for tok in location_tokens:
                if tok in full_loc_context or tok in title:
                    loc_matched = True
                    break
            # Also allow remote if searching for a generic region like US/USA
            if not loc_matched and any(tok in ("us", "usa", "united states") for tok in location_tokens):
                if "remote" in full_loc_context or "united states" in full_loc_context:
                    loc_matched = True

        if not loc_matched:
            return False, 0.0

    # Keyword / Search Term matching
    score = 0.0
    if search_tokens:
        full_text = f"{title} {depts} {board} {company}"
        matched_tokens = 0
        for tok in search_tokens:
            if tok in title:
                score += 10.0
                matched_tokens += 1
            elif tok in full_text:
                score += 4.0
                matched_tokens += 1

        # We require at least 1 keyword match if search_tokens exist
        if matched_tokens == 0:
            return False, 0.0

        # Bonus if entire query phrase appears consecutively in title
        full_query = " ".join(search_tokens)
        if full_query in title:
            score += 25.0
    else:
        score = 1.0

    return True, score


def fetch_greenhouse_jobs(
    search_term: str,
    location: Optional[str] = None,
    results_wanted: int = 20,
    hours_old: Optional[int] = None,
    boards: Optional[list[str]] = None,
    timeout_seconds: Optional[float] = None,
) -> list[dict]:
    """
    Query Greenhouse boards for matching jobs, score & filter, and return normalized dicts.
    """
    timeout = timeout_seconds or _greenhouse_timeout()
    headers = {"User-Agent": _greenhouse_user_agent()}

    # Determine which boards to query
    if boards and len(boards) > 0:
        target_boards = [str(b).strip().lower() for b in boards if str(b).strip()]
    else:
        target_boards = _configured_greenhouse_boards()

    # Prioritize specific company board if user mentions it in search_term
    st_clean = (search_term or "").strip().lower()
    matched_specific_board = None
    for b in DEFAULT_GREENHOUSE_BOARDS:
        company_name = KNOWN_COMPANY_NAMES.get(b, b).lower()
        if b in st_clean or company_name in st_clean:
            matched_specific_board = b
            break

    if matched_specific_board:
        # Move prioritized company to the very front
        target_boards = [matched_specific_board] + [b for b in target_boards if b != matched_specific_board]

    # Pre-tokenize search & location
    stopwords = {"a", "an", "and", "in", "at", "for", "with", "the", "to", "of", "on"}
    search_tokens = [tok for tok in re.findall(r"\w+", st_clean) if tok not in stopwords and len(tok) > 1]
    
    loc_clean = (location or "").strip().lower()
    loc_tokens = [tok for tok in re.findall(r"\w+", loc_clean) if tok not in stopwords and len(tok) > 1]

    cutoff_time = None
    if hours_old is not None and hours_old > 0:
        cutoff_time = timezone.now() - timedelta(hours=int(hours_old))

    # Fetch boards concurrently
    def fetch_single_board(b_slug: str) -> list[dict]:
        api_url = f"{GREENHOUSE_API_ROOT}/boards/{b_slug}/jobs"
        try:
            resp = requests.get(api_url, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                jobs = data.get("jobs", [])
                for j in jobs:
                    j["board"] = b_slug
                return jobs
        except Exception as exc:
            logger.debug("[greenhouse_client] Board %s fetch failed: %s", b_slug, exc)
        return []

    # Limit board concurrency
    max_workers = min(12, max(2, len(target_boards)))
    all_raw_jobs: list[dict] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_single_board, b): b for b in target_boards}
        for future in as_completed(futures):
            try:
                board_jobs = future.result()
                if board_jobs:
                    all_raw_jobs.extend(board_jobs)
            except Exception:
                pass

    # Score and filter candidate jobs
    scored_candidates: list[Tuple[float, dict]] = []
    for job in all_raw_jobs:
        is_match, score = _matches_query(job, search_tokens, loc_tokens, cutoff_time)
        if is_match:
            scored_candidates.append((score, job))

    # Sort candidates by score descending
    scored_candidates.sort(key=lambda x: x[0], reverse=True)
    top_candidates = [job for _, job in scored_candidates[:results_wanted]]

    # For top candidates, enrich full descriptions concurrently if needed
    def enrich_detail(candidate: dict) -> dict:
        board = candidate.get("board", "")
        jid = str(candidate.get("id", ""))
        
        # If candidate already includes full content (e.g. from tests or ?content=true), use it directly
        if candidate.get("content"):
            title = candidate.get("title") or "Untitled"
            company_name = resolve_company_name(board, candidate.get("company_name"))
            loc_obj = candidate.get("location") or {}
            location_name = loc_obj.get("name", "").strip() if isinstance(loc_obj, dict) else str(loc_obj or "").strip()
            abs_url = candidate.get("absolute_url") or f"https://boards.greenhouse.io/{board}/jobs/{jid}"
            posted_at = parse_greenhouse_date(candidate.get("first_published") or candidate.get("updated_at"))
            return {
                "title": title,
                "company_name": company_name,
                "location": location_name,
                "description": clean_greenhouse_html(candidate.get("content")),
                "job_url": abs_url,
                "source": "greenhouse",
                "external_id": f"greenhouse:{board}:{jid}",
                "date_posted": posted_at,
                "raw_json": candidate,
            }

        try:
            detail = fetch_greenhouse_job_detail(f"greenhouse:{board}:{jid}", timeout_seconds=timeout)
            return detail
        except Exception:
            # Fall back to metadata in candidate card
            title = candidate.get("title") or "Untitled"
            company_name = resolve_company_name(board, candidate.get("company_name"))
            loc_obj = candidate.get("location") or {}
            location_name = loc_obj.get("name", "").strip() if isinstance(loc_obj, dict) else str(loc_obj or "").strip()
            abs_url = candidate.get("absolute_url") or f"https://boards.greenhouse.io/{board}/jobs/{jid}"
            posted_at = parse_greenhouse_date(candidate.get("first_published") or candidate.get("updated_at"))
            return {
                "title": title,
                "company_name": company_name,
                "location": location_name,
                "description": f"Role at {company_name} in {location_name}. Visit job link to view full description and apply.",
                "job_url": abs_url,
                "source": "greenhouse",
                "external_id": f"greenhouse:{board}:{jid}",
                "date_posted": posted_at,
                "raw_json": candidate,
            }

    results: list[dict] = []
    if top_candidates:
        enrich_workers = min(8, len(top_candidates))
        with ThreadPoolExecutor(max_workers=enrich_workers) as executor:
            enriched_jobs = list(executor.map(enrich_detail, top_candidates))
            results.extend(enriched_jobs)

    logger.info(
        "[greenhouse_client] Sourced %d matching Greenhouse jobs for query='%s' location='%s'",
        len(results),
        search_term,
        location,
    )
    return results
