"""
Domain service for orchestrating job sourcing across multiple adapters,
deduplicating listings, and persisting them as domain JobListing entities.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from django.conf import settings
from django.utils import timezone

from ..models import JobListing
from .adapters.adzuna_adapter import AdzunaJobAdapter
from .adapters.builtin_adapter import BuiltInJobAdapter
from .adapters.dice_adapter import DiceJobAdapter
from .adapters.jobspy_adapter import JobSpyJobAdapter
from .adapters.levels_adapter import LevelsJobAdapter
from .ports import JobSourcePort, RawJobDTO

logger = logging.getLogger(__name__)


class JobIngestionService:
    """
    Orchestrates job search across registered board adapters, normalizes data,
    deduplicates incoming postings, and provides clean domain persistence.
    """

    def __init__(self, adapters: Optional[dict[str, JobSourcePort]] = None):
        self._adapters: dict[str, JobSourcePort] = adapters or {
            "indeed": JobSpyJobAdapter("indeed"),
            "linkedin": JobSpyJobAdapter("linkedin"),
            "dice": DiceJobAdapter(),
            "levels": LevelsJobAdapter(),
            "builtin": BuiltInJobAdapter(),
            "adzuna": AdzunaJobAdapter(),
        }

    def register_adapter(self, name: str, adapter: JobSourcePort) -> None:
        self._adapters[name.lower()] = adapter

    def fetch_all(
        self,
        search_term: str,
        location: Optional[str] = None,
        site_names: Optional[list[str]] = None,
        results_wanted: int = 20,
        hours_old: Optional[int] = None,
        **kwargs: Any,
    ) -> list[RawJobDTO]:
        """Fetch and aggregate postings from all requested job boards."""
        target_sites = [s.lower().strip() for s in (site_names or ["indeed"]) if s]
        if not target_sites:
            target_sites = ["indeed"]

        per_source_limit = max(5, results_wanted // len(target_sites)) if len(target_sites) > 0 else results_wanted
        aggregated: list[RawJobDTO] = []
        seen_keys: set[tuple[str, str]] = set()

        for site in target_sites:
            adapter = self._adapters.get(site)
            if not adapter:
                logger.warning("[JobIngestionService] No adapter registered for site '%s'", site)
                continue
            try:
                results = adapter.fetch_jobs(
                    search_term=search_term,
                    location=location,
                    limit=per_source_limit,
                    hours_old=hours_old,
                    **kwargs,
                )
                for dto in results:
                    key = (dto.source, dto.external_id)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        aggregated.append(dto)
            except Exception as exc:
                logger.warning("[JobIngestionService] Adapter '%s' failed: %s", site, exc)

        if hours_old is not None and hours_old > 0:
            cutoff = timezone.now() - timedelta(hours=hours_old)
            aggregated = [d for d in aggregated if d.posted_at is None or d.posted_at >= cutoff]

        return aggregated

    def persist_dtos(self, dtos: list[RawJobDTO]) -> list[JobListing]:
        """
        Upsert a collection of RawJobDTO objects into the database as JobListing entities.
        Returns the saved JobListing instances.
        """
        saved: list[JobListing] = []
        for dto in dtos:
            defaults = {
                "title": dto.title,
                "company_name": dto.company_name,
                "location": dto.location,
                "description": dto.description,
                "url": dto.url,
            }
            if dto.posted_at is not None:
                defaults["posted_at"] = dto.posted_at
            if dto.raw_json is not None:
                defaults["raw_json"] = dto.raw_json

            job_listing, _created = JobListing.objects.update_or_create(
                source=dto.source,
                external_id=dto.external_id,
                defaults=defaults,
            )
            saved.append(job_listing)
        return saved
