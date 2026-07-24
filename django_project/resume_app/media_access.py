"""Authorize and manage MEDIA paths via Django's default storage backend."""
from __future__ import annotations

import json
import logging
import re
from typing import BinaryIO, Optional

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)

_ATTEMPT_DIR_RE = re.compile(r"^attempt_(\d+)$")
_ATTEMPT_RESUME_RE = re.compile(r"^attempt_(\d+)_resume\.(pdf|docx)$", re.IGNORECASE)


def _normalize_rel_path(relative_path: str) -> str:
    return relative_path.replace("\\", "/").lstrip("/")


def using_remote_storage() -> bool:
    return bool(getattr(settings, "AWS_STORAGE_BUCKET_NAME", "") or "")


def media_exists(relative_path: str) -> bool:
    path = _normalize_rel_path(relative_path)
    if not path or ".." in path.split("/"):
        return False
    try:
        return default_storage.exists(path)
    except Exception as exc:
        logger.debug("media_exists failed %s: %s", path, exc)
        return False


def media_open(relative_path: str) -> BinaryIO:
    return default_storage.open(_normalize_rel_path(relative_path), "rb")


def media_size(relative_path: str) -> int:
    path = _normalize_rel_path(relative_path)
    if not path or ".." in path.split("/"):
        return 0
    try:
        return int(default_storage.size(path))
    except Exception:
        return 0


def media_delete(relative_path: str) -> bool:
    path = _normalize_rel_path(relative_path)
    if not path or ".." in path.split("/"):
        return False
    try:
        if default_storage.exists(path):
            default_storage.delete(path)
            return True
    except Exception as exc:
        logger.warning("media_delete failed %s: %s", path, exc)
    return False


def _listdir(relative_dir: str) -> tuple[list[str], list[str]]:
    path = _normalize_rel_path(relative_dir)
    try:
        if not path:
            return default_storage.listdir("")
        if not default_storage.exists(path):
            return [], []
        return default_storage.listdir(path)
    except Exception:
        return [], []


def _walk_files(relative_dir: str) -> list[str]:
    """Return storage-relative file paths under ``relative_dir`` (recursive)."""
    root = _normalize_rel_path(relative_dir)
    found: list[str] = []
    dirs, files = _listdir(root)
    for name in files:
        found.append(f"{root}/{name}" if root else name)
    for dirname in dirs:
        child = f"{root}/{dirname}" if root else dirname
        found.extend(_walk_files(child))
    return found


def _dir_size_storage(relative_dir: str) -> int:
    return sum(media_size(p) for p in _walk_files(relative_dir))


def _delete_tree(relative_dir: str) -> int:
    removed = 0
    for path in reversed(_walk_files(relative_dir)):
        if media_delete(path):
            removed += 1
    # Best-effort: remove empty directory markers if the backend supports it.
    try:
        if default_storage.exists(_normalize_rel_path(relative_dir)):
            default_storage.delete(_normalize_rel_path(relative_dir))
    except Exception:
        pass
    return removed


def _attempt_owned_by(user: AbstractBaseUser, attempt_id: int) -> bool:
    from .models import ApplicationAttempt

    return ApplicationAttempt.objects.filter(
        pk=attempt_id,
        pipeline_entry__owner_id=user.pk,
    ).exists()


def _read_json_media(relative_path: str) -> Optional[dict]:
    if not media_exists(relative_path):
        return None
    try:
        with media_open(relative_path) as fh:
            return json.load(fh)
    except Exception as exc:
        logger.warning("media_access: failed to read JSON %s: %s", relative_path, exc)
        return None


def _pipeline_extract_owned_by(user: AbstractBaseUser, relative_path: str) -> bool:
    """
    Allow pipeline_llm_extract/{track}/{run_id}/... when run meta.owner_id matches.

    Legacy runs without owner_id are denied.
    """
    parts = _normalize_rel_path(relative_path).split("/")
    if len(parts) < 3:
        return False
    track, run_id = parts[1], parts[2]
    from .pipeline_llm_skill_extract import RUN_META_NAME

    meta = _read_json_media(f"pipeline_llm_extract/{track}/{run_id}/{RUN_META_NAME}")
    if not meta:
        return False
    owner_id = meta.get("owner_id")
    try:
        return int(owner_id) == int(user.pk)
    except (TypeError, ValueError):
        return False


def user_may_access_media(user: AbstractBaseUser, relative_path: str) -> bool:
    """
    Return True if ``user`` may read the media object at ``relative_path``
    (path relative to MEDIA_ROOT / storage key, no leading slash).
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False

    path = _normalize_rel_path(relative_path)
    if not path or ".." in path.split("/"):
        return False

    parts = path.split("/")
    root = parts[0]

    if root == "resumes":
        if len(parts) < 2:
            return False
        try:
            owner_id = int(parts[1])
        except ValueError:
            return False
        return owner_id == int(user.pk)

    if root == "apply_agent":
        if len(parts) < 2:
            return False
        leaf = parts[1]
        dir_match = _ATTEMPT_DIR_RE.match(leaf)
        if dir_match and len(parts) >= 2:
            return _attempt_owned_by(user, int(dir_match.group(1)))
        resume_match = _ATTEMPT_RESUME_RE.match(leaf)
        if resume_match and len(parts) == 2:
            return _attempt_owned_by(user, int(resume_match.group(1)))
        return False

    if root == "pipeline_llm_extract":
        return _pipeline_extract_owned_by(user, path)

    return False


def delete_user_media_files(user: AbstractBaseUser) -> int:
    """Remove media belonging to ``user`` via default storage. Returns files removed."""
    from .models import ApplicationAttempt
    from .pipeline_llm_skill_extract import RUN_META_NAME

    removed = 0
    removed += _delete_tree(f"resumes/{user.pk}")

    attempt_ids = ApplicationAttempt.objects.filter(
        pipeline_entry__owner_id=user.pk
    ).values_list("id", flat=True)
    for attempt_id in attempt_ids:
        removed += _delete_tree(f"apply_agent/attempt_{attempt_id}")
        for ext in ("pdf", "docx"):
            if media_delete(f"apply_agent/attempt_{attempt_id}_resume.{ext}"):
                removed += 1

    # Owned pipeline extract runs (meta.owner_id).
    tracks, _ = _listdir("pipeline_llm_extract")
    for track in tracks:
        runs, _ = _listdir(f"pipeline_llm_extract/{track}")
        for run_id in runs:
            meta = _read_json_media(f"pipeline_llm_extract/{track}/{run_id}/{RUN_META_NAME}")
            if not meta:
                continue
            try:
                if int(meta.get("owner_id") or 0) != int(user.pk):
                    continue
            except (TypeError, ValueError):
                continue
            removed += _delete_tree(f"pipeline_llm_extract/{track}/{run_id}")

    return removed


def resolve_safe_media_path(relative_path: str) -> Optional[str]:
    """
    Return a normalized storage-relative path, or None if unsafe.

    For local filesystem this is still relative (not absolute). Callers that need
    an absolute local path should join MEDIA_ROOT themselves when not using remote storage.
    """
    path = _normalize_rel_path(relative_path)
    if not path or ".." in path.split("/"):
        return None
    return path


def resolve_absolute_local_media_path(relative_path: str) -> Optional[str]:
    """Absolute filesystem path under MEDIA_ROOT when using local storage; else None."""
    import os

    if using_remote_storage():
        return None
    media_root = os.path.abspath(str(settings.MEDIA_ROOT))
    full_path = os.path.normpath(os.path.join(media_root, _normalize_rel_path(relative_path)))
    if not full_path.startswith(media_root + os.sep) and full_path != media_root:
        return None
    return full_path
