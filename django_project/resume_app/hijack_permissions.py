"""django-hijack permission callbacks."""
from django.contrib.auth import get_user_model

User = get_user_model()


def can_hijack(*, hijacker, hijacked):
    """Only users with can_impersonate_users may hijack; staff targets need superuser."""
    if not hijacker.has_perm("resume_app.can_impersonate_users"):
        return False
    if hijacked.is_superuser and not hijacker.is_superuser:
        return False
    if hijacked.is_staff and not hijacker.is_superuser:
        return False
    return True
