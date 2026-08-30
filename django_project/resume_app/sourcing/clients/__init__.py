"""Low-level HTTP clients and HTML scrapers for third-party job boards."""
from .adzuna_client import fetch_adzuna_jobs
from .builtin_client import fetch_builtin_jobs
from .dice_client import fetch_dice_jobs
from .levels_client import fetch_levels_jobs

__all__ = [
    "fetch_dice_jobs",
    "fetch_levels_jobs",
    "fetch_builtin_jobs",
    "fetch_adzuna_jobs",
]
