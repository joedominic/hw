"""Password reset, email verification, and account lifecycle HTML views."""
from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth import logout, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm, PasswordResetForm, SetPasswordForm
from django.contrib.auth.views import (
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_http_methods

from .account import (
    delete_user_account,
    export_account_json,
    is_email_verified,
    mark_email_verified,
    normalize_email,
    request_email_change,
    send_verification_email,
    user_from_verify_uid,
    email_verify_token,
)
from .models import UserExperienceSettings


class StyledPasswordResetForm(PasswordResetForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].widget.attrs.update(
            {"class": "w-full rounded border border-slate-300 px-3 py-2", "autocomplete": "email"}
        )


class StyledSetPasswordForm(SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"{css} w-full rounded border border-slate-300 px-3 py-2".strip()


class AppPasswordResetView(PasswordResetView):
    template_name = "resume_app/auth/password_reset.html"
    email_template_name = "resume_app/auth/password_reset_email.txt"
    subject_template_name = "resume_app/auth/password_reset_subject.txt"
    success_url = reverse_lazy("password_reset_done")
    form_class = StyledPasswordResetForm


class AppPasswordResetDoneView(PasswordResetDoneView):
    template_name = "resume_app/auth/password_reset_done.html"


class AppPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "resume_app/auth/password_reset_confirm.html"
    success_url = reverse_lazy("password_reset_complete")
    form_class = StyledSetPasswordForm


class AppPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = "resume_app/auth/password_reset_complete.html"


@require_http_methods(["GET"])
def verify_email_view(request, uidb64: str, token: str):
    user = user_from_verify_uid(uidb64)
    if user is None or not email_verify_token.check_token(user, token):
        messages.error(request, "This verification link is invalid or has expired.")
        return redirect("login")

    exp = UserExperienceSettings.get_for_user(user)
    target = normalize_email(exp.pending_email or user.email or "")
    mark_email_verified(user, email=target or None)
    messages.success(request, "Your email address has been verified.")
    if request.user.is_authenticated and request.user.pk == user.pk:
        return redirect(reverse("settings") + "?tab=account")
    return redirect("login")


@require_http_methods(["POST"])
def resend_verification_view(request):
    if not request.user.is_authenticated:
        return redirect("login")
    if is_email_verified(request.user) and not UserExperienceSettings.get_for_user(request.user).pending_email:
        messages.info(request, "Your email is already verified.")
        return redirect(reverse("settings") + "?tab=account")
    if send_verification_email(request.user, request=request):
        messages.success(request, "Verification email sent. Check your inbox.")
    else:
        messages.error(request, "Could not send verification email. Check your email address and try again.")
    next_url = (request.POST.get("next") or "").strip() or (reverse("settings") + "?tab=account")
    return redirect(next_url)


def handle_account_settings_post(request) -> HttpResponse | None:
    """
    Process Account-tab POST actions from settings_view.
    Returns an HttpResponse when handled, else None.
    """
    action = (request.POST.get("action") or "").strip()
    if action not in (
        "update_account_email",
        "change_password",
        "resend_verification",
        "export_account",
        "delete_account",
        "create_api_key",
        "revoke_api_key",
    ):
        return None

    user = request.user
    account_url = reverse("settings") + "?tab=account"

    if action == "create_api_key":
        from .api_keys import generate_api_key
        from .entitlements import EntitlementDenied

        try:
            _row, raw = generate_api_key(user, name=(request.POST.get("api_key_name") or "").strip())
            messages.success(
                request,
                f"API key created. Copy it now — it will not be shown again: {raw}",
            )
        except EntitlementDenied as exc:
            messages.error(request, str(exc))
        except Exception as exc:
            messages.error(request, str(exc))
        return redirect(account_url)

    if action == "revoke_api_key":
        from .api_keys import revoke_api_key

        try:
            key_id = int(request.POST.get("api_key_id") or 0)
        except ValueError:
            key_id = 0
        if revoke_api_key(user, key_id):
            messages.success(request, "API key revoked.")
        else:
            messages.error(request, "API key not found.")
        return redirect(account_url)

    if action == "resend_verification":
        if send_verification_email(user, request=request):
            messages.success(request, "Verification email sent.")
        else:
            messages.error(request, "Could not send verification email.")
        return redirect(account_url)

    if action == "update_account_email":
        new_email = normalize_email(request.POST.get("email") or "")
        if not new_email:
            messages.error(request, "Email is required.")
            return redirect(account_url)
        try:
            request_email_change(user, new_email)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect(account_url)
        if normalize_email(user.email) == new_email and is_email_verified(user):
            messages.success(request, "Email unchanged.")
            return redirect(account_url)
        if send_verification_email(user, request=request):
            messages.success(
                request,
                f"Confirmation sent to {new_email}. Your account email updates after you verify.",
            )
        else:
            messages.warning(request, "Email change saved, but sending the verification message failed.")
        return redirect(account_url)

    if action == "change_password":
        form = PasswordChangeForm(user, request.POST)
        if form.is_valid():
            form.save()
            update_session_auth_hash(request, form.user)
            messages.success(request, "Password updated.")
        else:
            for err in form.errors.values():
                for msg in err:
                    messages.error(request, msg)
        return redirect(account_url)

    if action == "export_account":
        payload = export_account_json(user)
        response = HttpResponse(payload, content_type="application/json")
        response["Content-Disposition"] = f'attachment; filename="resumeelite-export-{user.pk}.json"'
        return response

    if action == "delete_account":
        confirm = (request.POST.get("confirm_username") or "").strip()
        password = request.POST.get("confirm_password") or ""
        if confirm != user.get_username():
            messages.error(request, "Type your username exactly to confirm deletion.")
            return redirect(account_url)
        if not user.check_password(password):
            messages.error(request, "Password is incorrect.")
            return redirect(account_url)
        delete_user_account(user)
        logout(request)
        messages.success(request, "Your account and associated data have been deleted.")
        return redirect("landing")

    return None


def account_settings_context(user) -> dict:
    """Template context extras for the Account settings tab."""
    from .models import CustomerApiKey
    from .entitlements import get_user_plan

    exp = UserExperienceSettings.get_for_user(user)
    plan = get_user_plan(user)
    return {
        "account_email": user.email or "",
        "account_pending_email": exp.pending_email or "",
        "account_email_verified": is_email_verified(user),
        "password_change_form": PasswordChangeForm(user),
        "api_keys": list(
            CustomerApiKey.objects.for_user(user).order_by("-created_at")[:20]
        ),
        "plan_allows_api": bool(plan and plan.api_access),
    }
