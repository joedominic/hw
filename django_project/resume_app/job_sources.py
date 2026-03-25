"""
Fetch job listings using the free JobSpy library (Indeed, LinkedIn, Glassdoor, Google, ZipRecruiter).
Returns a list of normalized dicts for upsert into JobListing.
"""
import hashlib
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default sites: all JobSpy boards (concurrent per-site scrape). Matches jobspy.model.Site string values.
DEFAULT_SITE_NAMES = [
    "indeed",
    "linkedin",
    "zip_recruiter",
    "glassdoor",
    "google",
]
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
    return {
        "title": title or "Untitled",
        "company_name": company or "Unknown",
        "location": location,
        "description": description,
        "job_url": job_url,
        "source": source,
        "external_id": external_id,
    }


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

    kwargs = {
        "site_name": sites,
        "search_term": search_term,
        "results_wanted": results_wanted,
        "country_indeed": country_indeed,
        # Forward timeout to JobSpy/requests so calls don't hang forever when remote APIs stall.
        "request_timeout": timeout_seconds,
    }
    # LinkedIn returns truncated/no description unless this is explicitly enabled.
    if any(str(s).strip().lower() == "linkedin" for s in sites):
        kwargs["linkedin_fetch_description"] = True
    if location:
        kwargs["location"] = location
    if hours_old is not None:
        kwargs["hours_old"] = hours_old
    if google_search_term is not None:
        kwargs["google_search_term"] = google_search_term

    # Retry with simple exponential backoff. Detect ReadTimeout from Indeed and allow
    # the caller to distinguish partial results vs complete failure.
    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            df = scrape_jobs(**kwargs)
            break
        except Exception as e:  # pragma: no cover - network-dependent
            last_err = e
            msg = str(e)
            logger.warning("JobSpy scrape_jobs failed (attempt %s/%s): %s", attempt + 1, max_retries, msg)
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
    return out
