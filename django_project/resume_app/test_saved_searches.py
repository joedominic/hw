"""Tests for SavedJobSearch presets."""

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from resume_app.experience import set_experience_mode
from resume_app.models import SavedJobSearch, Track
from resume_app.saved_searches import (
    apply_saved_search_to_get,
    backfill_saved_search_profile_tracks,
    create_or_update_saved_search,
    delete_saved_search,
    get_saved_search_or_none,
    list_saved_searches,
)


class SavedJobSearchHelpersTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="preset_user", password="pass12345!")
        Track.ensure_baseline(self.user)

    def test_create_and_load_preset(self):
        saved = create_or_update_saved_search(
            self.user,
            name="Remote Python",
            search_term="Python developer",
            location="Remote",
            profile_slug="general",
            min_score=70,
            results_wanted=30,
            site_names=["indeed", "linkedin"],
        )
        self.assertEqual(saved.name, "Remote Python")
        self.assertEqual(saved.profile_slug, "remote-python")
        self.assertEqual(saved.slug, "remote-python")
        loaded = get_saved_search_or_none(self.user, saved.id)
        self.assertIsNotNone(loaded)
        applied = apply_saved_search_to_get(loaded)
        self.assertEqual(applied["query"], "Python developer")
        self.assertEqual(applied["location"], "Remote")
        self.assertEqual(applied["min_score_raw"], "70")
        self.assertIn("indeed", applied["selected_site_names"])

    def test_duplicate_name_rejected(self):
        create_or_update_saved_search(self.user, name="Dup", search_term="a")
        with self.assertRaises(ValueError):
            create_or_update_saved_search(self.user, name="Dup", search_term="b")

    def test_update_preset_by_id(self):
        saved = create_or_update_saved_search(self.user, name="Original", search_term="a")
        updated = create_or_update_saved_search(
            self.user,
            name="Renamed",
            search_term="b",
            preset_id=saved.id,
        )
        self.assertEqual(updated.id, saved.id)
        self.assertEqual(updated.search_term, "b")
        self.assertEqual(SavedJobSearch.objects.for_user(self.user).count(), 1)

    def test_delete_preset(self):
        saved = create_or_update_saved_search(self.user, name="To delete", search_term="x")
        delete_saved_search(self.user, saved.id)
        self.assertFalse(SavedJobSearch.objects.for_user(self.user).exists())

    def test_tenant_isolation(self):
        other = User.objects.create_user(username="other", password="pass12345!")
        Track.ensure_baseline(other)
        saved = create_or_update_saved_search(self.user, name="Mine", search_term="x")
        self.assertIsNone(get_saved_search_or_none(other, saved.id))

    def test_backfill_legacy_preset_creates_search_profile(self):
        saved = SavedJobSearch.objects.create(
            owner=self.user,
            name="FinCrimes",
            search_term="AML",
            slug="general",
            profile_slug="general",
        )
        backfill_saved_search_profile_tracks(self.user)
        saved.refresh_from_db()
        self.assertEqual(saved.slug, "fincrimes")
        self.assertEqual(saved.profile_slug, "fincrimes")

    def test_delete_preset_removes_unused_search_profile(self):
        saved = create_or_update_saved_search(self.user, name="Temp search", search_term="x")
        slug = saved.profile_slug
        delete_saved_search(self.user, saved.id)
        self.assertFalse(Track.objects.for_user(self.user).filter(slug=slug).exists())


class SavedJobSearchScheduleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="sched_user", password="pass12345!")
        Track.ensure_baseline(self.user)
        self.saved = create_or_update_saved_search(
            self.user,
            name="Nightly SWE",
            search_term="Engineer",
            location="Remote",
            site_names=["indeed"],
        )

    def test_cron_roundtrip(self):
        from resume_app.saved_search_schedule import (
            cron_from_schedule,
            schedule_from_cron,
        )

        for interval in ("daily", "weekdays", "weekly"):
            cron = cron_from_schedule(interval, "08:30")
            parsed = schedule_from_cron(cron)
            self.assertEqual(parsed, (interval, "08:30"))

    def test_set_schedule_creates_task(self):
        from resume_app.models import JobSearchTask
        from resume_app.saved_search_schedule import set_saved_search_schedule

        task = set_saved_search_schedule(
            self.user,
            self.saved.id,
            interval="daily",
            time_str="09:00",
        )
        self.assertIsNotNone(task)
        self.assertTrue(task.is_active)
        self.assertEqual(task.frequency, "0 9 * * *")
        self.assertEqual(task.saved_search_id, self.saved.id)
        self.assertEqual(JobSearchTask.objects.for_user(self.user).count(), 1)

    def test_set_schedule_off_deactivates(self):
        from resume_app.saved_search_schedule import set_saved_search_schedule

        set_saved_search_schedule(self.user, self.saved.id, interval="daily", time_str="09:00")
        task = set_saved_search_schedule(self.user, self.saved.id, interval="off")
        task.refresh_from_db()
        self.assertFalse(task.is_active)

    def test_update_saved_search_syncs_task(self):
        from resume_app.models import JobSearchTask
        from resume_app.saved_search_schedule import set_saved_search_schedule

        set_saved_search_schedule(self.user, self.saved.id, interval="daily", time_str="09:00")
        create_or_update_saved_search(
            self.user,
            name="Nightly SWE",
            search_term="Staff Engineer",
            location="Austin",
            preset_id=self.saved.id,
        )
        task = JobSearchTask.objects.for_user(self.user).get(saved_search=self.saved)
        self.assertEqual(task.search_term, "Staff Engineer")
        self.assertEqual(task.location, "Austin")


class SavedJobSearchScheduleViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username="sched_view", password="pass12345!")
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        Track.ensure_baseline(self.user)
        set_experience_mode(self.user, "power")
        self.client.login(username="sched_view", password="pass12345!")
        self.saved = create_or_update_saved_search(
            self.user,
            name="Scheduled search",
            search_term="Python",
            site_names=["indeed"],
        )

    def test_schedule_post_creates_task(self):
        from resume_app.models import JobSearchTask

        resp = self.client.post(
            reverse("jobs_search"),
            {
                "action": "schedule_saved_search",
                "preset_id": str(self.saved.id),
                "schedule_interval": "weekdays",
                "schedule_time": "07:30",
            },
        )
        self.assertEqual(resp.status_code, 302)
        task = JobSearchTask.objects.for_user(self.user).get(saved_search=self.saved)
        self.assertEqual(task.frequency, "30 7 * * 1-5")
        self.assertTrue(task.is_active)

    def test_schedule_panel_on_selected_preset(self):
        url = reverse("jobs_search") + f"?preset={self.saved.id}"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Automatic runs")
        self.assertContains(resp, "schedule_interval")
        self.assertContains(resp, "Save schedule")

    def test_normal_user_sees_schedule_panel(self):
        set_experience_mode(self.user, "normal")
        url = reverse("jobs_search") + f"?preset={self.saved.id}"
        resp = self.client.get(url)
        self.assertContains(resp, "Save schedule")
        self.assertContains(resp, "Automatic runs")


class SavedJobSearchViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username="view_user", password="pass12345!")
        Track.ensure_baseline(self.user)
        self.client.login(username="view_user", password="pass12345!")
        self.saved = create_or_update_saved_search(
            self.user,
            name="Dallas SWE",
            search_term="Software Engineer",
            location="Dallas",
            profile_slug="general",
            site_names=["indeed"],
        )

    def test_load_preset_sets_search_profile_track(self):
        backfill_saved_search_profile_tracks(self.user)
        self.saved.refresh_from_db()
        url = reverse("jobs_search") + f"?preset={self.saved.id}"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'?preset={self.saved.id}')
        self.assertContains(resp, "Dallas SWE")
        self.assertContains(resp, f'value="{self.saved.slug}"', html=False)

    def test_save_preset_post(self):
        resp = self.client.post(
            reverse("jobs_search"),
            {
                "action": "save_saved_search",
                "saved_search_name": "New preset",
                "q": "DevOps",
                "location": "Austin",
                "track": "general",
                "min_score": "60",
                "results_wanted": "40",
                "site_name": ["indeed", "dice"],
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("from_save=1", resp["Location"])
        preset = SavedJobSearch.objects.for_user(self.user).get(name="New preset")
        self.assertEqual(preset.search_term, "DevOps")
        self.assertEqual(preset.location, "Austin")
        self.assertEqual(preset.profile_slug, "new-preset")
        self.assertEqual(preset.slug, "new-preset")

    def test_save_as_new_while_preset_selected(self):
        resp = self.client.post(
            reverse("jobs_search"),
            {
                "action": "save_saved_search",
                "save_as_new": "1",
                "preset_id": str(self.saved.id),
                "saved_search_name": "Principal Plano",
                "q": "Principal engineer",
                "location": "Plano, TX",
                "track": self.saved.profile_slug,
                "results_wanted": "50",
                "site_name": ["indeed", "linkedin"],
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(SavedJobSearch.objects.for_user(self.user).count(), 2)
        new_preset = SavedJobSearch.objects.for_user(self.user).get(name="Principal Plano")
        self.assertEqual(new_preset.search_term, "Principal engineer")

    def test_diverged_search_keeps_preset_context(self):
        """Changing criteria while ?preset= stays in context so Update can persist."""
        backfill_saved_search_profile_tracks(self.user)
        self.saved.refresh_from_db()
        url = (
            reverse("jobs_search")
            + f"?preset={self.saved.id}&q=Principal+engineer&location=Plano%2C+TX"
            + "&site_name=indeed&site_name=dice"
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Working in saved search")
        self.assertContains(resp, "Unsaved changes")
        self.assertContains(resp, "Save changes to Dallas SWE")
        self.assertContains(resp, f'id="save-preset-id" value="{self.saved.id}"', html=False)
        self.assertNotContains(resp, "Save current search")

    def test_update_preset_persists_sites_and_criteria(self):
        resp = self.client.post(
            reverse("jobs_search"),
            {
                "action": "save_saved_search",
                "preset_id": str(self.saved.id),
                "saved_search_name": "Dallas SWE",
                "q": "Staff engineer",
                "location": "Remote",
                "track": self.saved.profile_slug or "general",
                "results_wanted": "75",
                "site_name": ["dice", "linkedin"],
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("from_save=1", resp["Location"])
        self.saved.refresh_from_db()
        self.assertEqual(self.saved.search_term, "Staff engineer")
        self.assertEqual(self.saved.location, "Remote")
        self.assertEqual(self.saved.results_wanted, 75)
        self.assertEqual(set(self.saved.site_names), {"dice", "linkedin"})

    def test_save_ajax_returns_json_without_requiring_board_search(self):
        resp = self.client.post(
            reverse("jobs_search"),
            {
                "action": "save_saved_search",
                "preset_id": str(self.saved.id),
                "saved_search_name": "Dallas SWE",
                "q": "Staff engineer",
                "location": "Remote",
                "track": self.saved.profile_slug or "general",
                "results_wanted": "50",
                "site_name": ["indeed"],
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["success"])
        self.assertIn("from_save=1", payload["preset"]["url"])
        self.assertEqual(payload["preset"]["name"], "Dallas SWE")

    def test_from_save_skips_board_search_and_restores_session_results(self):
        session = self.client.session
        session["job_search_display"] = {
            "jobs": [
                {
                    "id": 1,
                    "title": "Cached Role",
                    "company_name": "Acme",
                    "location": "Remote",
                    "snippet": "snippet",
                    "url": "https://example.com",
                    "source": "indeed",
                    "posted_at": "2026-03-01T12:00:00+00:00",
                }
            ],
            "total": 1,
        }
        session.save()
        url = (
            reverse("jobs_search")
            + f"?preset={self.saved.id}&q=Software+Engineer&location=Dallas&from_save=1"
            + "&site_name=indeed"
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Cached Role")
        self.assertContains(resp, "Working in saved search")

    def test_update_preset_allows_empty_name(self):
        """Update keeps the existing name when the name field is blank."""
        updated = create_or_update_saved_search(
            self.user,
            name="",
            search_term="Updated term",
            location="Austin",
            site_names=["adzuna"],
            preset_id=self.saved.id,
        )
        self.assertEqual(updated.name, "Dallas SWE")
        self.assertEqual(updated.search_term, "Updated term")
        self.assertEqual(updated.site_names, ["adzuna"])

    def test_delete_preset_post(self):
        resp = self.client.post(
            reverse("jobs_search"),
            {"action": "delete_saved_search", "preset_id": str(self.saved.id)},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(SavedJobSearch.objects.for_user(self.user).exists())

    def test_job_task_create_prefill_from_preset(self):
        url = reverse("job_task_create") + f"?preset={self.saved.id}"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Software Engineer")
        self.assertContains(resp, "Dallas SWE")

    def test_normal_user_collapsed_search_options(self):
        set_experience_mode(self.user, "normal")
        resp = self.client.get(reverse("jobs_search"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "job-search-options")
        self.assertContains(resp, "Save search")
        self.assertNotContains(resp, 'id="job-search-options" open')
        self.assertNotContains(resp, 'id="llm-model-select"')

    def test_power_user_expanded_search_options(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        set_experience_mode(self.user, "power")
        resp = self.client.get(reverse("jobs_search"))
        self.assertEqual(resp.status_code, 200)
        self.assertRegex(
            resp.content.decode(),
            r'<details[^>]*id="job-search-options"[^>]*\sopen',
        )
