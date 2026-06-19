"""
Shared types for the Autonomous Apply Agent: the adapter protocol and the
data structures passed between the orchestrator, adapters, and generic agent.

These types are import-safe without Playwright/browser-use installed so that
answer-key building and orchestration logic can be unit tested in isolation.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable, Optional, Protocol, runtime_checkable


@dataclasses.dataclass
class ApplyContext:
    """Everything an adapter needs to fill and submit a single application.

    ``page`` is a Playwright page when running live and ``None`` in pure unit
    tests (where only answer-key building is exercised).
    """

    full_name: str
    email: str
    phone: str
    location: str
    linkedin_url: str
    website_url: str
    work_authorization: str
    requires_sponsorship: bool
    salary_expectation: str
    cover_letter: str
    custom_qa: dict
    include_eeo: bool
    apply_url: str
    resume_file_path: str
    company_name: str = ""
    job_title: str = ""
    page: Any = None
    credential: Any = None
    attempt_id: int | None = None
    _log_step: Optional[Callable[..., None]] = None

    def log(
        self,
        step_name: str,
        message: str = "",
        action_snapshot: Optional[dict] = None,
        network_log: Optional[list] = None,
        screenshot_path: str = "",
    ) -> None:
        if self._log_step is not None:
            self._log_step(
                step_name,
                message=message,
                action_snapshot=action_snapshot,
                network_log=network_log or [],
                screenshot_path=screenshot_path,
            )

    @property
    def first_name(self) -> str:
        return (self.full_name or "").strip().split(" ")[0] if self.full_name else ""

    @property
    def last_name(self) -> str:
        parts = (self.full_name or "").strip().split(" ")
        return parts[-1] if len(parts) > 1 else ""

    def rendered_cover_letter(self) -> str:
        tpl = self.cover_letter or ""
        if not tpl:
            return ""
        try:
            return tpl.format(company=self.company_name or "", title=self.job_title or "")
        except (KeyError, IndexError, ValueError):
            # Template uses unexpected placeholders; return raw template untouched.
            return tpl


@dataclasses.dataclass
class FillResult:
    """Outcome of a (dry-run or re-validation) fill pass."""

    ok: bool
    confidence: float = 0.0
    payload: dict = dataclasses.field(default_factory=dict)
    missing_fields: list = dataclasses.field(default_factory=list)
    error_code: str = ""
    message: str = ""


@dataclasses.dataclass
class SubmitResult:
    """Outcome of an atomic submit-and-verify pass."""

    ok: bool
    confirmed: bool = False
    error_code: str = ""
    message: str = ""
    network_log: list = dataclasses.field(default_factory=list)


@runtime_checkable
class AtsAdapter(Protocol):
    """Deterministic adapter for one ATS family.

    Implementations must keep ``build_answer_key`` pure (no DOM access) so it can
    be unit tested, and must treat ``fill_from_payload`` as a re-validation pass
    against a freshly loaded form (never a static DOM/token replay).
    """

    ats_id: str

    def can_handle(self, url: str) -> bool:
        ...

    def build_answer_key(self, ctx: ApplyContext) -> dict:
        """Pure mapping of profile + custom answers to this ATS's field names."""
        ...

    def fill_application(self, ctx: ApplyContext, *, stop_before_submit: bool) -> FillResult:
        """Fill the live form. In the dry run, stop before submit and capture the payload."""
        ...

    def fill_from_payload(self, ctx: ApplyContext, payload: dict) -> FillResult:
        """Re-validation pass: load a clean form and re-fill using payload as an answer key."""
        ...

    def submit_and_verify(self, ctx: ApplyContext) -> SubmitResult:
        """Submit and assert success via DOM confirmation AND network response, atomically."""
        ...


def standard_answer_key(ctx: ApplyContext) -> dict:
    """Build the common, ATS-agnostic answer key from the applicant profile.

    Adapters extend/rename these to match their specific field labels. Hidden
    tokens and CSRF values are intentionally never included here.
    """

    answers: dict = {
        "first_name": ctx.first_name,
        "last_name": ctx.last_name,
        "full_name": ctx.full_name,
        "email": ctx.email,
        "phone": ctx.phone,
        "location": ctx.location,
        "linkedin": ctx.linkedin_url,
        "website": ctx.website_url,
        "work_authorization": ctx.work_authorization,
        "requires_sponsorship": ctx.requires_sponsorship,
        "salary_expectation": ctx.salary_expectation,
        "cover_letter": ctx.rendered_cover_letter(),
    }
    # Custom Q&A pairs override/extend standard answers (keyed by question label).
    for key, value in (ctx.custom_qa or {}).items():
        if key and value is not None:
            answers[str(key)] = value
    # Drop empty strings so we do not overwrite prefilled fields with blanks.
    return {k: v for k, v in answers.items() if v not in ("", None)}
