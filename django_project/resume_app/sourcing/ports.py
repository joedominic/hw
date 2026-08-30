"""
Domain Port and Data Transfer Object definitions for external job board sourcing.
Acts as an Anti-Corruption Layer (ACL) between third-party scrapers/APIs and domain entities.
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass(frozen=True)
class RawJobDTO:
    """
    Standardized, normalized Data Transfer Object representing a job posting fetched
    from an external source before being converted to a domain JobListing entity.
    """
    source: str
    external_id: str
    title: str
    company_name: str
    location: str = ""
    description: str = ""
    url: str = ""
    posted_at: Optional[datetime] = None
    raw_json: Optional[dict[str, Any]] = field(default=None)

    @classmethod
    def create(
        cls,
        *,
        source: str,
        title: str,
        company_name: str,
        url: str = "",
        location: str = "",
        description: str = "",
        posted_at: Optional[datetime] = None,
        external_id: Optional[str] = None,
        raw_json: Optional[dict[str, Any]] = None,
    ) -> "RawJobDTO":
        """Factory that guarantees normalization and stable deduplication keys."""
        norm_source = (source or "unknown").strip().lower()
        norm_title = (title or "Untitled").strip()
        norm_company = (company_name or "Unknown").strip()
        norm_url = (url or "").strip()
        norm_location = (location or "").strip()
        norm_desc = (description or "").strip()

        if not external_id:
            raw_key = f"{norm_title}|{norm_company}|{norm_url}"
            generated_id = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:64]
        else:
            generated_id = str(external_id).strip()

        return cls(
            source=norm_source,
            external_id=generated_id,
            title=norm_title,
            company_name=norm_company,
            location=norm_location,
            description=norm_desc,
            url=norm_url,
            posted_at=posted_at,
            raw_json=raw_json,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to legacy dict shape expected by downstream callers."""
        out: dict[str, Any] = {
            "source": self.source,
            "external_id": self.external_id,
            "title": self.title,
            "company_name": self.company_name,
            "location": self.location,
            "description": self.description,
            "job_url": self.url,
        }
        if self.posted_at is not None:
            out["date_posted"] = self.posted_at
        if self.raw_json is not None:
            out["raw_json"] = self.raw_json
        return out


class JobSourcePort(ABC):
    """
    Abstract Port interface for external job sourcing adapters.
    Each adapter isolates specific board scraping/API quirks.
    """

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Unique identifier for this job source, e.g. 'jobspy_indeed', 'dice'."""
        pass

    @abstractmethod
    def fetch_jobs(
        self,
        search_term: str,
        location: Optional[str] = None,
        limit: int = 20,
        hours_old: Optional[int] = None,
        **kwargs: Any,
    ) -> list[RawJobDTO]:
        """Fetch and translate external job postings into standardized RawJobDTOs."""
        pass
