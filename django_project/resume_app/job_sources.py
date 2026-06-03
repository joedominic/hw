"""
Fetch job listings using the free JobSpy library (Indeed, LinkedIn, Glassdoor, Google, ZipRecruiter).
Returns a list of normalized dicts for upsert into JobListing.
"""
import hashlib
import logging
import math
from datetime import date, datetime, time, timedelta
from typing import Any, List, Optional, Tuple, TYPE_CHECKING

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

if TYPE_CHECKING:
    from .models import JobListing

logger = logging.getLogger(__name__)

# Default sites: JobSpy boards (concurrent per-site scrape). Matches jobspy.model.Site string values.
# Glassdoor is omitted by default: its location API often returns 400 / fails to parse US cities,
# which can abort the whole multi-site scrape. Enable per-task via the site checkboxes if needed.
DEFAULT_SITE_NAMES = [
    "indeed",
    "linkedin",
    "zip_recruiter",
    "google",
]

# Passed to JobSpy scrapers; stale defaults are often blocked (especially Glassdoor).
DEFAULT_JOBSPY_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_RESULTS_WANTED = 20
DEFAULT_COUNTRY_INDEED = "USA"


def _normalize_site_name(site: str) -> str:
    """Map JobSpy SITE column to our source string, e.g. jobspy_indeed."""
    s = (site or "").strip().lower().replace(" ", "_")
    if not s:
        return "jobspy_unknown"
    if s == "zip_recruiter":
        return "jobspy_ziprecruiter"
    return f"jobspy_{s}"


def _external_id(row: dict) -> str:
    """Stable id for deduplication: hash of title|company|job_url."""
    title = str(row.get("title") or "").strip()
    company = str(row.get("company") or row.get("company_name") or "").strip()
    url = str(row.get("job_url") or row.get("url") or "").strip()
    raw = f"{title}|{company}|{url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


def _row_to_dict(row: Any, site: str) -> dict:
    """Convert a DataFrame row (or dict) to our common shape."""
    if hasattr(row, "to_dict"):
        row = row.to_dict()
    title = str(row.get("title") or row.get("TITLE") or "").strip()
    company = str(row.get("company") or row.get("COMPANY") or row.get("company_name") or "").strip()
    location = str(
        row.get("location")
        or row.get("LOCATION")
        or row.get("city")
        or row.get("CITY")
        or ""
    ).strip()
    if not location and (row.get("state") or row.get("STATE")):
        loc_part = str(row.get("state") or row.get("STATE")).strip()
        if location:
            location = f"{location}, {loc_part}"
        else:
            location = loc_part
    description = str(
        row.get("description")
        or row.get("DESCRIPTION")
        or row.get("job_description")
        or row.get("JOB_DESCRIPTION")
        or row.get("linkedin_description")
        or row.get("LINKEDIN_DESCRIPTION")
        or row.get("description_html")
        or row.get("DESCRIPTION_HTML")
        or row.get("summary")
        or row.get("SUMMARY")
        or row.get("description_text")
        or row.get("DESCRIPTION_TEXT")
        or row.get("snippet")
        or row.get("SNIPPET")
        or ""
    ).strip()
    job_url = str(row.get("job_url") or row.get("JOB_URL") or row.get("url") or "").strip()
    source = _normalize_site_name(site)
    external_id = _external_id({"title": title, "company": company, "job_url": job_url})
    date_posted = _parse_date_posted(
        row.get("date_posted")
        or row.get("DATE_POSTED")
        or row.get("date")
        or row.get("DATE")
    )
    out = {
        "title": title or "Untitled",
        "company_name": company or "Unknown",
        "location": location,
        "description": description,
        "job_url": job_url,
        "source": source,
        "external_id": external_id,
    }
    if date_posted is not None:
        out["date_posted"] = date_posted
    return out


def _parse_date_posted(val: Any) -> Optional[datetime]:
    """Normalize JobSpy date_posted to an aware datetime, or None if missing/invalid."""
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
        return None
    if hasattr(val, "to_pydatetime"):
        val = val.to_pydatetime()
    if isinstance(val, date) and not isinstance(val, datetime):
        val = datetime.combine(val, time.min)
    if isinstance(val, datetime):
        if timezone.is_naive(val):
            return timezone.make_aware(val, timezone.get_current_timezone())
        return val
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "<na>", "nat"):
        return None
    parsed = parse_datetime(s)
    if parsed is None:
        d = parse_date(s)
        if d is not None:
            parsed = datetime.combine(d, time.min)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def filter_rows_by_max_age(rows: List[dict], hours_old: int) -> List[dict]:
    """Drop rows with date_posted older than hours_old (keeps rows with no date)."""
    if not hours_old or hours_old <= 0:
        return rows
    cutoff = timezone.now() - timedelta(hours=hours_old)
    kept: List[dict] = []
    dropped = 0
    for row in rows:
        posted = row.get("date_posted")
        if posted is not None and posted < cutoff:
            dropped += 1
            continue
        kept.append(row)
    if dropped:
        logger.info(
            "[job_sources] Dropped %d job(s) with date_posted older than %d hours",
            dropped,
            hours_old,
        )
    return kept


def _scalar_fetch_value(val: Any) -> Any:
    """Coerce JobSpy/pandas sentinels to DB-safe scalars; drop explicit nulls."""
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
        return None
    s = str(val).strip()
    if s.lower() in ("nan", "none", "<na>", "nat"):
        return None
    return val


def upsert_job_listing_from_fetch(row: dict) -> Tuple["JobListing", bool]:
    """
    Upsert a normalized JobSpy row into JobListing.

    Uses get + QuerySet.update/create instead of update_or_create: Django 5 adds
    auto_now_add fields (e.g. fetched_at) to every update_or_create save. Rows with
    legacy empty fetched_at ('' in SQLite) load as None and then get written back as
    NULL, triggering NOT NULL constraint failures.
    """
    from django.utils import timezone

    from .models import JobListing

    desc = _scalar_fetch_value(row.get("description")) or ""
    defaults = {
        "title": _scalar_fetch_value(row.get("title")) or "Untitled",
        "company_name": _scalar_fetch_value(row.get("company_name")) or "Unknown",
        "location": _scalar_fetch_value(row.get("location")) or "",
        "description": desc,
        "url": _scalar_fetch_value(row.get("job_url")) or "",
    }
    posted = row.get("date_posted")
    if posted is not None:
        defaults["posted_at"] = posted
    defaults = {k: v for k, v in defaults.items() if v is not None}

    lookup = {"source": row["source"], "external_id": row["external_id"]}
    updated = JobListing.objects.filter(**lookup).update(**defaults)
    if updated:
        job = JobListing.objects.get(**lookup)
        if not job.fetched_at:
            JobListing.objects.filter(pk=job.pk).update(fetched_at=timezone.now())
            job.refresh_from_db()
        return job, False

    return (
        JobListing.objects.create(**lookup, **defaults, fetched_at=timezone.now()),
        True,
    )


def fetch_jobs(
    search_term: str,
    location: Optional[str] = None,
    site_name: Optional[list] = None,
    results_wanted: int = DEFAULT_RESULTS_WANTED,
    country_indeed: str = DEFAULT_COUNTRY_INDEED,
    hours_old: Optional[int] = None,
    google_search_term: Optional[str] = None,
    *,
    timeout_seconds: float = 10.0,
    max_retries: int = 3,
) -> list[dict]:
    """
    Fetch jobs using JobSpy. Returns list of dicts with keys:
    title, company_name, location, description, job_url, source, external_id.

    Adds a simple retry/backoff loop and special handling for Indeed ReadTimeouts:
    - On ReadTimeout from Indeed, retries up to max_retries with exponential backoff.
    - If Indeed keeps timing out and other sites are configured, falls back to
      those sites only and raises a RuntimeError with a clear message so the
      caller can surface a warning banner.
    """
    try:
        from jobspy import scrape_jobs
    except ImportError as e:
        logger.exception("python-jobspy not installed")
        raise ImportError("Install python-jobspy: pip install python-jobspy") from e

    sites = site_name if site_name is not None else DEFAULT_SITE_NAMES
    if isinstance(sites, str):
        sites = [sites]

    if hours_old is None:
        hours_old = getattr(settings, "JOB_SEARCH_HOURS_OLD", None)

    kwargs = {
        "site_name": sites,
        "search_term": search_term,
        "results_wanted": results_wanted,
        "country_indeed": country_indeed,
        # Forward timeout to JobSpy/requests so calls don't hang forever when remote APIs stall.
        "request_timeout": timeout_seconds,
        "user_agent": DEFAULT_JOBSPY_USER_AGENT,
    }
    # LinkedIn returns truncated/no description unless this is explicitly enabled.
    if any(str(s).strip().lower() == "linkedin" for s in sites):
        kwargs["linkedin_fetch_description"] = True
    if location:
        kwargs["location"] = location
    if hours_old is not None and hours_old > 0:
        kwargs["hours_old"] = int(hours_old)
    if google_search_term is not None:
        kwargs["google_search_term"] = google_search_term

    # Retry with simple exponential backoff. Detect ReadTimeout from Indeed and allow
    # the caller to distinguish partial results vs complete failure.
    last_err: Optional[Exception] = None
    glassdoor_stripped = False
    for attempt in range(max_retries):
        try:
            df = scrape_jobs(**kwargs)
            break
        except Exception as e:  # pragma: no cover - network-dependent
            last_err = e
            msg = str(e)
            logger.warning("JobSpy scrape_jobs failed (attempt %s/%s): %s", attempt + 1, max_retries, msg)
            # One-shot: Glassdoor location/API failures can abort the entire concurrent scrape.
            if not glassdoor_stripped:
                cur = kwargs.get("site_name") or []
                if isinstance(cur, str):
                    cur = [cur]
                non_gd = [s for s in cur if str(s).strip().lower() != "glassdoor"]
                if len(non_gd) < len(cur) and non_gd:
                    glassdoor_stripped = True
                    kwargs["site_name"] = non_gd
                    sites = non_gd
                    logger.warning(
                        "Retrying JobSpy without Glassdoor after error (Glassdoor often breaks multi-site runs)."
                    )
                    continue
            # ReadTimeout from Indeed: let caller know specifically
            if "apis.indeed.com" in msg and "Read timed out" in msg:
                # If there are non-Indeed sites configured, fall back to those instead.
                non_indeed_sites = [s for s in sites if str(s).lower() != "indeed"]
                if non_indeed_sites:
                    logger.warning(
                        "Indeed timed out; retrying without Indeed. sites_before=%s, sites_after=%s",
                        sites,
                        non_indeed_sites,
                    )
                    kwargs["site_name"] = non_indeed_sites
                    sites = non_indeed_sites
                    # On next loop iteration we'll retry without Indeed.
                else:
                    # No alternative sites; propagate a clear error
                    raise RuntimeError("Indeed timed out; please try again later.") from e
            # Generic retry with backoff
            import time as _time

            if attempt < max_retries - 1:
                delay = 2 ** attempt
                _time.sleep(delay)
            else:
                logger.exception("JobSpy scrape_jobs failed after %s attempts", max_retries)
                raise RuntimeError(f"Job fetch failed: {e}") from e

    if df is None or df.empty:
        return []

    out = []
    site_col = "SITE" if "SITE" in df.columns else "site"
    for _, row in df.iterrows():
        site = str(row.get(site_col, "unknown")).strip() if site_col in df.columns else "unknown"
        out.append(_row_to_dict(row, site))
    if hours_old is not None and hours_old > 0:
        out = filter_rows_by_max_age(out, int(hours_old))
    return out
