"""Shared helpers for multi-tenant tests."""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from .models import Track

User = get_user_model()
TEST_PASSWORD = "test-pass-123"


TENANT_TEST_MIDDLEWARE = tuple(
    m for m in settings.MIDDLEWARE if m != "resume_app.middleware.LoginRequiredMiddleware"
)


def create_user(username: str = "testuser") -> User:
    user, created = User.objects.get_or_create(
        username=username,
        defaults={"email": f"{username}@example.com"},
    )
    if created:
        user.set_password(TEST_PASSWORD)
        user.save(update_fields=["password"])
    from .onboarding import seed_user_defaults

    seed_user_defaults(user)
    return user


def login_client(client: Client, user: User | None = None) -> User:
    user = user or create_user()
    client.login(username=user.username, password=TEST_PASSWORD)
    return user


@override_settings(MIDDLEWARE=TENANT_TEST_MIDDLEWARE)
class TenantTestCase(TestCase):
    """Base test case with a default tenant user and authenticated client."""

    username = "testuser"

    def setUp(self):
        super().setUp()
        self.user = create_user(self.username)
        self.client = Client()
        login_client(self.client, self.user)

    def owned(self, model, **kwargs):
        kwargs.setdefault("owner", self.user)
        return model.objects.create(**kwargs)
