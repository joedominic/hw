"""Signup, login, logout, and public landing views."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.views import LoginView, LogoutView
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.generic import CreateView

from .experience import get_post_login_url
from .forms import LoginForm, SignupForm
from .models import ApplicantProfile


def landing_view(request):
    """Public marketing page; signed-in users go straight to the workspace."""
    if request.user.is_authenticated:
        return redirect(get_post_login_url(request.user))
    return render(
        request,
        "resume_app/landing.html",
        {
            "signup_enabled": getattr(settings, "SIGNUP_ENABLED", True),
        },
    )


def privacy_view(request):
    """Public privacy policy page."""
    return render(request, "resume_app/legal/privacy.html")


def terms_view(request):
    """Public terms of service page."""
    return render(request, "resume_app/legal/terms.html")


class AppLoginView(LoginView):
    template_name = "resume_app/auth/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def get_success_url(self):
        next_url = self.get_redirect_url()
        if next_url:
            return next_url
        return get_post_login_url(self.request.user)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["signup_enabled"] = getattr(settings, "SIGNUP_ENABLED", True)
        return ctx


class AppLogoutView(LogoutView):
    next_page = reverse_lazy(settings.LOGOUT_REDIRECT_URL)


class SignupView(CreateView):
    template_name = "resume_app/auth/signup.html"
    form_class = SignupForm

    def dispatch(self, request, *args, **kwargs):
        if not getattr(settings, "SIGNUP_ENABLED", True):
            messages.error(request, "New account registration is currently disabled.")
            return redirect("login")
        if request.user.is_authenticated:
            return redirect(get_post_login_url(request.user))
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        login(self.request, self.object, backend="django.contrib.auth.backends.ModelBackend")
        # Experience mode defaults via seed_user_defaults (DEFAULT_EXPERIENCE_MODE);
        # advanced UI is optional later in Settings, not a signup fork.

        profile = ApplicantProfile.get_for_user(self.object)
        email = (form.cleaned_data.get("email") or "").strip()
        if email and profile.email != email:
            profile.email = email
            profile.save(update_fields=["email"])

        from .account import send_verification_email

        if send_verification_email(self.object, request=self.request):
            messages.success(
                self.request,
                "Welcome! Check your email to verify your address.",
            )
        else:
            messages.success(self.request, "Welcome! Your account is ready.")
            messages.warning(
                self.request,
                "We could not send a verification email. You can resend it from Settings → Account.",
            )
        return response

    def get_success_url(self):
        return get_post_login_url(self.object)
