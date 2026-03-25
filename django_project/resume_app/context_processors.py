from django.conf import settings


def dev_tools(request):
    """Expose SHOW_DEV_TOOLS (defaults to DEBUG) for conditional nav links."""
    show = getattr(settings, "SHOW_DEV_TOOLS", settings.DEBUG)
    return {"show_dev_tools": bool(show)}
