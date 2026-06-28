"""Signup, login, logout views."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.views import LoginView, LogoutView
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.generic import CreateView

from .forms import LoginForm, SignupForm


class AppLoginView(LoginView):
    template_name = "resume_app/auth/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def get_success_url(self):
        return self.get_redirect_url() or reverse_lazy(settings.LOGIN_REDIRECT_URL)


    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["signup_enabled"] = getattr(settings, "SIGNUP_ENABLED", True)
        return ctx


class AppLogoutView(LogoutView):
    next_page = reverse_lazy(settings.LOGOUT_REDIRECT_URL)


class SignupView(CreateView):
    template_name = "resume_app/auth/signup.html"
    form_class = SignupForm
    success_url = reverse_lazy(settings.LOGIN_REDIRECT_URL)

    def dispatch(self, request, *args, **kwargs):
        if not getattr(settings, "SIGNUP_ENABLED", True):
            messages.error(request, "New account registration is currently disabled.")
            return redirect("login")
        if request.user.is_authenticated:
            return redirect(settings.LOGIN_REDIRECT_URL)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        login(self.request, self.object, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(self.request, "Welcome! Your account is ready.")
        return response
