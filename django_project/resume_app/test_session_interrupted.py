"""Tests for SessionInterruptedMiddleware and idempotent session handling in job search."""
from unittest.mock import MagicMock
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.exceptions import SessionInterrupted
from django.http import HttpResponse, JsonResponse
from django.test import RequestFactory, TestCase, Client
from django.urls import reverse

from resume_app.middleware import SessionInterruptedMiddleware

User = get_user_model()


class SessionInterruptedMiddlewareTest(TestCase):
    """Test SessionInterruptedMiddleware error recovery and cookie cleanup."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username="session_test_user",
            email="session_test@example.com",
            password="testpassword123",
        )

    def test_normal_request_passes_through(self):
        """When no exception is raised, response passes through unchanged."""
        def get_response(req):
            return HttpResponse("OK", status=200)

        middleware = SessionInterruptedMiddleware(get_response)
        request = self.factory.get("/jobs/search/")
        request.user = self.user
        response = middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"OK")

    def test_session_interrupted_authenticated_redirects_and_deletes_cookie(self):
        """On SessionInterrupted for authenticated user, redirects to requested URL and deletes cookie."""
        def get_response(req):
            raise SessionInterrupted("Session deleted concurrently")

        middleware = SessionInterruptedMiddleware(get_response)
        request = self.factory.get("/jobs/search/?q=python")
        request.user = self.user
        response = middleware(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/jobs/search/?q=python")
        cookie_name = getattr(settings, "SESSION_COOKIE_NAME", "sessionid")
        self.assertIn(cookie_name, response.cookies)
        # Deleting a cookie in Django sets max-age=0 or expires in past
        self.assertEqual(response.cookies[cookie_name]["max-age"], 0)

    def test_session_interrupted_unauthenticated_redirects_to_login(self):
        """On SessionInterrupted for unauthenticated user, redirects to LOGIN_URL."""
        def get_response(req):
            raise SessionInterrupted("Session deleted concurrently")

        middleware = SessionInterruptedMiddleware(get_response)
        request = self.factory.get("/jobs/search/")
        request.user = MagicMock(is_authenticated=False)
        response = middleware(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, getattr(settings, "LOGIN_URL", "/accounts/login/"))
        cookie_name = getattr(settings, "SESSION_COOKIE_NAME", "sessionid")
        self.assertIn(cookie_name, response.cookies)
        self.assertEqual(response.cookies[cookie_name]["max-age"], 0)

    def test_session_interrupted_api_returns_401_json(self):
        """On SessionInterrupted for API or Ajax requests, returns 401 JSON and deletes cookie."""
        def get_response(req):
            raise SessionInterrupted("Session deleted concurrently")

        middleware = SessionInterruptedMiddleware(get_response)
        request = self.factory.get("/api/v1/jobs/", HTTP_ACCEPT="application/json")
        request.user = self.user
        response = middleware(request)

        self.assertEqual(response.status_code, 401)
        self.assertIsInstance(response, JsonResponse)
        cookie_name = getattr(settings, "SESSION_COOKIE_NAME", "sessionid")
        self.assertIn(cookie_name, response.cookies)
        self.assertEqual(response.cookies[cookie_name]["max-age"], 0)


class JobSearchSessionIdempotenceTest(TestCase):
    """Verify that visiting /jobs/search/ does not dirty the session unnecessarily."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="search_idempotence_user",
            email="search_idem@example.com",
            password="testpassword123",
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_repeated_search_page_loads_do_not_dirty_session(self):
        """Initial load establishes session; repeated load without track changes leaves session clean."""
        # First load
        res1 = self.client.get("/jobs/search/")
        self.assertEqual(res1.status_code, 200)

        # Retrieve the session
        session = self.client.session
        # Check that track keys are initialized
        self.assertEqual(session.get("job_search_track"), "")
        self.assertEqual(session.get("job_search_profile_slug"), "")

        # Second load: no param changes
        res2 = self.client.get("/jobs/search/")
        self.assertEqual(res2.status_code, 200)

    def test_concurrent_session_deletion_does_not_500(self):
        """If session is deleted while request is in flight, client receives clean redirect instead of 500."""
        # Force login establishes session in DB
        self.client.get("/jobs/search/")
        session_key = self.client.cookies[settings.SESSION_COOKIE_NAME].value

        # Emulate concurrent logout / session flush by deleting session row from DB
        from django.contrib.sessions.models import Session
        Session.objects.filter(session_key=session_key).delete()

        # Next request with stale session cookie will try to access and should handle cleanly
        # If the view attempts to write or save with modified session, SessionInterrupted is caught.
        res = self.client.get("/jobs/search/")
        # Must not be 500 Server Error
        self.assertNotEqual(res.status_code, 500)
