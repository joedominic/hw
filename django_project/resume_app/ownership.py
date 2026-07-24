"""Ownership invariant helpers (no schema migration required)."""
from __future__ import annotations

from typing import Any, Iterable, Optional


class OwnershipError(ValueError):
    """Related objects do not share the same owner."""


def owner_id_of(obj: Any) -> Optional[int]:
    if obj is None:
        return None
    oid = getattr(obj, "owner_id", None)
    if oid is not None:
        return int(oid)
    owner = getattr(obj, "owner", None)
    if owner is not None and getattr(owner, "pk", None) is not None:
        return int(owner.pk)
    return None


def assert_same_owner(*objects: Any, context: str = "") -> int:
    """
    Ensure every non-None object with an owner shares the same owner_id.
    Returns that owner_id. Raises OwnershipError on mismatch.
    """
    ids: list[int] = []
    for obj in objects:
        oid = owner_id_of(obj)
        if oid is not None:
            ids.append(oid)
    if not ids:
        raise OwnershipError(f"No owner found{': ' + context if context else ''}")
    if len(set(ids)) > 1:
        raise OwnershipError(
            f"Cross-tenant ownership mismatch{': ' + context if context else ''} ({ids})"
        )
    return ids[0]


def filter_same_owner(qs, user) -> Any:
    """Convenience: qs.for_user(user) when available, else filter(owner=user)."""
    if hasattr(qs, "for_user"):
        return qs.for_user(user)
    return qs.filter(owner=user)
