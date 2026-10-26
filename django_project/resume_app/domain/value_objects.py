"""
Domain Value Objects encapsulating validation invariants, domain operations,
and formatting rules to eliminate Primitive Obsession across the application.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from django.utils import timezone


@dataclass(frozen=True)
class MatchScore:
    """
    Immutable Value Object representing a job matching / fit score (0 to 100).
    Enforces domain invariants and provides business grading rules.
    """
    value: int

    def __post_init__(self):
        if self.value is None:
            raise ValueError("MatchScore value cannot be None")
        try:
            int_val = int(self.value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"MatchScore must be an integer, got {self.value}") from exc

        if not (0 <= int_val <= 100):
            raise ValueError(f"MatchScore must be between 0 and 100, got {int_val}")
        object.__setattr__(self, "value", int_val)

    @classmethod
    def from_optional(cls, val: Any) -> Optional["MatchScore"]:
        """Safely parse optional/raw input or return None if empty/invalid."""
        if val is None:
            return None
        try:
            clamped = max(0, min(100, int(val)))
            return cls(clamped)
        except (TypeError, ValueError):
            return None

    @property
    def grade(self) -> str:
        if self.value >= 85:
            return "Strong Match"
        if self.value >= 70:
            return "Moderate Match"
        return "Low Match"

    def apply_penalty(self, penalty_points: int) -> "MatchScore":
        """Return a new MatchScore with the specified penalty deducted (clamped to 0)."""
        new_val = max(0, min(100, self.value - int(penalty_points or 0)))
        return MatchScore(new_val)

    def __int__(self) -> int:
        return self.value

    def __str__(self) -> str:
        return f"{self.value}%"

    def __lt__(self, other: Any) -> bool:
        if isinstance(other, MatchScore):
            return self.value < other.value
        return self.value < int(other)

    def __le__(self, other: Any) -> bool:
        if isinstance(other, MatchScore):
            return self.value <= other.value
        return self.value <= int(other)

    def __gt__(self, other: Any) -> bool:
        if isinstance(other, MatchScore):
            return self.value > other.value
        return self.value > int(other)

    def __ge__(self, other: Any) -> bool:
        if isinstance(other, MatchScore):
            return self.value >= other.value
        return self.value >= int(other)


@dataclass(frozen=True)
class CronSchedule:
    """
    Immutable Value Object representing a validated 5-field cron expression.
    """
    expression: str

    def __post_init__(self):
        expr = (self.expression or "").strip()
        if not expr:
            raise ValueError("Cron expression cannot be empty.")
        import croniter
        try:
            croniter.croniter(expr)
        except Exception as e:
            raise ValueError(f"Invalid cron expression '{expr}': {e}") from e
        object.__setattr__(self, "expression", expr)

    def next_run_after(self, base_time: Optional[datetime] = None) -> datetime:
        """Calculate the next execution timestamp after the given base time."""
        import croniter
        t = base_time or timezone.now()
        itr = croniter.croniter(self.expression, t)
        next_dt = itr.get_next(datetime)
        if timezone.is_naive(next_dt):
            next_dt = timezone.make_aware(next_dt, timezone.get_current_timezone())
        return next_dt

    def __str__(self) -> str:
        return self.expression


@dataclass(frozen=True)
class SalaryRange:
    """
    Immutable Value Object representing compensation boundaries.
    """
    min_amount: Optional[Decimal] = None
    max_amount: Optional[Decimal] = None
    currency: str = "USD"
    period: str = "yearly"  # 'yearly', 'hourly', 'monthly'

    def format_display(self) -> str:
        if self.min_amount is not None and self.max_amount is not None:
            return f"${self.min_amount:,.0f} - ${self.max_amount:,.0f} / {self.period}"
        if self.min_amount is not None:
            return f"From ${self.min_amount:,.0f} / {self.period}"
        if self.max_amount is not None:
            return f"Up to ${self.max_amount:,.0f} / {self.period}"
        return "Compensation not specified"


@dataclass(frozen=True)
class TokenUsage:
    """
    Immutable Value Object encapsulating token consumption counts and arithmetic.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    def __post_init__(self):
        object.__setattr__(self, "input_tokens", max(0, int(self.input_tokens or 0)))
        object.__setattr__(self, "output_tokens", max(0, int(self.output_tokens or 0)))
        object.__setattr__(self, "cached_tokens", max(0, int(self.cached_tokens or 0)))

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Any) -> "TokenUsage":
        if not isinstance(other, TokenUsage):
            raise TypeError(f"Cannot add TokenUsage with {type(other)}")
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )

    def is_within_budget(self, max_tokens: int) -> bool:
        if max_tokens <= 0:
            return True
        return self.total_tokens <= max_tokens


@dataclass(frozen=True)
class TrackSlug:
    """
    Immutable Value Object representing a valid, URL-safe search profile / track slug.
    """
    value: str

    def __post_init__(self):
        raw = (self.value or "").strip().lower()
        if not raw:
            raise ValueError("Track slug cannot be empty.")
        if len(raw) > 32:
            raise ValueError(f"Track slug exceeds maximum length of 32 chars: '{raw}'")
        if not re.match(r"^[a-z0-9_-]+$", raw):
            raise ValueError(f"Track slug contains invalid characters: '{raw}'")
        object.__setattr__(self, "value", raw)

    def __str__(self) -> str:
        return self.value
