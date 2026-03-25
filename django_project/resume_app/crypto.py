"""Encrypt/decrypt API keys at rest using Fernet (symmetric) key derived from Django SECRET_KEY."""
import base64
import hashlib
import logging

logger = logging.getLogger(__name__)


def _get_fernet_key():
    from django.conf import settings
    secret = getattr(settings, "SECRET_KEY", "") or ""
    digest = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


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
        from cryptography.fernet import Fernet
        f = Fernet(_get_fernet_key())
        return f.decrypt(encrypted.encode()).decode()
    except Exception as e:
        logger.warning("Decrypt failed: %s", e)
        return ""
