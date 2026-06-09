"""
Fetch job listings from the Adzuna REST API.
https://developer.adzuna.com/docs/search
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

import requests
from django.conf import settings

from .job_sources import _parse_date_posted

logger = logging.getLogger(__name__)

ADZUNA_API_ROOT = "https://api.adzuna.com/v1/api"


def _adzuna_credentials() -> tuple[str, str]:
    app_id = getattr(settings, "ADZUNA_APP_ID", "") or ""
    app_key = getattr(settings, "ADZUNA_APP_KEY", "") or ""
    return str(app_id).strip(), str(app_key).strip()


def _adzuna_external_id(country: str, job_id: Any) -> str:
    return f"adzuna:{country}:{job_id}"


def _adzuna_result_to_dict(result: dict, country: str) -> dict:
    company = result.get("company") or {}
    if isinstance(company, dict):
        company_name = str(company.get("display_name") or "").strip()
    else:
        company_name = str(company or "").strip()

    loc = result.get("location") or {}
    if isinstance(loc, dict):
        location = str(loc.get("display_name") or "").strip()
    else:
        location = str(loc or "").strip()

    job_id = result.get("id")
    title = str(result.get("title") or "").strip() or "Untitled"
    description = str(result.get("description") or "").strip()
    job_url = str(result.get("redirect_url") or result.get("url") or "").strip()

    out = {
        "title": title,
        "company_name": company_name or "Unknown",
        "location": location,
        "description": description,
        "job_url": job_url,
        "source": "adzuna",
        "external_id": _adzuna_external_id(country, job_id),
    }
    created = result.get("created")
    if created:
        parsed = _parse_date_posted(created)
        if parsed is not None:
            out["date_posted"] = parsed
    return out


def fetch_adzuna_jobs(
    search_term: str,
    location: Optional[str] = None,
    results_wanted: int = 20,
    *,
    timeout_seconds: float = 10.0,
) -> List[dict]:
    """
    Query Adzuna job search API. Requires ADZUNA_APP_ID and ADZUNA_APP_KEY in settings.
    """
    app_id, app_key = _adzuna_credentials()
    if not app_id or not app_key:
        raise RuntimeError(
            "Adzuna API keys not configured. Set ADZUNA_APP_ID and ADZUNA_APP_KEY in .env"
        )

    if not search_term or not search_term.strip():
        raise ValueError("search_term is required for Adzuna")

    country = (getattr(settings, "ADZUNA_COUNTRY", "us") or "us").strip().lower()
    max_pages = max(1, int(getattr(settings, "ADZUNA_MAX_PAGES", 3)))
    results_per_page = min(50, max(1, results_wanted))

    collected: List[dict] = []
    page = 1

    while len(collected) < results_wanted and page <= max_pages:
        url = f"{ADZUNA_API_ROOT}/jobs/{country}/search/{page}"
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "what": search_term.strip(),
            "results_per_page": results_per_page,
            "content-type": "application/json",
        }
        where = (location or "").strip()
        if where:
            params["where"] = where

        try:
            response = requests.get(url, params=params, timeout=timeout_seconds)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            logger.warning("Adzuna fetch failed page=%s: %s", page, e)
            raise RuntimeError(f"Adzuna job fetch failed: {e}") from e

        results = data.get("results") or []
        if not results:
            break
        for item in results:
            if len(collected) >= results_wanted:
                break
            if isinstance(item, dict):
                collected.append(_adzuna_result_to_dict(item, country))
        page += 1

    logger.info(
        "[adzuna_client] Fetched %d Adzuna job(s) (wanted %d, country=%s)",
        len(collected),
        results_wanted,
        country,
    )
    return collected
