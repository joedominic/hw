"""Staff-only support views."""
import logging

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .account import is_email_verified
from .subscriptions import assign_plan, ensure_default_plans, get_or_create_subscription, subscription_summary
from .models import LLMAppUsageTotals, Plan

import csv
from django.http import HttpResponse

logger = logging.getLogger(__name__)
User = get_user_model()


@login_required
@permission_required("resume_app.can_impersonate_users", raise_exception=True)
def staff_users_view(request):
    ensure_default_plans()
    query = (request.GET.get("q") or "").strip()
    selected_plan = (request.GET.get("plan") or "").strip()
    show_inactive = request.GET.get("inactive") == "1"
    export_csv = request.GET.get("export") == "csv"

    users = User.objects.all().order_by("-date_joined")
    if not show_inactive:
        users = users.filter(is_active=True)
    if query:
        users = users.filter(Q(username__icontains=query) | Q(email__icontains=query))
    if selected_plan:
        users = users.filter(subscription__plan__slug=selected_plan)

    if not export_csv:
        users = list(users[:100])
    else:
        users = list(users[:5000])

    rows = []
    for u in users:
        sub = get_or_create_subscription(u)
        summary = subscription_summary(u)
        tokens = LLMAppUsageTotals.get_for_user(u)
        rows.append(
            {
                "user": u,
                "plan_slug": summary.get("plan_slug"),
                "plan_name": summary.get("plan_name"),
                "sub_status": sub.status,
                "email_verified": is_email_verified(u),
                "usage": summary.get("usage") or {},
                "storage": summary.get("storage") or {},
                "tokens": {
                    "input": int(tokens.total_input_tokens or 0),
                    "output": int(tokens.total_output_tokens or 0),
                    "requests": int(tokens.total_requests or 0),
                    "estimated_invokes": int(tokens.total_estimated_invokes or 0),
                },
            }
        )

    if export_csv:
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="hireedge-users-export.csv"'
        writer = csv.writer(response)
        writer.writerow([
            "User ID",
            "Username",
            "Email",
            "Date Joined",
            "Plan",
            "Subscription Status",
            "Email Verified",
            "Is Active",
            "Is Staff",
            "LLM Requests Today",
            "Searches Today",
            "Applies Today",
            "Storage (MB)",
            "Lifetime Input Tokens",
            "Lifetime Output Tokens",
            "Lifetime Total Requests",
        ])
        for r in rows:
            u = r["user"]
            usg = r.get("usage") or {}
            tok = r.get("tokens") or {}
            stg = r.get("storage") or {}
            writer.writerow([
                u.pk,
                u.username,
                u.email or "",
                u.date_joined.strftime("%Y-%m-%d %H:%M:%S") if u.date_joined else "",
                r.get("plan_name") or r.get("plan_slug") or "",
                r.get("sub_status") or "",
                "Yes" if r.get("email_verified") else "No",
                "Active" if u.is_active else "Suspended",
                "Yes" if u.is_staff else "No",
                usg.get("llm_requests", {}).get("used", 0),
                usg.get("job_searches", {}).get("used", 0),
                usg.get("apply_runs", {}).get("used", 0),
                stg.get("used_mb", 0),
                tok.get("input", 0),
                tok.get("output", 0),
                tok.get("requests", 0),
            ])
        return response

    return render(
        request,
        "resume_app/staff/users.html",
        {
            "rows": rows,
            "query": query,
            "selected_plan": selected_plan,
            "show_inactive": show_inactive,
            "plans": list(Plan.objects.filter(is_active=True).order_by("sort_order")),
        },
    )


@login_required
@permission_required("resume_app.can_impersonate_users", raise_exception=True)
@require_http_methods(["POST"])
def staff_user_action_view(request, user_id: int):
    target = get_object_or_404(User, pk=user_id)
    action = (request.POST.get("action") or "").strip()

    # Plan assignment is allowed for staff (dev/support); suspend/activate stay non-staff only.
    if action in ("suspend", "activate") and (target.is_superuser or target.is_staff):
        messages.error(request, "Cannot suspend or activate staff accounts here.")
        return redirect("staff_users")

    if action == "suspend":
        target.is_active = False
        target.save(update_fields=["is_active"])
        messages.success(request, f"Suspended {target.username}.")
    elif action == "activate":
        target.is_active = True
        target.save(update_fields=["is_active"])
        messages.success(request, f"Activated {target.username}.")
    elif action == "assign_plan":
        slug = (request.POST.get("plan_slug") or "").strip()
        try:
            assign_plan(target, slug)
            messages.success(request, f"Assigned plan {slug} to {target.username}.")
        except Exception as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, "Unknown action.")
    return redirect("staff_users")
