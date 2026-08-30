"""Persist apply-agent step screenshots under ``MEDIA_ROOT`` for the review UI."""
from __future__ import annotations

import base64
import logging
import os
import shutil
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


def attempt_screenshot_dir(attempt_id: int) -> str:
    media_root = getattr(settings, "MEDIA_ROOT", "") or os.path.join(os.getcwd(), "media")
    out_dir = os.path.join(media_root, "apply_agent", f"attempt_{attempt_id}")
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _estimate_raw_size(raw: Any) -> int:
    if isinstance(raw, bytes):
        return len(raw)
    text = str(raw)
    if os.path.isfile(text):
        try:
            return int(os.path.getsize(text))
        except OSError:
            return 0
    try:
        payload = text.split(",", 1)[-1] if text.startswith("data:") else text
        return len(base64.b64decode(payload))
    except Exception:
        return len(text.encode("utf-8", errors="ignore"))


def _owner_for_attempt(attempt_id: int):
    from ..models import ApplicationAttempt

    attempt = (
        ApplicationAttempt.objects.select_related("pipeline_entry__owner")
        .filter(pk=attempt_id)
        .first()
    )
    if attempt is None:
        return None
    return attempt.pipeline_entry.owner


def save_step_screenshot(attempt_id: int, step_key: str, raw: Any) -> str:
    """Save a PNG for an attempt step. ``raw`` may be base64, bytes, or a file path."""
    if not attempt_id or raw in (None, ""):
        return ""

    owner = _owner_for_attempt(attempt_id)
    if owner is not None:
        try:
            from ..subscriptions import QuotaExceeded, check_storage_quota

            check_storage_quota(owner, additional_bytes=_estimate_raw_size(raw))
        except QuotaExceeded as exc:
            logger.warning("storage quota blocked screenshot attempt=%s: %s", attempt_id, exc)
            return ""
        except Exception:
            pass

    safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(step_key))[:64]
    out_dir = attempt_screenshot_dir(attempt_id)
    path = os.path.join(out_dir, f"{safe_key}.png")

    if isinstance(raw, bytes):
        with open(path, "wb") as fh:
            fh.write(raw)
        return path

    text = str(raw)
    if os.path.isfile(text):
        shutil.copy2(text, path)
        return path

    try:
        payload = text.split(",", 1)[-1] if text.startswith("data:") else text
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(payload))
        return path
    except Exception as exc:
        logger.warning("screenshot decode failed attempt=%s: %s", attempt_id, exc)
        return ""


def media_url_for_path(path: str) -> str:
    """Return a browser URL for a file under ``MEDIA_ROOT``, or empty if not servable."""
    if not path:
        return ""
    media_root = os.path.normpath(getattr(settings, "MEDIA_ROOT", "") or "")
    if not media_root:
        return ""
    norm = os.path.normpath(path)
    if not norm.startswith(media_root):
        return ""
    rel = os.path.relpath(norm, media_root).replace("\\", "/")
    base = (getattr(settings, "MEDIA_URL", "/media/") or "/media/").rstrip("/")
    return f"{base}/{rel}"
