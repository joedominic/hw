"""Staff-only support views."""
import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Q
from django.shortcuts import render

logger = logging.getLogger(__name__)
User = get_user_model()


@login_required
@permission_required("resume_app.can_impersonate_users", raise_exception=True)
def staff_users_view(request):
    query = (request.GET.get("q") or "").strip()
    users = User.objects.filter(is_active=True).order_by("-date_joined")
    if query:
        users = users.filter(Q(username__icontains=query) | Q(email__icontains=query))
    users = users[:100]
    return render(
        request,
        "resume_app/staff/users.html",
        {"users": users, "query": query},
    )
