from django.conf import settings

from .account import is_email_verified
from .experience import is_power_user, onboarding_progress
from .models import UserExperienceSettings


def dev_tools(request):
    """Expose SHOW_DEV_TOOLS (defaults to DEBUG) for conditional nav links."""
    show = getattr(settings, "SHOW_DEV_TOOLS", settings.DEBUG)
    return {"show_dev_tools": bool(show)}


def experience_context(request):
    """Experience mode and onboarding checklist for templates."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {
            "is_power_user": False,
            "onboarding": {},
            "email_verified": True,
            "show_email_verify_banner": False,
            "pending_email": "",
        }
    exp = UserExperienceSettings.get_for_user(user)
    verified = is_email_verified(user)
    return {
        "is_power_user": is_power_user(user),
        "onboarding": onboarding_progress(user),
        "email_verified": verified,
        "show_email_verify_banner": (not verified) or bool(exp.pending_email),
        "pending_email": exp.pending_email or "",
    }
