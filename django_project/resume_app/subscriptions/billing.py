"""Stripe billing helpers (optional when STRIPE_SECRET_KEY is unset)."""
from __future__ import annotations

import logging
from typing import Any, Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from .entitlements import assign_plan, ensure_default_plans, get_or_create_subscription
from ..models import Plan, StripeWebhookEvent, Subscription

logger = logging.getLogger(__name__)
User = get_user_model()


def stripe_enabled() -> bool:
    return bool(getattr(settings, "STRIPE_SECRET_KEY", None))


def _stripe():
    import stripe

    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def create_checkout_session(user, *, plan_slug: str, success_url: str, cancel_url: str) -> Optional[str]:
    """Return Checkout Session URL, or None if Stripe is not configured."""
    if not stripe_enabled():
        return None
    plan = Plan.objects.get(slug=plan_slug, is_active=True)
    if not plan.stripe_price_id:
        raise ValueError(f"Plan {plan_slug} has no stripe_price_id configured.")
    stripe = _stripe()
    sub = get_or_create_subscription(user)
    customer_id = sub.stripe_customer_id or None
    if not customer_id:
        customer = stripe.Customer.create(
            email=user.email or None,
            metadata={"user_id": str(user.pk), "username": user.get_username()},
        )
        customer_id = customer["id"]
        sub.stripe_customer_id = customer_id
        sub.save(update_fields=["stripe_customer_id", "updated_at"])

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": plan.stripe_price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(user.pk),
        metadata={"user_id": str(user.pk), "plan_slug": plan.slug},
    )
    return session.url


def create_billing_portal_session(user, *, return_url: str) -> Optional[str]:
    if not stripe_enabled():
        return None
    sub = get_or_create_subscription(user)
    if not sub.stripe_customer_id:
        raise ValueError("No Stripe customer on this account yet.")
    stripe = _stripe()
    session = stripe.billing_portal.Session.create(
        customer=sub.stripe_customer_id,
        return_url=return_url,
    )
    return session.url


def _plan_for_price(price_id: str) -> Optional[Plan]:
    if not price_id:
        return None
    return Plan.objects.filter(stripe_price_id=price_id, is_active=True).first()


def _user_from_customer(customer_id: str):
    if not customer_id:
        return None
    sub = Subscription.objects.filter(stripe_customer_id=customer_id).select_related("owner").first()
    return sub.owner if sub else None


def apply_stripe_event(event: dict[str, Any]) -> bool:
    """
    Process a verified Stripe event. Returns True if newly processed.
    Idempotent via StripeWebhookEvent.event_id.
    """
    ensure_default_plans()
    event_id = event.get("id") or ""
    if not event_id:
        return False
    if StripeWebhookEvent.objects.filter(event_id=event_id).exists():
        return False

    etype = event.get("type") or ""
    data = (event.get("data") or {}).get("object") or {}

    if etype == "checkout.session.completed":
        user_id = (data.get("metadata") or {}).get("user_id") or data.get("client_reference_id")
        plan_slug = (data.get("metadata") or {}).get("plan_slug")
        customer_id = data.get("customer") or ""
        subscription_id = data.get("subscription") or ""
        user = None
        if user_id:
            user = User.objects.filter(pk=user_id).first()
        if user is None and customer_id:
            user = _user_from_customer(customer_id)
        if user and plan_slug:
            assign_plan(user, plan_slug, status=Subscription.Status.ACTIVE)
            sub = get_or_create_subscription(user)
            updates = []
            if customer_id and sub.stripe_customer_id != customer_id:
                sub.stripe_customer_id = customer_id
                updates.append("stripe_customer_id")
            if subscription_id:
                sub.stripe_subscription_id = subscription_id
                updates.append("stripe_subscription_id")
            if updates:
                updates.append("updated_at")
                sub.save(update_fields=updates)

    elif etype in ("customer.subscription.updated", "customer.subscription.created"):
        customer_id = data.get("customer") or ""
        user = _user_from_customer(customer_id)
        status_map = {
            "trialing": Subscription.Status.TRIALING,
            "active": Subscription.Status.ACTIVE,
            "past_due": Subscription.Status.PAST_DUE,
            "canceled": Subscription.Status.CANCELED,
            "unpaid": Subscription.Status.UNPAID,
        }
        status = status_map.get(data.get("status") or "", Subscription.Status.ACTIVE)
        items = ((data.get("items") or {}).get("data") or [])
        price_id = ""
        if items:
            price_id = ((items[0].get("price") or {}).get("id")) or ""
        plan = _plan_for_price(price_id)
        if user:
            sub = get_or_create_subscription(user)
            sub.status = status
            sub.stripe_subscription_id = data.get("id") or sub.stripe_subscription_id
            if plan:
                sub.plan = plan
            period_end = data.get("current_period_end")
            if period_end:
                from datetime import datetime, timezone as dt_tz

                sub.current_period_end = datetime.fromtimestamp(int(period_end), tz=dt_tz.utc)
            sub.save()

    elif etype == "customer.subscription.deleted":
        customer_id = data.get("customer") or ""
        user = _user_from_customer(customer_id)
        if user:
            assign_plan(user, getattr(settings, "SAAS_DEFAULT_PLAN_SLUG", "free"), status=Subscription.Status.CANCELED)
            sub = get_or_create_subscription(user)
            sub.stripe_subscription_id = ""
            sub.save(update_fields=["stripe_subscription_id", "updated_at"])

    elif etype == "invoice.payment_failed":
        customer_id = data.get("customer") or ""
        user = _user_from_customer(customer_id)
        if user:
            sub = get_or_create_subscription(user)
            # Keep plan FK for portal recovery messaging; entitlements fall back to free.
            sub.status = Subscription.Status.PAST_DUE
            sub.save(update_fields=["status", "updated_at"])
            logger.info("invoice.payment_failed user=%s → past_due", user.pk)

    elif etype in ("invoice.paid", "invoice.payment_succeeded"):
        customer_id = data.get("customer") or ""
        user = _user_from_customer(customer_id)
        if user:
            sub = get_or_create_subscription(user)
            if sub.status in (Subscription.Status.PAST_DUE, Subscription.Status.UNPAID):
                sub.status = Subscription.Status.ACTIVE
                sub.save(update_fields=["status", "updated_at"])
                logger.info("invoice.paid user=%s → active", user.pk)

    StripeWebhookEvent.objects.create(
        event_id=event_id,
        event_type=etype,
        payload=event,
    )
    return True
