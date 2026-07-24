"""Encrypt/decrypt secrets at rest using Fernet with optional key rotation."""
from __future__ import annotations

import base64
import hashlib
import logging
from typing import List

logger = logging.getLogger(__name__)


def _derive_key_from_secret(secret: str) -> bytes:
    digest = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet_keys() -> List[bytes]:
    """
    Return Fernet keys newest-first.

    Prefer ``FERNET_KEYS`` (comma-separated urlsafe base64 Fernet keys).
    Fall back to a key derived from Django ``SECRET_KEY`` for compatibility.
    """
    from django.conf import settings

    raw = (getattr(settings, "FERNET_KEYS", None) or "").strip()
    keys: list[bytes] = []
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            keys.append(part.encode() if isinstance(part, str) else part)
    secret = getattr(settings, "SECRET_KEY", "") or ""
    derived = _derive_key_from_secret(secret)
    if derived not in keys:
        keys.append(derived)
    return keys


def _get_fernet_key():
    """Primary (encrypt) key — first configured key."""
    return _fernet_keys()[0]


def encrypt_api_key(plain: str) -> str:
    if not plain:
        return ""
    try:
        from cryptography.fernet import Fernet

        f = Fernet(_get_fernet_key())
        return f.encrypt(plain.encode()).decode()
    except Exception as e:
        logger.exception("Encrypt failed: %s", e)
        return ""


def decrypt_api_key(encrypted: str) -> str:
    if not encrypted:
        return ""
    try:
        from cryptography.fernet import Fernet, MultiFernet

        fernets = [Fernet(k) for k in _fernet_keys()]
        f = MultiFernet(fernets) if len(fernets) > 1 else fernets[0]
        return f.decrypt(encrypted.encode()).decode()
    except Exception as e:
        logger.warning("Decrypt failed: %s", e)
        return ""


def rotate_encrypted_value(encrypted: str) -> str:
    """Decrypt with any known key and re-encrypt with the primary key."""
    plain = decrypt_api_key(encrypted)
    if not plain:
        return encrypted
    return encrypt_api_key(plain)
