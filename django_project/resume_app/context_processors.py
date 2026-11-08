from django.conf import settings

from .account import is_email_verified
from .experience import is_power_user, onboarding_progress
from .models import UserExperienceSettings
from .tenancy import get_real_user


def dev_tools(request):
    """Expose SHOW_DEV_TOOLS (defaults to DEBUG) for conditional nav links."""
    show = getattr(settings, "SHOW_DEV_TOOLS", settings.DEBUG)
    return {"show_dev_tools": bool(show)}


def experience_context(request):
    """Experience mode, staff chrome flags, and onboarding checklist for templates."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {
            "is_power_user": False,
            "onboarding": {},
            "email_verified": True,
            "show_email_verify_banner": False,
            "pending_email": "",
            "real_user_is_staff": False,
            "real_user_is_advanced": False,
            "can_impersonate_users": False,
            "is_hijacked": False,
            "show_admin_console": False,
        }
    exp = UserExperienceSettings.get_for_user(user)
    verified = is_email_verified(user)
    require_verify = getattr(settings, "REQUIRE_EMAIL_VERIFICATION", False)
    # Only show verification banner if explicitly required by config and user is not staff/superuser,
    # or if the user explicitly initiated an email change that requires confirmation.
    show_banner = bool(exp.pending_email)
    if require_verify and not verified and not getattr(user, "is_staff", False) and not getattr(user, "is_superuser", False):
        show_banner = True

    real = get_real_user(request)
    real_is_staff = bool(real and getattr(real, "is_staff", False))
    is_hijacked = bool(getattr(user, "is_hijacked", False))
    can_impersonate = bool(
        real and real.has_perm("resume_app.can_impersonate_users")
    )

    return {
        "is_power_user": is_power_user(user),
        "onboarding": onboarding_progress(user),
        "email_verified": verified,
        "show_email_verify_banner": show_banner,
        "pending_email": exp.pending_email or "",
        "real_user_is_staff": real_is_staff,
        "real_user_is_advanced": bool(real_is_staff and real and is_power_user(real)),
        "can_impersonate_users": can_impersonate,
        "is_hijacked": is_hijacked,
        "show_admin_console": real_is_staff,
    }

