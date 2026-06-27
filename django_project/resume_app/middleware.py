"""Require authentication for all app routes except auth, admin, and static."""
from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.urls import resolve, Resolver404


class LoginRequiredMiddleware:
    """
    Redirect unauthenticated users to LOGIN_URL.
    API routes return 401 via django-ninja auth; HTML routes redirect.
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

    def __call__(self, request):
        if not self._requires_auth(request):
            return self.get_response(request)

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
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
            if view_name in ("AppLoginView", "SignupView", "AppLogoutView"):
                return False
        except Resolver404:
            pass
        return True
