"""Tests for the stateless-deployment foundations (locks, probes, timeouts, throttles)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from resume_app.abuse import _client_ip, is_rate_limited
from resume_app.coordination import scheduled_fire
from resume_app.llm.policy import LLMInvokeTimeout, run_with_invoke_timeout
from resume_app.tasks import acquire_periodic_leader_lock


class ScheduledFireTests(TestCase):
    def test_daily_fire_floors_to_0130_not_midnight(self):
        now = timezone.make_aware(datetime(2026, 11, 8, 1, 45))
        fire = scheduled_fire("30 1 * * *", now)
        self.assertEqual(fire.hour, 1)
        self.assertEqual(fire.minute, 30)
        self.assertEqual(fire.date().isoformat(), "2026-11-08")

    def test_before_the_daily_fire_uses_the_previous_day(self):
        now = timezone.make_aware(datetime(2026, 11, 8, 0, 10))
        fire = scheduled_fire("30 1 * * *", now)
        self.assertEqual(fire.date().isoformat(), "2026-11-07")
        self.assertEqual((fire.hour, fire.minute), (1, 30))

    def test_fifteen_minute_fire_stays_in_its_bucket_when_late(self):
        now = timezone.make_aware(datetime(2026, 11, 8, 12, 20))
        fire = scheduled_fire("*/15 * * * *", now)
        self.assertEqual((fire.hour, fire.minute), (12, 15))


class LeaderLockTests(TestCase):
    def test_same_cron_fire_allows_one_leader(self):
        first = acquire_periodic_leader_lock("pipeline_manager", cron_expr="*/30 * * * *")
        second = acquire_periodic_leader_lock("pipeline_manager", cron_expr="*/30 * * * *")
        self.assertTrue(first)
        self.assertFalse(second)


class InvokeTimeoutTests(TestCase):
    def test_backstop_returns_before_the_sleeper_finishes(self):
        started = time.monotonic()
        with self.assertRaises(LLMInvokeTimeout):
            run_with_invoke_timeout(lambda: time.sleep(5), timeout_seconds=0.2)
        self.assertLess(time.monotonic() - started, 1.5)


class HealthProbeTests(TestCase):
    @override_settings(SECURE_SSL_REDIRECT=True, ALLOWED_HOSTS=["example.com"])
    def test_ready_accepts_pod_ip_over_http(self):
        response = Client().get("/health/ready", HTTP_HOST="10.0.0.5")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["database"], "ok")
        self.assertIn("cache_backend", body)


class AbuseThrottleTests(TestCase):
    def test_untrusted_forwarded_for_is_ignored(self):
        request = type("R", (), {"META": {
            "HTTP_X_FORWARDED_FOR": "1.2.3.4",
            "REMOTE_ADDR": "10.0.0.8",
        }})()
        with override_settings(TRUSTED_PROXY_COUNT=0):
            self.assertEqual(_client_ip(request), "10.0.0.8")

    def test_one_trusted_proxy_uses_the_appended_address(self):
        request = type("R", (), {"META": {
            "HTTP_X_FORWARDED_FOR": "1.2.3.4, 10.1.1.1",
            "REMOTE_ADDR": "10.0.0.8",
        }})()
        with override_settings(TRUSTED_PROXY_COUNT=1):
            self.assertEqual(_client_ip(request), "10.1.1.1")

    def test_fixed_window_counter_blocks_at_the_limit(self):
        with override_settings(ABUSE_THROTTLE_ENABLED=True):
            blocked = [
                is_rate_limited("auth", "10.9.9.9", limit=2, window_seconds=60)
                for _ in range(3)
            ]
        self.assertEqual(blocked, [False, False, True])


class ProductionGuardTests(TestCase):
    def test_production_without_object_storage_fails_closed(self):
        env = os.environ.copy()
        env.update({
            "APP_ENV": "production",
            "DEBUG": "False",
            "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "DJANGO_CACHE_USE_REDIS": "1",
            "AWS_STORAGE_BUCKET_NAME": "",
            "ALLOW_LOCAL_MEDIA_IN_PRODUCTION": "0",
            "FERNET_KEYS": "test-key",
        })
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, "-c", "import django; django.setup()"],
            cwd=root,
            env={**env, "DJANGO_SETTINGS_MODULE": "core.settings"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Production storage failure", completed.stderr + completed.stdout)
