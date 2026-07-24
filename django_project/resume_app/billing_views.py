"""Billing HTML views and Stripe webhook."""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .billing import (
    apply_stripe_event,
    create_billing_portal_session,
    create_checkout_session,
    stripe_enabled,
)
from .entitlements import ensure_default_plans, subscription_summary
from .models import Plan

logger = logging.getLogger(__name__)


@require_http_methods(["GET", "POST"])
def billing_view(request):
    """Settings-adjacent billing page: plan summary + Stripe checkout/portal."""
    ensure_default_plans()
    summary = subscription_summary(request.user)
    plans = list(Plan.objects.filter(is_active=True).order_by("sort_order", "name"))

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "checkout":
            plan_slug = (request.POST.get("plan_slug") or "").strip()
            if not stripe_enabled():
                messages.error(request, "Stripe is not configured on this deployment.")
                return redirect("billing")
            try:
                url = create_checkout_session(
                    request.user,
                    plan_slug=plan_slug,
                    success_url=request.build_absolute_uri(reverse("billing") + "?checkout=success"),
                    cancel_url=request.build_absolute_uri(reverse("billing") + "?checkout=cancel"),
                )
            except Exception as exc:
                logger.exception("checkout failed: %s", exc)
                messages.error(request, str(exc))
                return redirect("billing")
            if url:
                return redirect(url)
            messages.error(request, "Could not start checkout.")
            return redirect("billing")

        if action == "portal":
            if not stripe_enabled():
                messages.error(request, "Stripe is not configured on this deployment.")
                return redirect("billing")
            try:
                url = create_billing_portal_session(
                    request.user,
                    return_url=request.build_absolute_uri(reverse("billing")),
                )
            except Exception as exc:
                messages.error(request, str(exc))
                return redirect("billing")
            if url:
                return redirect(url)

    if request.GET.get("checkout") == "success":
        messages.success(request, "Subscription updated. It may take a moment for Stripe to sync.")
    elif request.GET.get("checkout") == "cancel":
        messages.info(request, "Checkout canceled.")

    token_by_plan = getattr(settings, "LLM_DAILY_TOKEN_LIMIT_BY_PLAN", None) or {}
    for plan in plans:
        lim = int(token_by_plan.get(plan.slug, 0) or 0)
        # Request-scoped display attribute for the template (not persisted).
        plan.token_limit_display = f"{lim:,}" if lim > 0 else "Unlimited"

    return render(
        request,
        "resume_app/billing.html",
        {
            "subscription": summary,
            "plans": plans,
            "stripe_enabled": stripe_enabled(),
        },
    )


@csrf_exempt
@require_http_methods(["POST"])
def stripe_webhook(request):
    """Stripe webhook endpoint (public; signature-verified in production)."""
    payload = request.body
    sig = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "") or ""

    if secret and stripe_enabled():
        import stripe

        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            event = stripe.Webhook.construct_event(payload, sig, secret)
        except Exception as exc:
            logger.warning("stripe webhook verify failed: %s", exc)
            return HttpResponseBadRequest("invalid signature")
        event_dict = event if isinstance(event, dict) else event.to_dict()
    elif getattr(settings, "DEBUG", False):
        # Dev/test only: allow unsigned JSON when webhook secret is unset.
        import json

        try:
            event_dict = json.loads(payload.decode("utf-8"))
        except Exception:
            return HttpResponseBadRequest("invalid json")
    else:
        logger.error("stripe webhook rejected: STRIPE_WEBHOOK_SECRET required when DEBUG=False")
        return HttpResponseBadRequest("webhook secret required")

    try:
        apply_stripe_event(event_dict)
    except Exception:
        logger.exception("stripe webhook processing failed")
        return HttpResponse(status=500)
    return JsonResponse({"received": True})
