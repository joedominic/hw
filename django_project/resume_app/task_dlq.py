"""
Dead-Letter Queue (DLQ) and failure quarantine for background Huey worker tasks.

Provides automated isolation of failed tasks, structured exception capture,
and administrative inspection and replaying capabilities.
"""
from __future__ import annotations

import importlib
import logging
import traceback
from datetime import timedelta
from typing import Any, Optional

from django.utils import timezone

logger = logging.getLogger(__name__)


def quarantine_failed_task(
    task_id: str,
    task_name: str,
    args: Any = None,
    kwargs: Any = None,
    exc: Optional[Exception] = None,
) -> Any:
    """
    Persist a failed task execution into the FailedTaskQuarantine table.
    """
    from .models import FailedTaskQuarantine

    err_cls = exc.__class__.__name__ if exc else "UnknownException"
    err_msg = str(exc) if exc else "Unknown task failure"
    tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else ""

    try:
        record, created = FailedTaskQuarantine.objects.update_or_create(
            task_id=task_id,
            defaults={
                "task_name": task_name,
                "args_repr": repr(args) if args is not None else "",
                "kwargs_repr": repr(kwargs) if kwargs is not None else "",
                "error_class": err_cls,
                "error_message": err_msg,
                "traceback": tb_str,
                "status": FailedTaskQuarantine.Status.QUARANTINED,
            },
        )
        logger.warning(
            "[TaskDLQ] Quarantined failed task: %s (id=%s, error=%s)",
            task_name,
            task_id,
            err_msg,
        )
        return record
    except Exception as db_err:
        logger.error("[TaskDLQ] Failed to record task quarantine in DB: %s", db_err)
        return None


def register_dlq_signals() -> None:
    """
    Connect Huey's SIGNAL_ERROR to the DLQ quarantine handler.
    Called on Django application ready.
    """
    try:
        from huey import signals
        from huey.contrib.djhuey import HUEY

        @HUEY.signal(signals.SIGNAL_ERROR)
        def _handle_huey_task_error(signal, task, exc=None):
            task_id = getattr(task, "id", "unknown-task-id")
            task_name = getattr(task, "name", str(task))
            task_args = getattr(task, "args", ())
            task_kwargs = getattr(task, "kwargs", {})
            quarantine_failed_task(
                task_id=str(task_id),
                task_name=str(task_name),
                args=task_args,
                kwargs=task_kwargs,
                exc=exc,
            )

        logger.info("[TaskDLQ] Connected Huey SIGNAL_ERROR to FailedTaskQuarantine handler.")
    except Exception as exc:
        logger.warning("[TaskDLQ] Could not register Huey signal handler: %s", exc)


def replay_quarantined_task(quarantine_id: int) -> bool:
    """
    Replay a quarantined task by looking up its callable and re-enqueuing it.
    """
    from .models import FailedTaskQuarantine

    record = FailedTaskQuarantine.objects.filter(pk=quarantine_id).first()
    if not record:
        logger.error("[TaskDLQ] Quarantine record %s not found.", quarantine_id)
        return False

    task_name = record.task_name
    try:
        module_name, func_name = task_name.rsplit(".", 1)
        module = importlib.import_module(module_name)
        task_func = getattr(module, func_name)

        import ast
        args = ast.literal_eval(record.args_repr) if record.args_repr else ()
        kwargs = ast.literal_eval(record.kwargs_repr) if record.kwargs_repr else {}

        # Re-dispatch through Huey
        task_func(*args, **kwargs)

        record.status = FailedTaskQuarantine.Status.REPLAYED
        record.resolved_at = timezone.now()
        record.save(update_fields=["status", "resolved_at"])
        logger.info("[TaskDLQ] Replayed task %s (record #%d)", task_name, record.id)
        return True
    except Exception as exc:
        logger.error("[TaskDLQ] Failed to replay quarantined task #%d: %s", record.id, exc)
        return False


def purge_old_quarantine_records(days: int = 30) -> int:
    """Purge dismissed or resolved quarantine records older than specified days."""
    from .models import FailedTaskQuarantine

    cutoff = timezone.now() - timedelta(days=days)
    count, _ = FailedTaskQuarantine.objects.filter(
        created_at__lt=cutoff
    ).exclude(status=FailedTaskQuarantine.Status.QUARANTINED).delete()
    return count
