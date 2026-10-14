"""Require authentication for all app routes except auth, admin, and static."""
import logging
from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.contrib.sessions.exceptions import SessionInterrupted
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect
from django.urls import resolve, Resolver404, reverse

logger = logging.getLogger("resume_app.session")


class SessionInterruptedMiddleware:
    """
    Catch SessionInterrupted when concurrent requests delete, cycle, or invalidate
    a session while a long-running request was in flight.

    Rather than bubbling an unhandled 500 error to the user or Sentry, this middleware
    logs a clean warning, clears the stale session cookie from the client response,
    and redirects the user back to the requested path (or login if unauthenticated),
    or returns a clean 401 if it was an API / JSON request.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        except SessionInterrupted:
            logger.warning(
                "[session] SessionInterrupted on %s %s: session was deleted or cycled concurrently. Recovering cleanly.",
                request.method,
                request.path,
            )
            is_ajax_or_api = (
                request.path.startswith("/api/")
                or request.headers.get("x-requested-with") == "XMLHttpRequest"
                or "application/json" in request.headers.get("accept", "")
            )
            if is_ajax_or_api:
                response = JsonResponse(
                    {"detail": "Session was terminated or cycled concurrently. Please refresh or sign in again."},
                    status=401,
                )
            else:
                redirect_url = request.get_full_path()
                user = getattr(request, "user", None)
                if not (user is not None and getattr(user, "is_authenticated", False)):
                    redirect_url = getattr(settings, "LOGIN_URL", "/accounts/login/")
                response = HttpResponseRedirect(redirect_url)

            cookie_name = getattr(settings, "SESSION_COOKIE_NAME", "sessionid")
            cookie_path = getattr(settings, "SESSION_COOKIE_PATH", "/")
            cookie_domain = getattr(settings, "SESSION_COOKIE_DOMAIN", None)
            cookie_samesite = getattr(settings, "SESSION_COOKIE_SAMESITE", "Lax")
            response.delete_cookie(
                cookie_name,
                path=cookie_path,
                domain=cookie_domain,
                samesite=cookie_samesite,
            )
            return response


class LoginRequiredMiddleware:
    """
    Redirect unauthenticated users to LOGIN_URL.
    API routes return 401 via django-ninja auth; HTML routes redirect.

    When REQUIRE_EMAIL_VERIFICATION is enabled, authenticated but unverified
    users are limited to account settings, logout, and verification routes.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.exempt_prefixes = tuple(
            getattr(
                settings,
                "LOGIN_EXEMPT_URL_PREFIXES",
                (
                    "/accounts/",
                    "/admin/",
                    "/static/",
                ),
            )
        )
        self.unverified_allowed_prefixes = (
            "/accounts/",
            "/admin/",
            "/static/",
            "/media/",
        )

    def __call__(self, request):
        if not self._requires_auth(request):
            return self.get_response(request)

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            if self._requires_email_verification(request, user):
                if request.path.startswith("/api/"):
                    from django.http import JsonResponse

                    return JsonResponse(
                        {"detail": "Email verification required"},
                        status=403,
                    )
                return redirect(reverse("settings") + "?tab=account")
            return self.get_response(request)

        path = request.path
        if path.startswith("/api/"):
            from django.http import JsonResponse

            return JsonResponse({"detail": "Authentication required"}, status=401)

        return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)

    def _requires_auth(self, request) -> bool:
        path = request.path
        for prefix in self.exempt_prefixes:
            if path.startswith(prefix):
                return False
        try:
            match = resolve(path)
            view_name = getattr(match.func, "__name__", "")
            if view_name in ("AppLoginView", "SignupView", "AppLogoutView", "landing_view"):
                return False
        except Resolver404:
            pass
        return True

    def _requires_email_verification(self, request, user) -> bool:
        if not getattr(settings, "REQUIRE_EMAIL_VERIFICATION", False):
            return False
        if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
            return False
        path = request.path
        for prefix in self.unverified_allowed_prefixes:
            if path.startswith(prefix):
                return False
        # Allow Settings → Account so users can resend / update email.
        if path.startswith("/settings/"):
            return False
        from .account import is_email_verified

        return not is_email_verified(user)
