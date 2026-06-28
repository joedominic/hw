"""Hijack audit logging and session cleanup."""
import logging

from django.dispatch import receiver
from hijack.signals import hijack_ended, hijack_started

from .models import ImpersonationAuditLog
from .tenancy import clear_impersonation_session_keys

logger = logging.getLogger(__name__)


def _client_ip(request) -> str:
    if request is None:
        return ""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or ""


@receiver(hijack_started)
def on_hijack_started(sender, request, hijacker, hijacked, **kwargs):
    reason = (request.POST.get("reason") or "").strip() if request.method == "POST" else ""
    ImpersonationAuditLog.objects.create(
        hijacker=hijacker,
        target=hijacked,
        ip_address=_client_ip(request),
        reason=reason[:500],
    )
    clear_impersonation_session_keys(request)
    logger.info(
        "impersonation_started hijacker=%s target=%s ip=%s",
        hijacker.pk,
        hijacked.pk,
        _client_ip(request),
    )


@receiver(hijack_ended)
def on_hijack_ended(sender, request, hijacker, hijacked, **kwargs):
    ImpersonationAuditLog.objects.filter(
        hijacker=hijacker,
        target=hijacked,
        ended_at__isnull=True,
    ).order_by("-started_at").update(ended_at=__import__("django.utils.timezone", fromlist=["timezone"]).timezone.now())
    logger.info("impersonation_ended hijacker=%s target=%s", hijacker.pk, hijacked.pk)
