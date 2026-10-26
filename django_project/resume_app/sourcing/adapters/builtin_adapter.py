"""BuiltIn Job Board Adapter translating BuiltIn responses to RawJobDTOs."""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..clients.builtin_client import fetch_builtin_jobs
from ..ports import JobSourcePort, RawJobDTO

logger = logging.getLogger(__name__)


class BuiltInJobAdapter(JobSourcePort):
    @property
    def source_name(self) -> str:
        return "builtin"

    def fetch_jobs(
        self,
        search_term: str,
        location: Optional[str] = None,
        limit: int = 20,
        hours_old: Optional[int] = None,
        **kwargs: Any,
    ) -> list[RawJobDTO]:
        timeout = kwargs.get("timeout_seconds", 10.0)
        raw_rows = fetch_builtin_jobs(
            search_term=search_term,
            location=location,
            results_wanted=limit,
            timeout_seconds=timeout,
        )
        dtos: list[RawJobDTO] = []
        for r in raw_rows:
            dtos.append(
                RawJobDTO.create(
                    source=r.get("source") or "builtin",
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
