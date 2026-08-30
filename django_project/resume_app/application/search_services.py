"""
Application service for Job Search and Search Profile management use cases.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from django.contrib.auth import get_user_model

from ..models import JobListing, SearchProfile
from ..search_profile_scope import upsert_pipeline_entry
from ..sourcing.service import JobIngestionService

logger = logging.getLogger(__name__)


@dataclass
class JobSearchResult:
    jobs_fetched: int
    jobs_saved: int
    profile_slug: str
    listings: list[JobListing]


class SearchApplicationService:
    """
    Application service executing job search workflows and search profile persistence.
    """

    def __init__(self, ingestion_service: Optional[JobIngestionService] = None):
        self.ingestion = ingestion_service or JobIngestionService()

    def execute_search(
        self,
        user,
        search_term: str,
        location: Optional[str] = None,
        site_names: Optional[list[str]] = None,
        results_wanted: int = 20,
        profile_slug: Optional[str] = None,
        hours_old: Optional[int] = None,
        **kwargs: Any,
    ) -> JobSearchResult:
        """
        Use Case: Execute an on-demand job search across external sources,
        persist the job listings, and associate them with the user's search profile.
        """
        target_slug = profile_slug or SearchProfile.get_default_slug(user)
        sp = SearchProfile.get_by_slug(user, target_slug)

        dtos = self.ingestion.fetch_all(
            search_term=search_term,
            location=location,
            site_names=site_names,
            results_wanted=results_wanted,
            hours_old=hours_old,
            **kwargs,
        )

        saved_listings = self.ingestion.persist_dtos(dtos)

        # Seed into user's pipeline under this search profile
        for listing in saved_listings:
            upsert_pipeline_entry(
                user,
                job_listing_id=listing.id,
                slug=target_slug,
                search_profile=sp,
            )

        return JobSearchResult(
            jobs_fetched=len(dtos),
            jobs_saved=len(saved_listings),
            profile_slug=target_slug,
            listings=saved_listings,
        )
