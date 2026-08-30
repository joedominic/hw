"""JobSpy (Indeed, LinkedIn, Glassdoor, ZipRecruiter) Adapter translating to RawJobDTOs."""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..ports import JobSourcePort, RawJobDTO

logger = logging.getLogger(__name__)


class JobSpyJobAdapter(JobSourcePort):
    def __init__(self, site_name: str = "indeed"):
        self._site_name = site_name.strip().lower()

    @property
    def source_name(self) -> str:
        return f"jobspy_{self._site_name}"

    def fetch_jobs(
        self,
        search_term: str,
        location: Optional[str] = None,
        limit: int = 20,
        hours_old: Optional[int] = None,
        **kwargs: Any,
    ) -> list[RawJobDTO]:
        from ...job_sources import _fetch_jobs_jobspy

        country = kwargs.get("country_indeed", "USA")
        timeout = kwargs.get("timeout_seconds", 10.0)
        max_retries = kwargs.get("max_retries", 3)

        raw_rows = _fetch_jobs_jobspy(
            search_term=search_term,
            location=location,
            jobspy_sites=[self._site_name],
            results_wanted=limit,
            country_indeed=country,
            hours_old=hours_old,
            timeout_seconds=timeout,
            max_retries=max_retries,
        )
        dtos: list[RawJobDTO] = []
        for r in raw_rows:
            dtos.append(
                RawJobDTO.create(
                    source=r.get("source") or self.source_name,
                    title=r.get("title") or "Untitled",
                    company_name=r.get("company_name") or "Unknown",
                    url=r.get("job_url") or "",
                    location=r.get("location") or "",
                    description=r.get("description") or "",
                    posted_at=r.get("date_posted"),
                    external_id=r.get("external_id"),
                    raw_json=r.get("raw_json"),
                )
            )
        return dtos
