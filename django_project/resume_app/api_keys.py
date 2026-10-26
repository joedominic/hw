"""Customer API key helpers and Ninja auth."""
from __future__ import annotations

import hashlib
import secrets
from typing import Optional

from django.contrib.auth.models import AbstractBaseUser
from django.http import HttpRequest
from django.utils import timezone
from ninja.security import HttpBearer

from .subscriptions import require_api_access, EntitlementDenied
from .models import CustomerApiKey


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_api_key(user: AbstractBaseUser, *, name: str = "") -> tuple[CustomerApiKey, str]:
    """
    Create a new API key. Returns (row, plaintext_once).
    Plaintext is only available at creation time.
    """
    require_api_access(user)
    secret = secrets.token_urlsafe(32)
    prefix = secrets.token_hex(4)
    raw = f"re_{prefix}_{secret}"
    row = CustomerApiKey.objects.create(
        owner=user,
        name=(name or "").strip()[:128],
        prefix=prefix,
        key_hash=_hash_key(raw),
    )
    return row, raw


def revoke_api_key(user: AbstractBaseUser, key_id: int) -> bool:
    row = CustomerApiKey.objects.for_user(user).filter(pk=key_id, revoked_at__isnull=True).first()
    if not row:
        return False
    row.revoked_at = timezone.now()
    row.save(update_fields=["revoked_at"])
    return True


def authenticate_api_key(raw: str) -> Optional[AbstractBaseUser]:
    raw = (raw or "").strip()
    if not raw.startswith("re_") or raw.count("_") < 2:
        return None
    key_hash = _hash_key(raw)
    row = (
        CustomerApiKey.objects.select_related("owner")
        .filter(key_hash=key_hash, revoked_at__isnull=True)
        .first()
    )
    if not row:
        return None
    try:
        require_api_access(row.owner)
    except EntitlementDenied:
        return None
    CustomerApiKey.objects.filter(pk=row.pk).update(last_used_at=timezone.now())
    return row.owner


class SessionOrApiKeyAuth(HttpBearer):
    """
    Accept Django session (via request.user) OR Authorization: Bearer <api_key>.

    Ninja still calls authenticate for bearer tokens; session users are handled
    in ``__call__`` when no Authorization header is present.
    """

    def __call__(self, request: HttpRequest):
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            return user
        return super().__call__(request)

    def authenticate(self, request: HttpRequest, token: str):
        user = authenticate_api_key(token)
        if user is None:
            return None
        request.user = user
        return user
