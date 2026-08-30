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
from ..models import ImpersonationAuditLog, LLMAppUsageTotals, Plan, Subscription

logger = logging.getLogger(__name__)


@require_http_methods(["GET", "POST"])
def billing_view(request):
    """
    Billing page:
    - Staff / Admin: Plan & Quotas Control Console (configure pricing, quotas, features).
    - Regular Users / Impersonating: Candidate subscription summary & Stripe checkout/portal.
    """
    ensure_default_plans()
    is_admin_mode = bool(request.user.is_staff and not getattr(request.user, "is_hijacked", False))

    if is_admin_mode:
        from django.db.models import Count
        from django.utils.text import slugify

        if request.method == "POST":
            action = (request.POST.get("action") or "").strip()

            if action == "save_plan":
                plan_id = request.POST.get("plan_id")
                try:
                    plan = Plan.objects.get(pk=plan_id)
                except Plan.DoesNotExist:
                    messages.error(request, "Plan not found.")
                    return redirect("billing")

                plan.name = (request.POST.get("name") or plan.name).strip()
                plan.description = (request.POST.get("description") or "").strip()
                plan.price_display = (request.POST.get("price_display") or "").strip()
                plan.stripe_price_id = (request.POST.get("stripe_price_id") or "").strip()

                try:
                    plan.llm_requests_per_day = max(0, int(request.POST.get("llm_requests_per_day", 0) or 0))
                    plan.llm_tokens_per_day = max(0, int(request.POST.get("llm_tokens_per_day", 0) or 0))
                    plan.job_searches_per_day = max(0, int(request.POST.get("job_searches_per_day", 0) or 0))
                    plan.apply_runs_per_day = max(0, int(request.POST.get("apply_runs_per_day", 0) or 0))
                    plan.storage_mb = max(0, int(request.POST.get("storage_mb", 0) or 0))
                    plan.sort_order = max(0, int(request.POST.get("sort_order", 100) or 100))
                except ValueError:
                    messages.error(request, "Invalid numeric quota value.")
                    return redirect("billing")

                plan.api_access = bool(request.POST.get("api_access"))
                new_is_active = bool(request.POST.get("is_active"))
                new_is_default = bool(request.POST.get("is_default"))

                if new_is_default:
                    # Setting as default requires plan to be active and clears default on all other plans
                    plan.is_active = True
                    plan.is_default = True
                    Plan.objects.exclude(pk=plan.pk).update(is_default=False)
                else:
                    if plan.is_default:
                        # Cannot unset default if no other active default exists
                        has_other_default = Plan.objects.exclude(pk=plan.pk).filter(is_default=True, is_active=True).exists()
                        if not has_other_default:
                            messages.warning(request, f"Plan '{plan.name}' was kept as default because at least one active default plan is required.")
                            new_is_default = True
                    plan.is_active = new_is_active
                    plan.is_default = new_is_default

                plan.save()
                messages.success(request, f"Plan '{plan.name}' updated successfully.")
                return redirect("billing")

            if action == "create_plan":
                name = (request.POST.get("name") or "").strip()
                slug = slugify((request.POST.get("slug") or name).strip())

                if not name or not slug:
                    messages.error(request, "Plan name and slug are required.")
                    return redirect("billing")

                if Plan.objects.filter(slug=slug).exists():
                    messages.error(request, f"A plan with slug '{slug}' already exists.")
                    return redirect("billing")

                try:
                    llm_req = max(0, int(request.POST.get("llm_requests_per_day", 100) or 0))
                    llm_tok = max(0, int(request.POST.get("llm_tokens_per_day", 500000) or 0))
                    searches = max(0, int(request.POST.get("job_searches_per_day", 50) or 0))
                    applies = max(0, int(request.POST.get("apply_runs_per_day", 20) or 0))
                    storage = max(0, int(request.POST.get("storage_mb", 1000) or 0))
                    sort_order = max(0, int(request.POST.get("sort_order", 50) or 50))
                except ValueError:
                    messages.error(request, "Invalid numeric quota value.")
                    return redirect("billing")

                is_default = bool(request.POST.get("is_default"))
                if is_default:
                    Plan.objects.all().update(is_default=False)

                Plan.objects.create(
                    slug=slug,
                    name=name,
                    description=(request.POST.get("description") or "").strip(),
                    price_display=(request.POST.get("price_display") or "").strip(),
                    stripe_price_id=(request.POST.get("stripe_price_id") or "").strip(),
                    llm_requests_per_day=llm_req,
                    llm_tokens_per_day=llm_tok,
                    job_searches_per_day=searches,
                    apply_runs_per_day=applies,
                    storage_mb=storage,
                    sort_order=sort_order,
                    api_access=bool(request.POST.get("api_access")),
                    is_active=bool(request.POST.get("is_active")),
                    is_default=is_default,
                )
                messages.success(request, f"New plan '{name}' created successfully.")
                return redirect("billing")

            if action == "delete_plan":
                plan_id = request.POST.get("plan_id")
                try:
                    plan = Plan.objects.get(pk=plan_id)
                except Plan.DoesNotExist:
                    messages.error(request, "Plan not found.")
                    return redirect("billing")

                if plan.is_default:
                    messages.error(request, f"Cannot delete '{plan.name}' because it is the default plan. Designate another plan as default first.")
                    return redirect("billing")

                if Subscription.objects.filter(plan=plan).exists():
                    messages.error(request, f"Cannot delete '{plan.name}' because tenants are currently subscribed to it. You can deactivate it instead.")
                    return redirect("billing")

                plan_name = plan.name
                plan.delete()
                messages.success(request, f"Plan '{plan_name}' deleted.")
                return redirect("billing")

        from django.db.models import Sum

        plans = Plan.objects.annotate(subscriber_count=Count("subscriptions")).order_by("sort_order", "name")
        total_subscribers = Subscription.objects.count()
        active_subscribers_count = Subscription.objects.filter(status__in=["active", "trialing"]).count()
        active_plans_count = plans.filter(is_active=True).count()
        recent_subscriptions = list(
            Subscription.objects.select_related("owner", "plan").order_by("-updated_at")[:8]
        )
        recent_impersonations = list(
            ImpersonationAuditLog.objects.select_related("hijacker", "target").order_by("-started_at")[:8]
        )
        token_aggregates = LLMAppUsageTotals.objects.aggregate(
            total_in=Sum("total_input_tokens"),
            total_out=Sum("total_output_tokens"),
            total_req=Sum("total_requests"),
        )

        return render(
            request,
            "resume_app/admin_billing.html",
            {
                "plans": plans,
                "total_subscribers": total_subscribers,
                "active_subscribers_count": active_subscribers_count,
                "active_plans_count": active_plans_count,
                "recent_subscriptions": recent_subscriptions,
                "recent_impersonations": recent_impersonations,
                "platform_tokens": {
                    "input": int(token_aggregates.get("total_in") or 0),
                    "output": int(token_aggregates.get("total_out") or 0),
                    "requests": int(token_aggregates.get("total_req") or 0),
                },
                "stripe_enabled": stripe_enabled(),
            },
        )

    # Candidate / Tenant Billing View
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
        lim = plan.llm_tokens_per_day or int(token_by_plan.get(plan.slug, 0) or 0)
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
