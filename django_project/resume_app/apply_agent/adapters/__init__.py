"""
ATS adapter registry.

``get_adapter`` returns the deterministic adapter for an ATS slug. Workday is
intentionally absent: it is handled as an assistive handoff (manual apply) by
the orchestrator rather than automated, per the plan.
"""
from __future__ import annotations

from ..base import AtsAdapter
from .ashby import AshbyAdapter
from .greenhouse import GreenhouseAdapter
from .icims import IcimsAdapter
from .lever import LeverAdapter

# ATS slugs handled as manual assistive handoff (never auto-filled/submitted).
HANDOFF_ONLY_ATS = ("workday",)

_REGISTRY: dict[str, AtsAdapter] = {
    GreenhouseAdapter.ats_id: GreenhouseAdapter(),
    LeverAdapter.ats_id: LeverAdapter(),
    AshbyAdapter.ats_id: AshbyAdapter(),
    IcimsAdapter.ats_id: IcimsAdapter(),
}


def get_adapter(ats_id: str) -> AtsAdapter | None:
    """Return the adapter for an ATS slug, or None if unsupported/handoff-only."""
    return _REGISTRY.get((ats_id or "").strip().lower())


def supported_ats() -> list[str]:
    return sorted(_REGISTRY.keys())
