from django.test import TestCase, Client, override_settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from datetime import timedelta
from unittest.mock import patch, MagicMock
from .models import (
    AppAutomationSettings,
    JobListingTrackMetrics,
    UserResume,
    JobDescription,
    OptimizedResume,
    JobListing,
    PipelineEntry,
    Track,
)
from .resume_keyword_miner import mine_keywords_from_jobs
from .tasks import (
    apply_pipeline_auto_promotions,
    apply_vetting_to_applying_promotions,
    _enqueue_single_pipeline_resume_optimization,
)
from .api import _normalize_export_content
from .services import parse_pdf, PDFParseError
from .llm_services import LLM_PROVIDERS
from .job_sources import (
    _dedupe_fetch_rows,
    _per_site_results_cap,
    _row_to_dict,
    fetch_jobs,
    normalize_site_names,
    upsert_job_listing_from_fetch,
)
from .adzuna_client import _adzuna_result_to_dict, fetch_adzuna_jobs
from .dice_client import _parse_jobs_from_html
from .utils import format_job_source_label
from .views import MAX_TRACK_RESUME_UPLOAD_BYTES, _count_unique_library_resumes
from .test_utils import TenantTestCase, create_user, login_client, TEST_PASSWORD
import json
import os


class ModelTestCase(TestCase):
    def test_model_creation(self):
        user = create_user("modeltest")
        resume = UserResume.objects.create(owner=user, file="test.pdf")
        jd = JobDescription.objects.create(content="Test JD")
        optimized = OptimizedResume.objects.create(
            owner=user, original_resume=resume, job_description=jd
        )
        self.assertEqual(optimized.status, OptimizedResume.STATUS_QUEUED)

    def test_pipelineentry_stage_helpers(self):
        user = create_user("stagehelper")
        job = JobListing.objects.create(
            source="test",
            external_id="1",
            title="T",
            company_name="C",
        )
        pe = PipelineEntry.objects.create(owner=user, job_listing=job, track="ic")
        # Default stage is blank / pipeline
        self.assertEqual(pe.stage, "")
        pe.move_to_vetting()
        self.assertEqual(pe.stage, PipelineEntry.Stage.VETTING)
        pe.move_to_applying()
        self.assertEqual(pe.stage, PipelineEntry.Stage.APPLYING)
        pe.mark_done()
        self.assertEqual(pe.stage, PipelineEntry.Stage.DONE)

class ServiceTestCase(TestCase):
    def test_parse_pdf_placeholder(self):
        self.assertTrue(callable(parse_pdf))

    def test_parse_pdf_raises_on_missing_file(self):
        with self.assertRaises(PDFParseError) as ctx:
            parse_pdf("/nonexistent/path.pdf")
        self.assertIn("not found", str(ctx.exception).lower())

    def test_parse_pdf_raises_on_empty_path(self):
        with self.assertRaises(PDFParseError):
            parse_pdf("")

    def test_normalize_export_content_replaces_unusual_hyphens(self):
        source = "Strategic engineering leader with 20+ years of experience delivering high\u2011impact software solutions"
        normalized = _normalize_export_content(source)
        self.assertNotIn("\u2011", normalized)
        self.assertIn("high-impact", normalized)


class NormalizeSiteNamesTestCase(TestCase):
    def test_drops_removed_boards(self):
        self.assertEqual(
            normalize_site_names(["indeed", "glassdoor", "google", "zip_recruiter"]),
            ["indeed"],
        )

    def test_empty_or_all_removed_defaults_to_indeed(self):
        self.assertEqual(normalize_site_names([]), ["indeed"])
        self.assertEqual(normalize_site_names(None), ["indeed"])
        self.assertEqual(normalize_site_names(["glassdoor"]), ["indeed"])

    def test_preserves_indeed_and_linkedin(self):
        self.assertEqual(
            normalize_site_names(["linkedin", "indeed"]),
            ["linkedin", "indeed"],
        )

    def test_preserves_dice_and_adzuna(self):
        self.assertEqual(
            normalize_site_names(["dice", "adzuna", "indeed"]),
            ["dice", "adzuna", "indeed"],
        )

    def test_preserves_levels(self):
        self.assertEqual(
            normalize_site_names(["levels", "indeed"]),
            ["levels", "indeed"],
        )

    def test_preserves_builtin(self):
        self.assertEqual(
            normalize_site_names(["builtin", "indeed"]),
            ["builtin", "indeed"],
        )


class JobFetchHelpersTestCase(TestCase):
    def test_per_site_results_cap(self):
        self.assertEqual(_per_site_results_cap(50, 4), 12)
        self.assertEqual(_per_site_results_cap(50, 1), 50)
        self.assertEqual(_per_site_results_cap(20, 3), 10)

    def test_dedupe_fetch_rows(self):
        rows = [
            {"source": "adzuna", "external_id": "a1"},
            {"source": "adzuna", "external_id": "a1"},
            {"source": "dice", "external_id": "d1"},
        ]
        self.assertEqual(len(_dedupe_fetch_rows(rows)), 2)


class AdzunaClientTestCase(TestCase):
    def test_adzuna_result_to_dict(self):
        row = _adzuna_result_to_dict(
            {
                "id": 42,
                "title": "Python Dev",
                "company": {"display_name": "Acme"},
                "location": {"display_name": "Boston, MA"},
                "description": "Build APIs",
                "redirect_url": "https://example.com/job/42",
                "created": "2026-01-15T12:00:00Z",
            },
            "us",
        )
        self.assertEqual(row["source"], "adzuna")
        self.assertEqual(row["external_id"], "adzuna:us:42")
        self.assertEqual(row["company_name"], "Acme")
        self.assertEqual(row["job_url"], "https://example.com/job/42")

    @patch("resume_app.adzuna_client.requests.get")
    def test_fetch_adzuna_jobs_requires_keys(self, mock_get):
        with self.assertRaises(RuntimeError) as ctx:
            fetch_adzuna_jobs("engineer", location="Boston", results_wanted=5)
        self.assertIn("not configured", str(ctx.exception))
        mock_get.assert_not_called()

    @patch("resume_app.adzuna_client.requests.get")
    @patch("resume_app.adzuna_client._adzuna_credentials", return_value=("id", "key"))
    def test_fetch_adzuna_jobs_parses_response(self, _creds, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {
                    "id": 1,
                    "title": "Dev",
                    "company": {"display_name": "Co"},
                    "location": {"display_name": "NYC"},
                    "description": "Desc",
                    "redirect_url": "https://adzuna.com/1",
                }
            ]
        }
        mock_get.return_value = mock_resp
        rows = fetch_adzuna_jobs("dev", results_wanted=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "adzuna")

    def test_upsert_adzuna_listing(self):
        row = {
            "title": "Dev",
            "company_name": "Co",
            "location": "NYC",
            "description": "Desc",
            "job_url": "https://adzuna.com/1",
            "source": "adzuna",
            "external_id": "adzuna:us:1",
        }
        job, created = upsert_job_listing_from_fetch(row)
        self.assertTrue(created)
        self.assertEqual(job.source, "adzuna")


class DiceClientTestCase(TestCase):
    def test_parse_jobs_from_html_empty(self):
        self.assertEqual(_parse_jobs_from_html("<html></html>", set()), [])

    def test_parse_jobs_from_html_card_link_titles(self):
        html = """
        <a aria-label="View Details for Director, Financial Crimes Advisory Data &amp; Analytics (0e5569e4d3144a2d89c7e498a2484ce7)"
           data-testid="job-search-job-card-link"
           href="/job-detail/0e5569e4-d314-4a2d-89c7-e498a2484ce7"></a>
        <a href="/company-profile/x?companyname=AML%20RightSource"></a>
        <a aria-label="View Details for Financial Crimes - Senior Data Scientist (9ce3c73e9abe181fb4190fa4ad7411c5)"
           data-testid="job-search-job-card-link"
           href="/job-detail/e072a75c-ac31-4f49-be39-ad7c040a3673"></a>
        <img alt="KeyCorp" />
        """
        rows = _parse_jobs_from_html(html, set())
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            rows[0]["title"],
            "Director, Financial Crimes Advisory Data & Analytics",
        )
        self.assertNotEqual(rows[0]["title"], "Untitled")
        self.assertEqual(rows[1]["title"], "Financial Crimes - Senior Data Scientist")

    def test_normalize_dice_api_query_strips_quotes(self):
        from resume_app.dice_client import _normalize_dice_api_query

        self.assertEqual(
            _normalize_dice_api_query('"financial crimes" technology'),
            "financial crimes technology",
        )

    def test_parse_jobposting_from_html(self):
        from resume_app.dice_client import _parse_jobposting_from_html

        html = """
        <html><head>
        <script type="application/ld+json">
        {
          "@type": "JobPosting",
          "title": "Senior Director, Software Engineering",
          "description": "<b>What you'll do...</b><br /><br /><b>Duties:</b> Analyze the requirements and build systems.",
          "hiringOrganization": {"@type": "Organization", "name": "Walmart Inc."},
          "jobLocation": {
            "@type": "Place",
            "address": {
              "@type": "PostalAddress",
              "addressLocality": "Dallas",
              "addressRegion": "TX",
              "addressCountry": "US"
            }
          },
          "datePosted": "2026-07-05T00:00:00Z"
        }
        </script>
        </head><body></body></html>
        """
        parsed = _parse_jobposting_from_html(html)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["title"], "Senior Director, Software Engineering")
        self.assertEqual(parsed["company_name"], "Walmart Inc.")
        self.assertIn("Dallas", parsed["location"])
        self.assertIn("Analyze the requirements", parsed["description"])
        self.assertNotIn("<b>", parsed["description"])

    def test_extract_dice_guid(self):
        from resume_app.dice_client import extract_dice_guid

        self.assertEqual(
            extract_dice_guid("https://www.dice.com/job-detail/aeba63c5-d102-4999-b7f4-5e5040e8da20"),
            "aeba63c5-d102-4999-b7f4-5e5040e8da20",
        )
        self.assertEqual(
            extract_dice_guid("dice:aeba63c5-d102-4999-b7f4-5e5040e8da20"),
            "aeba63c5-d102-4999-b7f4-5e5040e8da20",
        )

    def test_format_job_source_labels(self):
        self.assertEqual(format_job_source_label("adzuna"), "Adzuna")
        self.assertEqual(format_job_source_label("dice"), "Dice")
        self.assertEqual(format_job_source_label("jobspy_indeed"), "Indeed")
        self.assertEqual(format_job_source_label("builtin"), "Built In")

    def test_api_item_to_dict_normalizes_fields(self):
        from resume_app.dice_client import _api_item_to_dict

        row = _api_item_to_dict(
            {
                "id": "abc123",
                "title": "Python Engineer",
                "companyName": "Acme",
                "jobLocation": {"displayName": "Austin, Texas, USA"},
                "detailsPageUrl": "https://www.dice.com/job-detail/guid-1",
                "postedDate": "2026-07-01T12:00:00Z",
                "summary": "Build APIs in Python.",
            }
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["source"], "dice")
        self.assertEqual(row["external_id"], "dice:abc123")
        self.assertEqual(row["title"], "Python Engineer")
        self.assertEqual(row["company_name"], "Acme")
        self.assertEqual(row["location"], "Austin, Texas, USA")
        self.assertEqual(row["description"], "Build APIs in Python.")
        self.assertEqual(row["job_url"], "https://www.dice.com/job-detail/guid-1")
        self.assertIn("date_posted", row)

    @patch("resume_app.dice_client.requests.Session")
    def test_fetch_dice_jobs_uses_api(self, mock_session_cls):
        from resume_app.dice_client import fetch_dice_jobs

        session = mock_session_cls.return_value.__enter__.return_value
        response = session.get.return_value
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": [
                {
                    "id": "job-1",
                    "title": "Backend Engineer",
                    "companyName": "DiceCo",
                    "jobLocation": {"city": "Remote", "state": ""},
                    "detailsPageUrl": "https://www.dice.com/job-detail/guid-2",
                    "summary": "Remote Python role",
                    "postedDate": "2026-07-08T00:00:00Z",
                }
            ],
            "meta": {"currentPage": 1, "pageSize": 20, "totalResults": 1},
        }
        rows = fetch_dice_jobs("python", results_wanted=5, hours_old=168)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["external_id"], "dice:job-1")
        self.assertEqual(rows[0]["description"], "Remote Python role")
        session.get.assert_called()
        called_url = session.get.call_args[0][0]
        self.assertIn("job-search-api.svc.dhigroupinc.com", called_url)

    @patch("resume_app.dice_client._fetch_dice_jobs_api", side_effect=RuntimeError("api down"))
    @patch("resume_app.dice_client._fetch_dice_jobs_html")
    def test_fetch_dice_jobs_falls_back_to_html(self, mock_html, mock_api):
        from resume_app.dice_client import fetch_dice_jobs

        mock_html.return_value = [
            {
                "title": "Fallback Job",
                "company_name": "Co",
                "location": "NY",
                "description": "",
                "job_url": "https://www.dice.com/job-detail/x",
                "source": "dice",
                "external_id": "dice:x",
            }
        ]
        rows = fetch_dice_jobs("python", results_wanted=3)
        self.assertEqual(len(rows), 1)
        mock_api.assert_called_once()
        mock_html.assert_called_once()


class LevelsClientTestCase(TestCase):
    def test_slugify_search_term(self):
        from resume_app.levels_client import _slugify

        self.assertEqual(
            _slugify("Software Engineering Manager"),
            "software-engineering-manager",
        )

    def test_levels_location_slug_us_aliases(self):
        from resume_app.levels_client import _levels_location_slug

        self.assertEqual(_levels_location_slug("US"), "united-states")
        self.assertEqual(_levels_location_slug("USA"), "united-states")
        self.assertEqual(_levels_location_slug("United States"), "united-states")
        self.assertEqual(_levels_location_slug(""), "united-states")
        self.assertEqual(_levels_location_slug("San Francisco"), "san-francisco-bay-area")

    def test_locations_match_country_slug(self):
        from resume_app.levels_client import _locations_match_country_slug

        self.assertTrue(
            _locations_match_country_slug(
                ["San Francisco, California, United States"],
                "united-states",
            )
        )
        self.assertTrue(
            _locations_match_country_slug(["Chicago, Illinois"], "united-states")
        )
        self.assertFalse(
            _locations_match_country_slug(["Bangalore, IND"], "united-states")
        )
        self.assertFalse(
            _locations_match_country_slug(["Dublin, Ireland"], "united-states")
        )

    def test_resolve_levels_search_params_staff_engineer(self):
        from resume_app.levels_client import _resolve_levels_search_params

        params = _resolve_levels_search_params("Staff Software Development Engineer")
        self.assertEqual(params["job_family_slug"], "software-engineer")
        self.assertIsNone(params["job_title_slug"])
        self.assertTrue(params["filter_title_client_side"])

    def test_title_matches_search(self):
        from resume_app.levels_client import _title_matches_search

        self.assertTrue(
            _title_matches_search(
                "Staff Software Development Engineer",
                "Staff Software Engineer",
            )
        )
        self.assertFalse(
            _title_matches_search("Product Manager", "Staff Software Engineer")
        )

    def test_extract_levels_job_id(self):
        from resume_app.levels_client import extract_levels_job_id

        self.assertEqual(
            extract_levels_job_id("levels:103969804544549574"),
            "103969804544549574",
        )
        self.assertEqual(
            extract_levels_job_id(
                "https://www.levels.fyi/jobs?jobId=103969804544549574"
            ),
            "103969804544549574",
        )

    def test_flatten_search_results(self):
        from resume_app.levels_client import _flatten_search_results

        rows = _flatten_search_results(
            {
                "results": [
                    {
                        "companyName": "Acme",
                        "jobs": [
                            {
                                "id": "99",
                                "title": "Engineering Manager",
                                "locations": ["Boston, MA"],
                                "applicationUrl": "https://example.com/apply",
                                "postingDate": "2026-01-15T12:00:00Z",
                                "minBaseSalary": 200000,
                                "maxBaseSalary": 250000,
                                "baseSalaryCurrency": "USD",
                            }
                        ],
                    }
                ]
            }
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "levels")
        self.assertEqual(rows[0]["external_id"], "levels:99")
        self.assertEqual(rows[0]["company_name"], "Acme")
        self.assertEqual(rows[0]["job_url"], "https://example.com/apply")
        self.assertIn("Salary:", rows[0]["description"])

    def test_decrypt_payload_roundtrip(self):
        import base64
        import hashlib
        import zlib

        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from resume_app.levels_client import _decrypt_payload

        original = {"results": [], "total": 0, "totalMatchingJobs": 0}
        compressed = zlib.compress(json.dumps(original).encode("utf-8"))
        pad_len = 16 - (len(compressed) % 16)
        padded = compressed + bytes([pad_len]) * pad_len
        key = base64.b64encode(hashlib.md5(b"levelstothemoon!!").digest())[:16]
        cipher = Cipher(algorithms.AES(key), modes.ECB())
        encryptor = cipher.encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        payload_b64 = base64.b64encode(encrypted).decode("ascii")
        decoded = _decrypt_payload(payload_b64)
        self.assertEqual(decoded, original)

    def test_format_job_source_label_levels(self):
        self.assertEqual(format_job_source_label("levels"), "Levels.fyi")

    @patch("resume_app.levels_client._levels_request")
    def test_fetch_levels_jobs_paginates(self, mock_request):
        from resume_app.levels_client import fetch_levels_jobs

        mock_request.side_effect = [
            {
                "results": [
                    {
                        "companyName": "Co",
                        "jobs": [
                            {
                                "id": "1",
                                "title": "Staff Software Engineer",
                                "locations": ["Boston, Massachusetts, United States"],
                            }
                        ],
                    }
                ],
                "total": 10,
            },
            {
                "results": [
                    {
                        "companyName": "Co2",
                        "jobs": [
                            {
                                "id": "2",
                                "title": "Senior Software Engineer",
                                "locations": ["San Francisco, California, United States"],
                            }
                        ],
                    }
                ],
                "total": 10,
            },
        ]
        rows = fetch_levels_jobs(
            "Staff Software Engineer",
            location="United States",
            results_wanted=2,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(mock_request.call_count, 2)


class BuiltInClientTestCase(TestCase):
    def test_resolve_builtin_base_url(self):
        from resume_app.builtin_client import _resolve_builtin_base_url

        self.assertEqual(_resolve_builtin_base_url("Chicago, IL"), "https://www.builtinchicago.org")
        self.assertEqual(_resolve_builtin_base_url("NYC"), "https://www.builtinnyc.com")
        self.assertEqual(_resolve_builtin_base_url("San Francisco"), "https://www.builtinsf.com")
        self.assertEqual(_resolve_builtin_base_url("Boston"), "https://www.builtinboston.com")
        self.assertEqual(_resolve_builtin_base_url("Los Angeles"), "https://www.builtinla.com")
        self.assertEqual(_resolve_builtin_base_url("Seattle"), "https://www.builtinseattle.com")
        self.assertEqual(_resolve_builtin_base_url("Austin"), "https://www.builtinaustin.com")
        self.assertEqual(_resolve_builtin_base_url("Colorado"), "https://www.builtincolorado.com")
        self.assertEqual(_resolve_builtin_base_url("Remote"), "https://builtin.com")
        self.assertEqual(_resolve_builtin_base_url(""), "https://builtin.com")

    def test_parse_builtin_relative_date(self):
        from resume_app.builtin_client import _parse_builtin_relative_date

        self.assertIsNotNone(_parse_builtin_relative_date("Reposted 15 Minutes Ago"))
        self.assertIsNotNone(_parse_builtin_relative_date("6 Hours Ago"))
        self.assertIsNotNone(_parse_builtin_relative_date("2 Days Ago"))
        self.assertIsNotNone(_parse_builtin_relative_date("1 Week Ago"))
        self.assertIsNotNone(_parse_builtin_relative_date("Yesterday"))
        self.assertIsNotNone(_parse_builtin_relative_date("Today"))

    def test_extract_builtin_job_id(self):
        from resume_app.builtin_client import extract_builtin_job_id

        self.assertEqual(extract_builtin_job_id("10492500"), "10492500")
        self.assertEqual(extract_builtin_job_id("builtin:10492500"), "10492500")
        self.assertEqual(extract_builtin_job_id("https://builtin.com/job/senior-data-science-engineer/10201876"), "10201876")

    @patch("resume_app.builtin_client.requests.get")
    def test_fetch_builtin_jobs_parses_html_and_ld(self, mock_get):
        from resume_app.builtin_client import fetch_builtin_jobs

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = """
        <html>
        <head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@graph": [
            {
              "@type": "ItemList",
              "itemListElement": [
                {
                  "@type": "ListItem",
                  "position": 1,
                  "name": "Senior Python Engineer",
                  "url": "https://builtin.com/job/senior-python-engineer/10201876",
                  "description": "Clean list item snippet."
                }
              ]
            }
          ]
        }
        </script>
        </head>
        <body>
        <div id="job-card-10201876" class="job-bounded-responsive position-relative bg-white p-md rounded-3">
            <a href="/company/draftkings" class="align-items-center">DraftKings</a>
            <a href="/company/draftkings" data-id="company-title">DraftKings</a>
            <a href="/job/senior-python-engineer/10201876" data-id="job-card-title" class="card-alias-after-overlay">Senior Python Engineer</a>
            <div class="bounded-attribute-section">
                <span>Reposted 15 Minutes Ago</span>
                <span>Hybrid</span>
                <span>Boston, MA, USA</span>
                <span>Senior level</span>
            </div>
        </div>
        </body>
        </html>
        """
        mock_get.return_value = mock_resp

        jobs = fetch_builtin_jobs("python", location="Boston", results_wanted=5)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "Senior Python Engineer")
        self.assertEqual(jobs[0]["company_name"], "DraftKings")
        self.assertEqual(jobs[0]["location"], "Boston, MA, USA (Hybrid)")
        self.assertEqual(jobs[0]["description"], "Clean list item snippet.")
        self.assertEqual(jobs[0]["source"], "builtin")
        self.assertEqual(jobs[0]["external_id"], "builtin:10201876")

    @patch("resume_app.builtin_client.requests.get")
    def test_fetch_builtin_job_detail_parses_jobposting(self, mock_get):
        from resume_app.builtin_client import fetch_builtin_job_detail

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = """
        <html>
        <head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@graph": [
            {
              "@type": "JobPosting",
              "title": "Senior Python Engineer",
              "description": "<p>Build great APIs in Python.</p><br/>- Use AWS",
              "hiringOrganization": {
                "@type": "Organization",
                "name": "DraftKings"
              },
              "jobLocation": {
                "@type": "Place",
                "address": {
                  "@type": "PostalAddress",
                  "addressLocality": "Boston",
                  "addressRegion": "MA",
                  "addressCountry": "USA"
                }
              },
              "datePosted": "2026-08-01T14:52:10Z"
            }
          ]
        }
        </script>
        </head>
        </html>
        """
        mock_get.return_value = mock_resp

        detail = fetch_builtin_job_detail("https://builtin.com/job/senior-python-engineer/10201876")
        self.assertEqual(detail["title"], "Senior Python Engineer")
        self.assertEqual(detail["company_name"], "DraftKings")
        self.assertEqual(detail["location"], "Boston, MA, USA")
        self.assertEqual(detail["description"], "Build great APIs in Python.\n\n- Use AWS")

    @patch("resume_app.builtin_client.fetch_builtin_job_detail")
    def test_enrich_builtin_job_listing_description(self, mock_detail):
        from datetime import datetime
        from resume_app.builtin_client import enrich_builtin_job_listing_description
        from resume_app.models import JobListing

        mock_detail.return_value = {
            "title": "Full Rich Title",
            "company_name": "Full Co",
            "location": "Boston, MA",
            "description": "A very long detailed job description that passes the threshold easily.",
            "job_url": "https://builtin.com/job/enriched/999",
            "date_posted": datetime(2026, 8, 1, 14, 52, 10),
        }

        job = JobListing.objects.create(
            source="builtin",
            external_id="builtin:999",
            title="Untitled",
            company_name="Unknown",
            description="Short desc",
            url="https://builtin.com/job/enriched/999",
        )

        desc = enrich_builtin_job_listing_description(job)
        self.assertIn("A very long detailed", desc)
        job.refresh_from_db()
        self.assertEqual(job.title, "Full Rich Title")
        self.assertEqual(job.company_name, "Full Co")
        self.assertEqual(job.description, desc)


class FetchJobsOrchestratorTestCase(TestCase):
    @patch("resume_app.job_sources._fetch_jobs_jobspy")
    @patch("resume_app.levels_client.fetch_levels_jobs")
    @patch("resume_app.dice_client.fetch_dice_jobs")
    @patch("resume_app.adzuna_client.fetch_adzuna_jobs")
    def test_fetch_jobs_merges_providers(self, mock_adzuna, mock_dice, mock_levels, mock_jobspy):
        mock_jobspy.return_value = [
            {"source": "jobspy_indeed", "external_id": "j1", "title": "A"}
        ]
        mock_dice.return_value = [{"source": "dice", "external_id": "d1", "title": "B"}]
        mock_adzuna.return_value = [{"source": "adzuna", "external_id": "a1", "title": "C"}]
        mock_levels.return_value = [{"source": "levels", "external_id": "l1", "title": "D"}]
        rows = fetch_jobs(
            "engineer",
            site_name=["indeed", "dice", "adzuna", "levels"],
            results_wanted=40,
        )
        self.assertEqual(len(rows), 4)
        mock_jobspy.assert_called_once()
        mock_dice.assert_called_once()
        mock_adzuna.assert_called_once()
        mock_levels.assert_called_once()
        self.assertEqual(mock_jobspy.call_args[0][3], 10)


class JobSourceNormalizationTestCase(TestCase):
    def test_row_to_dict_falls_back_to_linkedin_description_keys(self):
        row = {
            "title": "Staff Engineer",
            "company": "ExampleCo",
            "location": "Remote",
            "job_url": "https://linkedin.com/jobs/view/123",
            "job_description": "LinkedIn full job description body",
        }
        normalized = _row_to_dict(row, "linkedin")
        self.assertEqual(normalized["description"], "LinkedIn full job description body")

    def test_row_to_dict_uses_summary_when_description_missing(self):
        row = {
            "title": "Staff Engineer",
            "company": "ExampleCo",
            "location": "Remote",
            "job_url": "https://linkedin.com/jobs/view/123",
            "summary": "Short LinkedIn summary fallback",
        }
        normalized = _row_to_dict(row, "linkedin")
        self.assertEqual(normalized["description"], "Short LinkedIn summary fallback")


class APITestCase(TenantTestCase):
    def setUp(self):
        super().setUp()
        from resume_app.onboarding import seed_user_defaults

        seed_user_defaults(self.user)

    def test_save_optimizer_supporting_context_in_settings_db(self):
        from .models import AppAutomationSettings

        response = self.client.post(
            "/settings/",
            data={
                "action": "save_app_automation",
                "pipeline_to_vetting_enabled": "1",
                "vetting_to_applying_enabled": "1",
                "pipeline_preference_margin_min": "0",
                "vetting_interview_probability_min": "70",
                "cleanup_pipeline_retention_days": "0",
                "cleanup_vetting_retention_days": "0",
                "cleanup_applying_retention_days": "0",
                "cleanup_done_retention_days": "0",
                "cleanup_generated_resume_retention_days": "0",
                "optimization_notes": "Emphasize leadership",
                "pipeline_skills_json": '{"hard_skills":["python"]}',
                "job_highlights": "Lead hiring projects",
            },
        )
        self.assertEqual(response.status_code, 302)
        automation = AppAutomationSettings.get_for_user(self.user)
        self.assertEqual(automation.default_optimization_notes, "Emphasize leadership")
        self.assertEqual(automation.default_pipeline_skills_json, '{"hard_skills":["python"]}')
        self.assertEqual(automation.default_job_highlights, "Lead hiring projects")

    def test_optimizer_form_prefills_supporting_context_from_db(self):
        from .models import AppAutomationSettings

        automation = AppAutomationSettings.get_for_user(self.user)
        automation.default_optimization_notes = "Emphasize leadership"
        automation.default_pipeline_skills_json = '{"hard_skills":["python"]}'
        automation.default_job_highlights = "Lead hiring projects"
        automation.save(
            update_fields=[
                "default_optimization_notes",
                "default_pipeline_skills_json",
                "default_job_highlights",
            ]
        )

        response = self.client.get("/resume/optimizer/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Emphasize leadership")
        self.assertContains(response, "hard_skills")
        self.assertContains(response, "python")
        self.assertContains(response, "Lead hiring projects")

    def test_save_supporting_context_ajax(self):
        from .models import AppAutomationSettings

        response = self.client.post(
            "/resume/optimizer/",
            data={
                "action": "save_supporting_context",
                "optimization_notes": "Focus on platform work",
                "pipeline_skills_json": "{}",
                "job_highlights": "Shipped v2",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        automation = AppAutomationSettings.get_for_user(self.user)
        self.assertEqual(automation.default_optimization_notes, "Focus on platform work")
        self.assertEqual(automation.default_job_highlights, "Shipped v2")

    def test_status_404_for_invalid_resume_id(self):
        response = self.client.get("/api/resume/status/99999/")
        self.assertEqual(response.status_code, 404)

    @patch("resume_app.api.optimize_resume_task")
    def test_optimize_accepts_valid_request(self, mock_task):
        mock_task.return_value = MagicMock(id="huey-task-id")
        pdf_content = b"%PDF-1.4 fake pdf content"
        uploaded = SimpleUploadedFile(
            "resume.pdf", pdf_content, content_type="application/pdf"
        )
        response = self.client.post(
            "/api/resume/optimize",
            data={
                "job_description": "Test job",
                "llm_provider": "OpenAI",
                "api_key": "test-key",
                "file": uploaded,
            },
            format="multipart",
        )
        self.assertEqual(
            response.status_code,
            200,
            msg=f"Expected 200, got {response.status_code}: {getattr(response, 'content', b'')[:500]}",
        )
        data = response.json()
        self.assertIn("resume_id", data)
        mock_task.assert_called_once()

    def test_ollama_cloud_is_supported_provider(self):
        self.assertIn("Ollama Cloud", LLM_PROVIDERS)

    def test_openrouter_is_supported_provider(self):
        self.assertIn("OpenRouter", LLM_PROVIDERS)

    def test_optimize_rejects_invalid_provider(self):
        uploaded = SimpleUploadedFile(
            "x.pdf", b"%PDF", content_type="application/pdf"
        )
        response = self.client.post(
            "/api/resume/optimize",
            data={
                "job_description": "Test",
                "llm_provider": "InvalidProvider",
                "api_key": "k",
                "file": uploaded,
            },
            format="multipart",
        )
        # 400 = business validation; 422 = request/schema validation
        self.assertIn(
            response.status_code,
            (400, 422),
            msg=f"Expected 400 or 422, got {response.status_code}: {getattr(response, 'content', b'')[:500]}",
        )

    @patch("resume_app.api.optimize_resume_task")
    def test_optimize_rejects_invalid_workflow_steps(self, mock_task):
        uploaded = SimpleUploadedFile(
            "x.pdf", b"%PDF", content_type="application/pdf"
        )
        response = self.client.post(
            "/api/resume/optimize",
            data={
                "job_description": "Test",
                "llm_provider": "OpenAI",
                "api_key": "k",
                "workflow_steps": '["writer", "invalid_step"]',
                "file": uploaded,
            },
            format="multipart",
        )
        # 400 = business validation; 422 = request/schema validation
        self.assertIn(
            response.status_code,
            (400, 422),
            msg=f"Expected 400 or 422, got {response.status_code}: {getattr(response, 'content', b'')[:500]}",
        )
        if response.status_code == 200:
            mock_task.assert_not_called()


class TaskTestCase(TestCase):
    @override_settings(HUEY_IMMEDIATE=True)
    @patch("resume_app.tasks.build_optimizer_graph_prompt_state")
    @patch("resume_app.tasks.build_optimizer_context_state")
    @patch("resume_app.tasks.parse_pdf")
    @patch("resume_app.tasks.create_workflow")
    def test_optimize_resume_task_updates_status_on_success(
        self, mock_create_workflow, mock_parse_pdf, mock_ctx, mock_prompt_state
    ):
        from .tasks import optimize_resume_task
        from .models import AgentLog

        user = create_user("opttask")
        mock_prompt_state.return_value = {
            "writer_prompt_template": "",
            "writer_prompt_system": "",
            "writer_prompt_user": "",
            "writer_prompt_legacy": "",
            "ats_judge_prompt_template": "",
            "ats_judge_prompt_system": "",
            "ats_judge_prompt_user": "",
            "ats_judge_prompt_legacy": "",
            "recruiter_judge_prompt_template": "",
            "recruiter_judge_prompt_system": "",
            "recruiter_judge_prompt_user": "",
            "recruiter_judge_prompt_legacy": "",
        }
        mock_ctx.return_value = {
            "job_description": "JD",
            "writer_job_description": "JD",
            "resume_text": "Resume text",
            "source_resume_text": "Resume text",
            "optimization_notes": "(none)",
            "pipeline_skills_json": "(none)",
            "job_highlights": "(none)",
            "retrieval_context": "(none)",
            "optimizer_context_budget": {"writer_jd_chars": 2},
        }
        mock_parse_pdf.return_value = "Resume text"
        graph = MagicMock()
        graph.stream.return_value = [
            {"writer": {"optimized_resume": "Optimized", "iteration_count": 1}},
            {"ats_judge": {"ats_score": 80, "feedback": ["ATS: good"]}},
            {"recruiter_judge": {"recruiter_score": 85, "feedback": ["Rec: good"]}},
        ]
        mock_create_workflow.return_value = graph

        resume = UserResume.objects.create(owner=user, file="test.pdf")
        jd = JobDescription.objects.create(content="JD")
        optimized = OptimizedResume.objects.create(
            owner=user,
            original_resume=resume,
            job_description=jd,
            status=OptimizedResume.STATUS_QUEUED,
        )
        optimize_resume_task.call_local(user.id, optimized.id, jd.id, "OpenAI", "key")

        optimized.refresh_from_db()
        self.assertEqual(optimized.status, OptimizedResume.STATUS_COMPLETED)
        self.assertEqual(optimized.optimized_content, "Optimized")
        self.assertEqual(optimized.ats_score, 80)
        self.assertEqual(optimized.recruiter_score, 85)
        self.assertEqual(optimized.optimizer_context_snapshot, {"writer_jd_chars": 2})
        self.assertGreater(AgentLog.objects.filter(optimized_resume=optimized).count(), 0)


class WriterNodePromptTestCase(TestCase):
    """Writer must pass optimized_resume into the template so multi-step workflows revise the real draft."""

    def test_writer_node_includes_optimized_resume_in_prompt(self):
        from resume_app.agents import writer_node

        captured = {}

        def fake_llm_invoke(llm, messages, max_attempts=3, config=None, structured_schema=None, job_cache_key=None, **kwargs):
            captured["prompt"] = "\n".join(getattr(m, "content", str(m)) for m in messages)
            r = MagicMock()
            r.content = "NEW RESUME OUT"
            r.usage_metadata = None
            return r

        template = (
            "DRAFT:\n{optimized_resume}\nORIG:\n{resume_text}\nJD:\n{job_description}\nFB:\n{feedback}"
        )
        state = {
            "resume_text": "ORIGINAL BODY",
            "source_resume_text": "ORIGINAL BODY",
            "job_description": "JD HERE",
            "optimized_resume": "PREVIOUS DRAFT LINE",
            "feedback": ["ATS: fix keywords"],
            "iteration_count": 0,
            "llm": MagicMock(),
            "writer_prompt_template": template,
            "writer_prompt_system": "",
            "writer_prompt_user": "",
            "writer_prompt_legacy": "",
            "debug": False,
        }

        with patch("resume_app.agents._llm_invoke_with_retry", side_effect=fake_llm_invoke):
            out = writer_node(state)

        self.assertIn("PREVIOUS DRAFT LINE", captured.get("prompt", ""))
        self.assertEqual(out.get("optimized_resume"), "NEW RESUME OUT")
        self.assertEqual(out.get("resume_text"), "NEW RESUME OUT")

    def test_writer_node_puts_latest_draft_in_resume_text_slot_not_pdf(self):
        from resume_app.agents import writer_node

        captured = {}

        def fake_llm_invoke(llm, messages, max_attempts=3, config=None, structured_schema=None, job_cache_key=None, **kwargs):
            captured["prompt"] = "\n".join(getattr(m, "content", str(m)) for m in messages)
            r = MagicMock()
            r.content = "OUT"
            r.usage_metadata = None
            return r

        template = "SRC:\n{source_resume_text}\nDOC:\n{resume_text}\n"
        state = {
            "resume_text": "RAW_PDF_TEXT",
            "source_resume_text": "RAW_PDF_TEXT",
            "job_description": "JD",
            "optimized_resume": "TAILORED_V1",
            "feedback": [],
            "iteration_count": 0,
            "llm": MagicMock(),
            "writer_prompt_template": template,
            "writer_prompt_system": "",
            "writer_prompt_user": "",
            "writer_prompt_legacy": "",
            "debug": False,
        }

        with patch("resume_app.agents._llm_invoke_with_retry", side_effect=fake_llm_invoke):
            writer_node(state)

        p = captured.get("prompt", "")
        self.assertIn("RAW_PDF_TEXT", p)
        self.assertIn("TAILORED_V1", p)
        self.assertGreater(p.index("TAILORED_V1"), p.index("RAW_PDF_TEXT"))
        self.assertIn("DOC:\nTAILORED_V1", p)

    def test_writer_node_omits_duplicate_source_on_first_pass(self):
        from resume_app.agents import writer_node
        from resume_app.optimizer_budget import omitted_duplicate_field_note

        captured = {}

        def fake_llm_invoke(llm, messages, **kwargs):
            captured["prompt"] = "\n".join(getattr(m, "content", str(m)) for m in messages)
            r = MagicMock()
            r.content = "OUT"
            r.usage_metadata = None
            return r

        template = "SRC:\n{source_resume_text}\nDOC:\n{resume_text}\n"
        state = {
            "resume_text": "SAME BODY",
            "source_resume_text": "SAME BODY",
            "job_description": "JD",
            "writer_job_description": "JD",
            "optimized_resume": "",
            "feedback": [],
            "iteration_count": 0,
            "llm": MagicMock(),
            "writer_prompt_template": template,
            "writer_prompt_system": "",
            "writer_prompt_user": "",
            "writer_prompt_legacy": "",
            "debug": False,
        }

        with patch("resume_app.agents._llm_invoke_with_retry", side_effect=fake_llm_invoke):
            writer_node(state)

        note = omitted_duplicate_field_note()
        self.assertIn(note, captured.get("prompt", ""))
        self.assertNotIn("SRC:\nSAME BODY", captured.get("prompt", ""))


class OptimizerAgentThoughtDebugTestCase(TestCase):
    def test_format_agent_log_thought_three_sections(self):
        from .agents import format_agent_log_thought

        text = format_agent_log_thought(
            {
                "debug_messages": [
                    {"role": "system", "content": "SYS"},
                    {"role": "user", "content": "USR"},
                ],
                "input_tokens": 11,
                "output_tokens": 22,
                "raw_llm_response": "RAW",
            }
        )
        self.assertIn("--- System Prompt ---", text)
        self.assertIn("SYS", text)
        self.assertIn("--- User Prompt ---", text)
        self.assertIn("USR", text)
        self.assertIn("--- Raw Output ---", text)
        self.assertIn("RAW", text)
        self.assertIn("input=11", text)

    def test_can_view_optimizer_llm_debug_while_hijacking(self):
        """Staff secret-sauce debug uses the original hijacker, not the impersonated user."""
        from django.test import RequestFactory

        from .agents import can_view_optimizer_llm_debug
        from .tenancy import get_real_user

        admin = create_user("hijack_admin")
        admin.is_staff = True
        admin.save(update_fields=["is_staff"])
        target = create_user("hijack_target")
        self.assertFalse(target.is_staff)

        rf = RequestFactory()
        req = rf.get("/")
        req.user = target
        req.session = {"hijack_history": [str(admin.pk)]}

        self.assertEqual(get_real_user(req).pk, admin.pk)
        self.assertTrue(can_view_optimizer_llm_debug(req))

        req_plain = rf.get("/")
        req_plain.user = target
        req_plain.session = {}
        self.assertFalse(can_view_optimizer_llm_debug(req_plain))

    def test_redact_strips_prompt_keys(self):
        from .agents import redact_agent_log_thought

        redacted = redact_agent_log_thought(
            {
                "debug_messages": [{"role": "system", "content": "secret"}],
                "raw_llm_response": "secret",
                "ats_score": 90,
                "input_tokens": 5,
            },
            include_prompt_debug=False,
        )
        self.assertNotIn("debug_messages", redacted)
        self.assertNotIn("raw_llm_response", redacted)
        self.assertEqual(redacted.get("ats_score"), 90)
        self.assertEqual(redacted.get("input_tokens"), 5)

    def test_get_status_data_hides_logs_for_non_staff(self):
        from django.test import RequestFactory

        from .api import get_status_data
        from .models import AgentLog

        user = create_user("thoughts_user")
        resume = UserResume.objects.create(owner=user, file="t.pdf")
        jd = JobDescription.objects.create(content="JD")
        optimized = OptimizedResume.objects.create(
            owner=user, original_resume=resume, job_description=jd, status="completed"
        )
        AgentLog.objects.create(
            optimized_resume=optimized,
            step_name="recruiter_judge",
            thought={
                "debug_messages": [{"role": "system", "content": "secret sauce"}],
                "input_tokens": 9,
                "output_tokens": 3,
                "raw_llm_response": "table",
            },
        )
        rf = RequestFactory()
        req = rf.get("/")
        req.user = user
        data = get_status_data(optimized.id, user, request=req)
        self.assertFalse(data["show_agent_thoughts"])
        self.assertEqual(data["logs"], [])

        user.is_staff = True
        user.save(update_fields=["is_staff"])
        req.user = user
        data_staff = get_status_data(optimized.id, user, request=req)
        self.assertTrue(data_staff["show_agent_thoughts"])
        self.assertEqual(len(data_staff["logs"]), 1)
        self.assertIn("secret sauce", data_staff["logs"][0]["thought_text"])
        self.assertEqual(data_staff["logs"][0]["step_in"], 9)

    def test_side_channel_pop_into_create_agent_log(self):
        from .agents import clear_node_llm_debug, push_node_llm_debug
        from .models import AgentLog
        from .tasks import _create_agent_log

        user = create_user("sidechan_user")
        resume = UserResume.objects.create(owner=user, file="t.pdf")
        jd = JobDescription.objects.create(content="JD")
        optimized = OptimizedResume.objects.create(
            owner=user, original_resume=resume, job_description=jd
        )
        clear_node_llm_debug(str(optimized.id))
        push_node_llm_debug(
            str(optimized.id),
            {
                "debug_messages": [{"role": "user", "content": "from-side-channel"}],
                "input_tokens": 42,
                "output_tokens": 7,
                "raw_llm_response": "out",
            },
        )
        _create_agent_log(optimized, ["writer"], "step_0", {"step_0": {"ats_score": 1}})
        log = AgentLog.objects.get(optimized_resume=optimized)
        self.assertEqual(log.step_name, "writer")
        self.assertEqual(log.thought.get("input_tokens"), 42)
        self.assertEqual(log.thought["debug_messages"][0]["content"], "from-side-channel")


class OptimizerBudgetTestCase(TestCase):
    def test_should_include_source_resume_when_different(self):
        from resume_app.optimizer_budget import should_include_source_resume

        self.assertTrue(should_include_source_resume("alpha", "beta"))

    def test_should_not_include_source_resume_when_same(self):
        from resume_app.optimizer_budget import should_include_source_resume

        self.assertFalse(should_include_source_resume("same text", "same   text"))

    def test_should_not_include_full_jd_when_role_slice_covers_posting(self):
        from resume_app.optimizer_budget import should_include_full_job_description

        full = "Role requirements " * 20
        role = full.strip()
        self.assertFalse(should_include_full_job_description(role, full))

    def test_should_include_full_jd_when_materially_longer(self):
        from resume_app.optimizer_budget import should_include_full_job_description

        role = "Short role slice"
        full = role + " " + ("extra posting detail " * 50)
        self.assertTrue(should_include_full_job_description(role, full))

    @override_settings(
        OPTIMIZER_JUDGE_RESUME_MAX_CHARS=100,
        OPTIMIZER_JUDGE_JD_MAX_CHARS=50,
    )
    def test_truncate_judge_inputs(self):
        from resume_app.optimizer_budget import (
            truncate_judge_job_description,
            truncate_judge_resume,
        )

        self.assertEqual(len(truncate_judge_resume("x" * 200)), 100)
        self.assertEqual(len(truncate_judge_job_description("y" * 200)), 50)

    @patch("resume_app.embeddings.extract_role_description", return_value="ROLE SLICE")
    def test_build_context_state_uses_role_focused_jd(self, _mock_extract):
        from resume_app.optimizer_budget import build_optimizer_context_state_raw

        ctx = build_optimizer_context_state_raw("resume", "full jd text", job_title="Engineer")
        self.assertEqual(ctx["writer_job_description"], "ROLE SLICE")
        self.assertEqual(ctx["judge_job_description"], "ROLE SLICE")
        self.assertEqual(ctx["job_description"], "full jd text")


class JudgeNodeRoutingTestCase(TestCase):
    def test_judge_node_prefers_remote_by_default(self):
        from resume_app.agents import ats_judge_node
        from resume_app.parsers import AtsJudgeResult

        captured = {}

        def fake_unstructured(*args, **kwargs):
            captured["prefer_local"] = kwargs.get("prefer_local")
            data = AtsJudgeResult(ats_match_score=80, strategic_feedback="ok")
            return data, {"ats_match_score": 80}, MagicMock(content='{"ats_match_score":80}'), {"path": "unstructured"}

        state = {
            "llm": MagicMock(),
            "optimized_resume": "draft",
            "resume_text": "draft",
            "judge_job_description": "jd",
            "job_description": "jd",
            "ats_judge_prompt_template": "RES:{optimized_resume}\nJD:{job_description}",
            "ats_judge_prompt_system": "",
            "ats_judge_prompt_user": "",
            "ats_judge_prompt_legacy": "",
            "debug": False,
        }

        with patch("resume_app.agents._unstructured_judge_invoke", side_effect=fake_unstructured):
            out = ats_judge_node(state)

        self.assertFalse(captured.get("prefer_local"))
        self.assertEqual(out.get("ats_score"), 80)

    @override_settings(OPTIMIZER_JUDGES_PREFER_LOCAL=True)
    def test_judge_node_can_opt_into_local_preference(self):
        from resume_app.agents import ats_judge_node
        from resume_app.parsers import AtsJudgeResult

        captured = {}

        def fake_unstructured(*args, **kwargs):
            captured["prefer_local"] = kwargs.get("prefer_local")
            data = AtsJudgeResult(ats_match_score=80, strategic_feedback="ok")
            return data, {"ats_match_score": 80}, MagicMock(content='{"ats_match_score":80}'), {"path": "unstructured"}

        state = {
            "llm": MagicMock(),
            "optimized_resume": "draft",
            "resume_text": "draft",
            "judge_job_description": "jd",
            "job_description": "jd",
            "ats_judge_prompt_template": "RES:{optimized_resume}\nJD:{job_description}",
            "ats_judge_prompt_system": "",
            "ats_judge_prompt_user": "",
            "ats_judge_prompt_legacy": "",
            "debug": False,
        }

        with patch("resume_app.agents._unstructured_judge_invoke", side_effect=fake_unstructured):
            out = ats_judge_node(state)

        self.assertTrue(captured.get("prefer_local"))
        self.assertEqual(out.get("ats_score"), 80)

    @override_settings(
        OPTIMIZER_JUDGES_PREFER_LOCAL=True,
        OPTIMIZER_JUDGE_RESUME_MAX_CHARS=10,
        OPTIMIZER_JUDGE_JD_MAX_CHARS=5,
    )
    def test_judge_node_caps_resume_and_jd_in_prompt(self):
        from resume_app.agents import recruiter_judge_node
        from resume_app.parsers import ScoreFeedback

        captured = {}

        def fake_unstructured(llm, messages, **kwargs):
            captured["prompt"] = "\n".join(getattr(m, "content", str(m)) for m in messages)
            data = ScoreFeedback(score=70, feedback="fine")
            return data, {"score": 70}, MagicMock(content='{"score":70}'), {"path": "unstructured"}

        state = {
            "llm": MagicMock(),
            "optimized_resume": "012345678901234567890",
            "resume_text": "012345678901234567890",
            "judge_job_description": "abcdefghijklmnop",
            "job_description": "abcdefghijklmnop",
            "recruiter_judge_prompt_template": "RES:{optimized_resume}\nJD:{job_description}",
            "recruiter_judge_prompt_system": "",
            "recruiter_judge_prompt_user": "",
            "recruiter_judge_prompt_legacy": "",
            "debug": False,
        }

        with patch("resume_app.agents._unstructured_judge_invoke", side_effect=fake_unstructured):
            recruiter_judge_node(state)

        prompt = captured.get("prompt", "")
        self.assertIn("0123456789", prompt)
        self.assertNotIn("012345678901234567890", prompt)
        self.assertIn("abcde", prompt)
        self.assertNotIn("abcdefghijklmnop", prompt)

    def test_writer_node_keeps_strong_model_routing(self):
        from resume_app.agents import writer_node

        captured = {}

        def fake_llm_invoke(llm, messages, **kwargs):
            captured["prefer_local"] = kwargs.get("prefer_local")
            r = MagicMock()
            r.content = "OUT"
            r.usage_metadata = None
            return r

        state = {
            "resume_text": "BODY",
            "source_resume_text": "BODY",
            "job_description": "JD",
            "optimized_resume": "",
            "feedback": [],
            "iteration_count": 0,
            "llm": MagicMock(),
            "writer_prompt_template": "X",
            "writer_prompt_system": "",
            "writer_prompt_user": "",
            "writer_prompt_legacy": "",
            "debug": False,
        }

        with patch("resume_app.agents._llm_invoke_with_retry", side_effect=fake_llm_invoke):
            writer_node(state)

        self.assertFalse(captured.get("prefer_local"))


class ResumeKeywordMinerTestCase(TestCase):
    def test_mine_keywords_empty_jobs(self):
        self.assertEqual(mine_keywords_from_jobs([]), [])

    def test_mine_keywords_finds_repeated_terms(self):
        jd_common = (
            "**Responsibilities**\n"
            "Work with Python and Kubernetes on distributed systems. "
            "Build APIs with PostgreSQL."
        )
        jobs = [
            ("Senior Backend Engineer", jd_common),
            ("Staff Software Engineer", jd_common.replace("PostgreSQL", "Postgres")),
        ]
        out = mine_keywords_from_jobs(jobs)
        phrases = [x["phrase"] for x in out]
        # Check for existence of terms within phrases
        self.assertTrue(any("python" in p for p in phrases))
        self.assertTrue(any("kubernetes" in p for p in phrases))
        self.assertTrue(any("distributed systems" in p for p in phrases))
        for row in out:
            self.assertGreaterEqual(row["doc_count"], 1)
            self.assertLessEqual(row["job_fraction"], 1.0)

    def test_mine_keywords_prefers_phrases_and_drops_filler(self):
        jd = (
            "**Requirements**\n"
            "Core skills: machine learning and data pipelines. "
            "Expertise in distributed systems."
        )
        jobs = [("Engineer", jd), ("Engineer", jd)]
        out = mine_keywords_from_jobs(jobs)
        phrases = [x["phrase"] for x in out]
        self.assertNotIn("across", phrases)
        self.assertTrue(any("machine learning" in p for p in phrases))
        if phrases:
            self.assertGreaterEqual(len(phrases[0].split()), 2)

    def test_mine_keywords_excludes_job_titles_and_subsumes_redundant_ngrams(self):
        jd = (
            "Principal Software Engineers and Distinguished Engineers work cross-functionally. "
            "We use software defined networking sdn and kubernetes. "
            "Long term we invest in machine learning and distributed systems."
        )
        jobs = [("Distinguished Engineer", jd), ("Principal Engineer", jd)]
        out = mine_keywords_from_jobs(jobs)
        phrases = [x["phrase"] for x in out]
        for bad in (
            "principal software",
            "distinguished software",
            "distinguished engineers",
            "principal engineer",
            "software engineer",
            "long term",
        ):
            self.assertNotIn(bad, phrases)
        self.assertTrue(any("software defined networking" in p for p in phrases))
        self.assertNotIn("software defined", phrases)
        self.assertTrue(any("machine learning" in p for p in phrases))


@patch(
    "resume_app.jobs_api._user_provider_api_key_available",
    return_value=True,
)
@patch(
    "resume_app.pipeline_llm_skill_extract.resolve_provider_api_key",
    return_value="sk-test-placeholder",
)
class PipelineResumeSummaryAPITestCase(TenantTestCase):
    def test_pipeline_resume_summary_start_unknown_track(self, _mock_key, _mock_api):
        response = self.client.post(
            "/api/resume/jobs/pipeline-resume-summary/start",
            data=json.dumps(
                {
                    "track": "not-a-real-track-slug-xyz",
                    "llm_provider": "OpenAI",
                    "model": "gpt-4o-mini",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_pipeline_resume_summary_start_empty_pipeline(self, _mock_key, _mock_api):
        response = self.client.post(
            "/api/resume/jobs/pipeline-resume-summary/start",
            data=json.dumps({"track": "ic", "llm_provider": "OpenAI", "model": "gpt-4o-mini"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    @patch("resume_app.tasks.pipeline_resume_llm_extract_task")
    def test_pipeline_resume_summary_start_enqueues(self, mock_task, _mock_key, _mock_api):
        jd = "Requirements: Python and Kubernetes."
        job = JobListing.objects.create(
            source="test",
            external_id="sum-1",
            title="Platform Engineer",
            company_name="Co",
            description=jd,
        )
        PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
        )
        response = self.client.post(
            "/api/resume/jobs/pipeline-resume-summary/start",
            data=json.dumps(
                {"track": "ic", "llm_provider": "OpenAI", "model": "gpt-4o-mini", "max_jobs": 1}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_jobs"], 1)
        self.assertTrue(data.get("run_id"))
        self.assertEqual(data["track"], "ic")
        mock_task.assert_called_once()

    def test_pipeline_resume_summary_status_not_found(self, _mock_key, _mock_api):
        response = self.client.get(
            "/api/resume/jobs/pipeline-resume-summary/status",
            {"track": "ic", "run_id": "00000000-0000-0000-0000-000000000000"},
        )
        self.assertEqual(response.status_code, 404)

    @patch("resume_app.tasks.pipeline_resume_llm_extract_task")
    def test_pipeline_resume_summary_stop_idempotent(self, mock_task, _mock_key, _mock_api):
        jd = "Requirements: Python."
        job = JobListing.objects.create(
            source="test",
            external_id="sum-stop",
            title="Engineer",
            company_name="Co",
            description=jd,
        )
        PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
        )
        start = self.client.post(
            "/api/resume/jobs/pipeline-resume-summary/start",
            data=json.dumps({"track": "ic", "llm_provider": "OpenAI", "model": "gpt-4o-mini"}),
            content_type="application/json",
        )
        self.assertEqual(start.status_code, 200)
        run_id = start.json()["run_id"]
        stop = self.client.post(
            "/api/resume/jobs/pipeline-resume-summary/stop",
            data=json.dumps({"track": "ic", "run_id": run_id}),
            content_type="application/json",
        )
        self.assertEqual(stop.status_code, 200)
        self.assertTrue(stop.json().get("ok"))
        from django.conf import settings
        from pathlib import Path

        flag = (
            Path(settings.MEDIA_ROOT)
            / "pipeline_llm_extract"
            / "ic"
            / run_id
            / "stop_signal.flag"
        )
        self.assertTrue(flag.is_file())


class PipelineStageViewTestCase(TenantTestCase):
    def setUp(self):
        super().setUp()
        from .experience import set_experience_mode
        from .models import UserExperienceSettings

        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        set_experience_mode(self.user, UserExperienceSettings.ExperienceMode.POWER)
        Track.objects.get_or_create(
            owner=self.user,
            slug="ic",
            defaults={"label": "IC", "is_default": False},
        )

    def _create_job_and_entry(self, track="ic"):
        job = JobListing.objects.create(
            source="test",
            external_id="ext-1",
            title="Engineer",
            company_name="ACME",
        )
        pe = PipelineEntry.objects.create(owner=self.user, job_listing=job, track=track)
        return job, pe

    def test_pipeline_save_moves_to_vetting(self):
        job, _pe = self._create_job_and_entry()
        response = self.client.post(
            "/jobs/pipeline/?track=ic",
            data={
                "action": "save",
                "job_id": job.id,
                "track": "ic",
                "next": "/jobs/pipeline/?track=ic",
            },
        )
        self.assertIn(response.status_code, (302, 303))
        pe = PipelineEntry.objects.get(owner=self.user, job_listing=job, track="ic")
        self.assertEqual(pe.stage, PipelineEntry.Stage.VETTING)

    def test_vetting_save_moves_to_applying(self):
        job, pe = self._create_job_and_entry()
        pe.move_to_vetting()
        response = self.client.post(
            "/jobs/vetting/?track=ic",
            data={
                "action": "save",
                "job_id": job.id,
                "track": "ic",
                "next": "/jobs/vetting/?track=ic",
            },
        )
        self.assertIn(response.status_code, (302, 303))
        pe.refresh_from_db()
        self.assertEqual(pe.stage, PipelineEntry.Stage.APPLYING)

    def test_applying_save_moves_to_done(self):
        job, pe = self._create_job_and_entry()
        pe.move_to_applying()
        response = self.client.post(
            "/jobs/applying/?track=ic",
            data={
                "action": "save",
                "job_id": job.id,
                "track": "ic",
                "next": "/jobs/applying/?track=ic",
            },
        )
        self.assertIn(response.status_code, (302, 303))
        pe.refresh_from_db()
        self.assertEqual(pe.stage, PipelineEntry.Stage.DONE)

    def test_applying_board_shows_open_optimizer_link(self):
        job, pe = self._create_job_and_entry()
        job.description = "x" * 60
        job.save(update_fields=["description"])
        pe.move_to_applying()
        UserResume.objects.create(owner=self.user, track="ic", file="r.pdf", is_library=True)
        response = self.client.get("/jobs/applying/?track=ic")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/resume/optimizer/")
        self.assertContains(response, f"job_id={job.id}")
        self.assertContains(response, "Open optimizer")


class FindJobsSavePipelineTestCase(TenantTestCase):
    """Find-jobs Save creates Review pipeline entries; Unsave removes them."""

    username = "findjobs_save"

    def _create_job(self, external_id: str = "save-1") -> JobListing:
        return JobListing.objects.create(
            source="test",
            external_id=external_id,
            title="Engineer",
            company_name="ACME",
        )

    @patch("resume_app.jobs_api._enqueue_vetting_match_for_entry")
    def test_save_creates_review_pipeline_entry(self, _mock_enqueue):
        job = self._create_job()
        track = Track.get_default_slug(self.user)
        response = self.client.post(f"/api/resume/jobs/{job.id}/save?track={track}")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("success"))
        pe = PipelineEntry.objects.get(owner=self.user, job_listing=job, track=track)
        self.assertEqual(pe.stage, PipelineEntry.Stage.VETTING)
        self.assertIsNone(pe.removed_at)
        _mock_enqueue.assert_called_once()

    @patch("resume_app.jobs_api._enqueue_vetting_match_for_entry")
    def test_save_leaves_existing_pipeline_stage(self, _mock_enqueue):
        job = self._create_job("save-existing")
        track = Track.get_default_slug(self.user)
        PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track=track,
            stage=PipelineEntry.Stage.PIPELINE,
        )
        response = self.client.post(f"/api/resume/jobs/{job.id}/save?track={track}")
        self.assertEqual(response.status_code, 200)
        pe = PipelineEntry.objects.get(owner=self.user, job_listing=job, track=track)
        self.assertEqual(pe.stage, PipelineEntry.Stage.PIPELINE)
        _mock_enqueue.assert_not_called()

    @patch("resume_app.jobs_api._enqueue_vetting_match_for_entry")
    def test_save_restores_soft_deleted_to_review(self, mock_enqueue):
        job = self._create_job("save-restored")
        track = Track.get_default_slug(self.user)
        pe = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track=track,
            stage=PipelineEntry.Stage.PIPELINE,
        )
        pe.mark_deleted(save=True)
        response = self.client.post(f"/api/resume/jobs/{job.id}/save?track={track}")
        self.assertEqual(response.status_code, 200)
        pe.refresh_from_db()
        self.assertIsNone(pe.removed_at)
        self.assertEqual(pe.stage, PipelineEntry.Stage.VETTING)
        mock_enqueue.assert_called_once()

    @patch("resume_app.jobs_api._enqueue_vetting_match_for_entry")
    def test_unsave_soft_deletes_pipeline_entry(self, _mock_enqueue):
        job = self._create_job("unsave-1")
        track = Track.get_default_slug(self.user)
        self.client.post(f"/api/resume/jobs/{job.id}/save?track={track}")
        pe = PipelineEntry.objects.get(owner=self.user, job_listing=job, track=track)
        self.assertEqual(pe.stage, PipelineEntry.Stage.VETTING)

        response = self.client.post(f"/api/resume/jobs/{job.id}/unsave?track={track}")
        self.assertEqual(response.status_code, 200)
        pe.refresh_from_db()
        self.assertIsNotNone(pe.removed_at)
        self.assertEqual(pe.stage, PipelineEntry.Stage.DELETED)

    @patch("resume_app.jobs_api._enqueue_vetting_match_for_entry")
    def test_saved_job_appears_on_review_board(self, _mock_enqueue):
        from resume_app.saved_searches import create_or_update_saved_search

        create_or_update_saved_search(self.user, name="FinCrimes", search_term="AML")
        job = self._create_job("save-board")
        # Saved searches create a profile slug from the name
        from resume_app.models import SearchProfile

        sp = SearchProfile.objects.for_user(self.user).filter(name="FinCrimes").first()
        track = sp.slug if sp else Track.get_default_slug(self.user)
        self.client.post(f"/api/resume/jobs/{job.id}/save?track={track}")
        response = self.client.get(f"/jobs/vetting/?track={track}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Engineer")
        self.assertContains(response, "ACME")


class PipelineResumeEnqueueTestCase(TestCase):
    def setUp(self):
        self.user = create_user("enqueue")

    def test_enqueue_skips_when_queued_exists(self):
        job = JobListing.objects.create(
            source="test",
            external_id="eq-1",
            title="Engineer",
            company_name="ACME",
            description="d" * 60,
        )
        pe = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="ic",
            stage=PipelineEntry.Stage.APPLYING,
        )
        ur = UserResume.objects.create(owner=self.user, file="r.pdf")
        jd = JobDescription.objects.create(content="c" * 60)
        OptimizedResume.objects.create(
            owner=self.user,
            original_resume=ur,
            job_description=jd,
            pipeline_entry=pe,
            status=OptimizedResume.STATUS_QUEUED,
        )
        result = _enqueue_single_pipeline_resume_optimization(pe.id, force_new=False)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(OptimizedResume.objects.filter(pipeline_entry=pe).count(), 1)

    @patch("resume_app.tasks.optimize_resume_task")
    @patch("resume_app.tasks.decrypt_api_key", return_value="sk-test")
    @patch("resume_app.tasks._resolve_llm_for_pipeline_optimization")
    def test_enqueue_creates_run_with_pipeline_link(
        self, mock_resolve, _mock_decrypt, mock_optimize_task
    ):
        cfg = MagicMock()
        cfg.encrypted_api_key = "enc"
        cfg.default_model = "gpt-4o-mini"
        mock_resolve.return_value = ("OpenAI", cfg)

        job = JobListing.objects.create(
            source="test",
            external_id="eq-2",
            title="Engineer",
            company_name="ACME",
            description="d" * 60,
        )
        pe = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="ic",
            stage=PipelineEntry.Stage.APPLYING,
        )
        UserResume.objects.create(owner=self.user, file="r2.pdf", is_library=True)

        result = _enqueue_single_pipeline_resume_optimization(pe.id, force_new=False)
        self.assertEqual(result["status"], "ok")
        self.assertIn("optimized_resume_id", result)
        opt = OptimizedResume.objects.get(id=result["optimized_resume_id"])
        self.assertEqual(opt.pipeline_entry_id, pe.id)
        mock_optimize_task.assert_called_once()
        call_kw = mock_optimize_task.call_args.kwargs
        self.assertTrue(call_kw.get("debug"))


class PipelineAutomationTestCase(TestCase):
    def setUp(self):
        self.user = create_user("pipelineauto")

    def test_pipeline_auto_promotion_moves_to_vetting_and_enqueues_matching(self):
        cfg = AppAutomationSettings.get_for_user(self.user)
        cfg.pipeline_to_vetting_enabled = True
        cfg.pipeline_preference_margin_min = 5
        cfg.save()

        job = JobListing.objects.create(
            source="test",
            external_id="auto-p1",
            title="Engineer",
            company_name="ACME",
        )
        pe = PipelineEntry.objects.create(owner=self.user, job_listing=job, track="ic", stage="")
        JobListingTrackMetrics.objects.create(
            owner=self.user,
            job_listing=job,
            track="ic",
            preference_margin=10,
        )

        with patch("resume_app.tasks.evaluate_vetting_matching_task") as mock_ev:
            n = apply_pipeline_auto_promotions(self.user)
        self.assertEqual(n, 1)
        pe.refresh_from_db()
        self.assertEqual(pe.stage, PipelineEntry.Stage.VETTING)
        mock_ev.assert_called_once()
        args, kwargs = mock_ev.call_args
        self.assertEqual(args[0], self.user.id)
        self.assertEqual(args[1], [pe.id])
        self.assertIsNone(kwargs.get("matching_prompt"))

    def test_pipeline_auto_promotion_skips_when_disabled(self):
        cfg = AppAutomationSettings.get_for_user(self.user)
        cfg.pipeline_to_vetting_enabled = False
        cfg.save()

        job = JobListing.objects.create(
            source="test",
            external_id="auto-p2",
            title="Engineer",
            company_name="ACME",
        )
        pe = PipelineEntry.objects.create(owner=self.user, job_listing=job, track="ic", stage="")
        JobListingTrackMetrics.objects.create(
            owner=self.user,
            job_listing=job,
            track="ic",
            preference_margin=99,
        )

        with patch("resume_app.tasks.evaluate_vetting_matching_task") as mock_ev:
            n = apply_pipeline_auto_promotions(self.user)
        self.assertEqual(n, 0)
        pe.refresh_from_db()
        self.assertEqual(pe.stage, "")
        mock_ev.assert_not_called()

    @override_settings(HUEY_IMMEDIATE=True)
    def test_vetting_matching_task_uses_explicit_llm_provider_and_model(self):
        from .tasks import evaluate_vetting_matching_task

        job = JobListing.objects.create(
            source="test",
            external_id="vetting-override",
            title="Engineer",
            company_name="ACME",
            description="A" * 2100,
        )
        pe = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
        )
        UserResume.objects.create(owner=self.user, file="resume.pdf", is_library=True, track="ic")

        mock_llm = MagicMock()
        mock_llm._resume_provider = "Ollama Local"
        mock_llm._resume_model = "mistral"

        with patch("resume_app.tasks.parse_pdf", return_value="resume text"), \
            patch("resume_app.tasks.resolve_prompt_parts", return_value=("sys", "usr", None)), \
            patch("resume_app.tasks.resolve_provider_api_key", return_value="http://localhost:11434"), \
            patch("resume_app.tasks.list_models_for_provider", return_value=["mistral", "starling"]), \
            patch("resume_app.tasks.get_llm", return_value=mock_llm), \
            patch("resume_app.tasks.run_matching", return_value={"interview_probability": 75, "reasoning": "ok"}) as mock_run_matching:
            result = evaluate_vetting_matching_task.call_local(
                self.user.id,
                [pe.id],
                llm_provider="Ollama Local",
                llm_model="mistral",
                matching_prompt="Custom matching prompt",
            )

        self.assertEqual(result["updated"], 1)
        mock_run_matching.assert_called_once()
        passed_llm = mock_run_matching.call_args.args[2]
        self.assertEqual(passed_llm, mock_llm)

    def test_vetting_auto_promotion_moves_to_applying(self):
        cfg = AppAutomationSettings.get_for_user(self.user)
        cfg.vetting_to_applying_enabled = True
        cfg.vetting_interview_probability_min = 50
        cfg.save()

        job = JobListing.objects.create(
            source="test",
            external_id="auto-v1",
            title="Engineer",
            company_name="ACME",
        )
        pe = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
        )
        PipelineEntry.objects.filter(pk=pe.pk).update(vetting_interview_probability=80)
        pe.refresh_from_db()

        with patch("resume_app.tasks.enqueue_applying_resume_optimization_task") as mock_eq:
            n = apply_vetting_to_applying_promotions(self.user)
        self.assertEqual(n, 1)
        pe.refresh_from_db()
        self.assertEqual(pe.stage, PipelineEntry.Stage.APPLYING)
        mock_eq.assert_not_called()

    def test_vetting_auto_promotion_respects_threshold(self):
        cfg = AppAutomationSettings.get_for_user(self.user)
        cfg.vetting_to_applying_enabled = True
        cfg.vetting_interview_probability_min = 90
        cfg.save()

        job = JobListing.objects.create(
            source="test",
            external_id="auto-v2",
            title="Engineer",
            company_name="ACME",
        )
        pe = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
        )
        PipelineEntry.objects.filter(pk=pe.pk).update(vetting_interview_probability=50)
        pe.refresh_from_db()

        n = apply_vetting_to_applying_promotions(self.user)
        self.assertEqual(n, 0)
        pe.refresh_from_db()
        self.assertEqual(pe.stage, PipelineEntry.Stage.VETTING)


class FitPrefScoringTestCase(TestCase):
    """Scheduled search persists Fit/Pref; managers cover SP-only slugs."""

    def setUp(self):
        self.user = create_user("fitpref")

    def _job_payload(self, job, *, focus_percent=72, preference_margin_percent=15):
        from .schemas import JobPayload

        return JobPayload(
            id=job.id,
            title=job.title,
            company_name=job.company_name,
            location="",
            snippet="snippet",
            url="https://example.com/j",
            source="test",
            focus_percent=focus_percent,
            preference_margin_percent=preference_margin_percent,
        )

    def test_persist_metrics_for_search_profile_only_slug(self):
        from resume_app.models import SearchProfile
        from resume_app.job_search_core import (
            persist_preference_metrics_for_jobs,
            pipeline_jobs_to_payloads,
        )
        from resume_app.search_profile_scope import profile_slugs_for_pipeline

        sp = SearchProfile.objects.create(
            owner=self.user,
            name="FinCrimes",
            slug="fincrimes",
            search_term="AML",
        )
        track_slugs = list(Track.objects.for_user(self.user).values_list("slug", flat=True))
        self.assertNotIn("fincrimes", track_slugs)
        self.assertIn("fincrimes", profile_slugs_for_pipeline(self.user))

        job = JobListing.objects.create(
            source="test",
            external_id="sp-only-1",
            title="Analyst",
            company_name="Bank",
        )
        PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="fincrimes",
            stage=PipelineEntry.Stage.PIPELINE,
            search_profile=sp,
        )
        payload = self._job_payload(job)

        written = persist_preference_metrics_for_jobs(
            user=self.user,
            track="fincrimes",
            job_listings=[job],
            payloads=[payload],
        )
        self.assertEqual(written, 1)
        metrics = JobListingTrackMetrics.objects.get(
            owner=self.user, job_listing=job, track="fincrimes"
        )
        self.assertEqual(metrics.focus_percent, 72)
        self.assertEqual(metrics.preference_margin, 15)
        self.assertEqual(metrics.search_profile_id, sp.id)

        board_payloads = pipeline_jobs_to_payloads([job], "fincrimes", user=self.user)
        self.assertEqual(board_payloads[0].focus_percent, 72)
        self.assertEqual(board_payloads[0].preference_margin_percent, 15)

    def test_pipeline_manager_visits_search_profile_only_slug(self):
        from resume_app.models import SearchProfile
        from resume_app.tasks import _pipeline_manager_for_user

        SearchProfile.objects.create(
            owner=self.user,
            name="FinCrimes",
            slug="fincrimes",
            search_term="AML",
        )
        job = JobListing.objects.create(
            source="test",
            external_id="pm-sp-1",
            title="Analyst",
            company_name="Bank",
        )
        PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="fincrimes",
            stage=PipelineEntry.Stage.PIPELINE,
        )

        with patch(
            "resume_app.tasks.persist_preference_metrics_for_jobs", return_value=1
        ) as mock_persist:
            _pipeline_manager_for_user(self.user)

        mock_persist.assert_called()
        tracks_called = {c.kwargs["track"] for c in mock_persist.call_args_list}
        self.assertIn("fincrimes", tracks_called)

    def test_apply_pipeline_auto_promotion_uses_owner_scoped_metrics(self):
        other = create_user("fitpref-other")
        job = JobListing.objects.create(
            source="test",
            external_id="owner-scope",
            title="Engineer",
            company_name="ACME",
        )
        pe = PipelineEntry.objects.create(
            owner=self.user, job_listing=job, track="ic", stage=""
        )
        JobListingTrackMetrics.objects.create(
            owner=other,
            job_listing=job,
            track="ic",
            preference_margin=99,
        )
        cfg = AppAutomationSettings.get_for_user(self.user)
        cfg.pipeline_to_vetting_enabled = True
        cfg.pipeline_preference_margin_min = 5
        cfg.save()

        with patch("resume_app.tasks.evaluate_vetting_matching_task") as mock_ev:
            n = apply_pipeline_auto_promotions(self.user)
        self.assertEqual(n, 0)
        pe.refresh_from_db()
        self.assertEqual(pe.stage, "")
        mock_ev.assert_not_called()

        JobListingTrackMetrics.objects.create(
            owner=self.user,
            job_listing=job,
            track="ic",
            preference_margin=10,
        )
        with patch("resume_app.tasks.evaluate_vetting_matching_task") as mock_ev:
            n = apply_pipeline_auto_promotions(self.user)
        self.assertEqual(n, 1)
        pe.refresh_from_db()
        self.assertEqual(pe.stage, PipelineEntry.Stage.VETTING)
        mock_ev.assert_called_once()

    def test_run_job_search_task_persists_metrics_from_payloads(self):
        from resume_app.models import JobSearchTask, SearchProfile
        from resume_app.tasks import _run_job_search_task_impl

        sp = SearchProfile.objects.create(
            owner=self.user,
            name="Data",
            slug="dataeng",
            search_term="data",
        )
        task = JobSearchTask.objects.create(
            owner=self.user,
            search_term="data engineer",
            track="dataeng",
            frequency="0 9 * * *",
            saved_search=sp,
        )
        job = JobListing.objects.create(
            source="test",
            external_id="ingest-1",
            title="Data Engineer",
            company_name="Co",
        )
        payload = self._job_payload(job, focus_percent=80, preference_margin_percent=20)

        with patch(
            "resume_app.tasks.run_job_search_core", return_value=(1, 1, [payload], [])
        ), patch(
            "resume_app.job_dedupe.dedupe_pipeline_entries",
            return_value={"entries_removed": 0, "duplicate_groups": 0},
        ), patch(
            "resume_app.tasks.apply_pipeline_auto_promotions"
        ) as mock_promo:
            result = _run_job_search_task_impl(self.user.id, task.id)

        self.assertEqual(result["status"], "success")
        metrics = JobListingTrackMetrics.objects.get(
            owner=self.user, job_listing=job, track="dataeng"
        )
        self.assertEqual(metrics.focus_percent, 80)
        self.assertEqual(metrics.preference_margin, 20)
        mock_promo.assert_called_once_with(self.user)

    def test_pipeline_payloads_include_interview_on_applying_stage(self):
        from resume_app.job_search_core import pipeline_jobs_to_payloads

        job = JobListing.objects.create(
            source="test",
            external_id="interview-applying",
            title="Engineer",
            company_name="ACME",
        )
        PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="ic",
            stage=PipelineEntry.Stage.APPLYING,
            vetting_interview_probability=43,
            vetting_interview_reasoning="Strong AML domain overlap; leadership gap on sanctions tooling.",
        )
        payloads = pipeline_jobs_to_payloads([job], "ic", user=self.user)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0].interview_probability, 43)
        self.assertIn("AML", payloads[0].interview_reasoning)

    def test_interview_status_pending_vs_short_jd(self):
        from resume_app.job_search_core import (
            pipeline_jobs_to_payloads,
            resolve_interview_display_status,
        )

        self.assertEqual(
            resolve_interview_display_status(description="x" * 500, interview_probability=None),
            "short_jd",
        )
        self.assertEqual(
            resolve_interview_display_status(description="x" * 2500, interview_probability=None),
            "pending",
        )
        self.assertIsNone(
            resolve_interview_display_status(description="", interview_probability=42),
        )

        short_job = JobListing.objects.create(
            source="test",
            external_id="short-jd",
            title="Role",
            company_name="Co",
            description="brief",
        )
        long_job = JobListing.objects.create(
            source="test",
            external_id="long-jd",
            title="Role",
            company_name="Co",
            description="x" * 2500,
        )
        PipelineEntry.objects.create(
            owner=self.user,
            job_listing=short_job,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
        )
        PipelineEntry.objects.create(
            owner=self.user,
            job_listing=long_job,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
        )
        by_id = {
            p.id: p
            for p in pipeline_jobs_to_payloads([short_job, long_job], "ic", user=self.user)
        }
        self.assertEqual(by_id[short_job.id].interview_status, "short_jd")
        self.assertEqual(by_id[long_job.id].interview_status, "pending")

        dice_job = JobListing.objects.create(
            source="dice",
            external_id="dice-short",
            title="Role",
            company_name="Co",
            description="brief dice snippet",
            url="https://www.dice.com/job-detail/guid-123",
        )
        PipelineEntry.objects.create(
            owner=self.user,
            job_listing=dice_job,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
        )
        dice_payload = pipeline_jobs_to_payloads([dice_job], "ic", user=self.user)[0]
        self.assertEqual(dice_payload.interview_status, "pending")


class JobDedupeTestCase(TestCase):
    def setUp(self):
        self.user = create_user("dedupe")
        Track.ensure_baseline(self.user)

    def test_fingerprint_matches_same_description_different_location(self):
        from .job_dedupe import job_listing_fingerprint

        desc = "Same body " * 20
        a = JobListing.objects.create(
            source="t",
            external_id="fp-a",
            title="Role",
            company_name="Co",
            description=desc,
            location="Seattle, WA",
        )
        b = JobListing.objects.create(
            source="t",
            external_id="fp-b",
            title="Role",
            company_name="Co",
            description=desc,
            location="Bellevue, WA",
        )
        self.assertEqual(job_listing_fingerprint(a), job_listing_fingerprint(b))

    def test_search_dedupe_by_title_company_keeps_first(self):
        from .job_dedupe import dedupe_payloads_by_title_company
        from .schemas import JobPayload

        payloads = [
            JobPayload(
                id=1,
                title="Staff Software Engineer (Backend) - Everand Core",
                company_name="Scribd, Inc.",
                location="Portland, OR, US",
                snippet="a",
                url="https://example.com/1",
                source="indeed",
                focus_percent=72,
            ),
            JobPayload(
                id=2,
                title="Staff Software Engineer (Backend) - Everand Core",
                company_name="Scribd, Inc.",
                location="Jacksonville, FL, US",
                snippet="a",
                url="https://example.com/2",
                source="indeed",
                focus_percent=72,
            ),
            JobPayload(
                id=3,
                title="Other Role",
                company_name="Scribd, Inc.",
                location="Remote",
                snippet="b",
                url="https://example.com/3",
                source="indeed",
                focus_percent=60,
            ),
        ]
        out = dedupe_payloads_by_title_company(payloads)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].id, 1)
        self.assertEqual(out[0].location, "Portland, OR, US")
        self.assertEqual(out[1].id, 3)

    def test_dedupe_keeps_higher_focus_after_penalty(self):
        from .job_dedupe import dedupe_pipeline_entries

        desc = "Shared description for dedupe winner test."
        j1 = JobListing.objects.create(
            source="t",
            external_id="dw-1",
            title="Role",
            company_name="Co",
            description=desc,
            location="A",
        )
        j2 = JobListing.objects.create(
            source="t",
            external_id="dw-2",
            title="Role",
            company_name="Co",
            description=desc,
            location="B",
        )
        JobListingTrackMetrics.objects.create(
            owner=self.user, job_listing=j1, track="ic", focus_after_penalty=50
        )
        JobListingTrackMetrics.objects.create(
            owner=self.user, job_listing=j2, track="ic", focus_after_penalty=90
        )
        e1 = PipelineEntry.objects.create(owner=self.user, job_listing=j1, track="ic", stage="")
        e2 = PipelineEntry.objects.create(owner=self.user, job_listing=j2, track="ic", stage="")
        dedupe_pipeline_entries(user=self.user, track_slug="ic", stage="pipeline", include_done=False)
        e1.refresh_from_db()
        e2.refresh_from_db()
        self.assertIsNotNone(e1.removed_at)
        self.assertIsNone(e2.removed_at)

    def test_dedupe_respects_stage_scope(self):
        from .job_dedupe import dedupe_pipeline_entries

        desc = "Stage scope " * 30
        j1 = JobListing.objects.create(
            source="t",
            external_id="st-1",
            title="R",
            company_name="C",
            description=desc,
        )
        j2 = JobListing.objects.create(
            source="t",
            external_id="st-2",
            title="R",
            company_name="C",
            description=desc,
        )
        PipelineEntry.objects.create(
            owner=self.user,
            job_listing=j1,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
        )
        PipelineEntry.objects.create(
            owner=self.user,
            job_listing=j2,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
        )
        r0 = dedupe_pipeline_entries(user=self.user, track_slug="ic", stage="pipeline", include_done=False)
        self.assertEqual(r0["entries_removed"], 0)
        r1 = dedupe_pipeline_entries(user=self.user, track_slug="ic", stage="vetting", include_done=False)
        self.assertEqual(r1["entries_removed"], 1)


class JdCleanseNodeTestCase(TestCase):
    @patch("resume_app.agents._llm_invoke_with_retry")
    def test_jd_cleanse_overwrites_downstream_jd_fields(self, mock_invoke):
        from types import SimpleNamespace

        from resume_app.agents import jd_cleanse_node, create_workflow_from_steps, VALID_STEP_IDS

        self.assertIn("jd_cleanse", VALID_STEP_IDS)
        app = create_workflow_from_steps(["jd_cleanse", "writer", "ats_judge"])
        self.assertIsNotNone(app)

        mock_invoke.return_value = SimpleNamespace(
            content="Core requirements: Python, Django, AWS. Lead platform delivery."
        )
        user = create_user("jdcleanse")
        noisy = (
            "About us: We love culture.\nBenefits: unlimited PTO.\n"
            "Responsibilities: Build APIs in Python and Django on AWS.\n"
            "EEO: Equal opportunity employer."
        )
        out = jd_cleanse_node(
            {
                "job_description": noisy,
                "job_title": "Staff Engineer",
                "llm": None,
                "user_id": user.id,
                "job_cache_key": "test-jd",
            }
        )
        cleansed = out["job_description"]
        self.assertEqual(out["writer_job_description"], cleansed)
        self.assertEqual(out["judge_job_description"], cleansed)
        self.assertLess(len(cleansed), len(noisy))
        self.assertIn("Python", cleansed)
        self.assertTrue(out.get("jd_cleansed"))
        mock_invoke.assert_called_once()

    @patch("resume_app.agents._llm_invoke_with_retry", side_effect=RuntimeError("down"))
    def test_jd_cleanse_falls_back_to_heuristic(self, _mock_invoke):
        from resume_app.agents import jd_cleanse_node

        user = create_user("jdcleanseh")
        noisy = (
            "About the company\nWe are great.\n\n"
            "Responsibilities:\n- Build scalable systems\n- Lead engineers\n"
        )
        out = jd_cleanse_node(
            {
                "job_description": noisy,
                "job_title": "Principal Engineer",
                "llm": None,
                "user_id": user.id,
            }
        )
        self.assertTrue(out["job_description"])
        self.assertEqual(out["writer_job_description"], out["job_description"])
        self.assertFalse(out.get("jd_cleansed"))
        self.assertEqual((out.get("parse_info") or {}).get("path"), "heuristic_fallback")


class PromptStoreResolveTestCase(TestCase):
    def test_resolve_matching_uses_code_defaults_when_profile_empty(self):
        from resume_app.prompt_store import resolve_prompt_parts
        from resume_app.models import SystemPromptProfile

        prof = SystemPromptProfile.get_solo()
        s, u, leg = resolve_prompt_parts(prof, "matching")
        self.assertIsNone(leg)
        self.assertIn("JSON", s)
        self.assertIn("{resume_text}", u)

    def test_resolve_writer_heals_default_legacy_to_system_user_split(self):
        from resume_app.prompt_store import resolve_prompt_parts
        from resume_app.models import SystemPromptProfile
        from resume_app.prompts import DEFAULT_WRITER_PROMPT, DEFAULT_WRITER_SYSTEM, DEFAULT_WRITER_USER

        prof = SystemPromptProfile.get_solo()
        prof.writer = DEFAULT_WRITER_PROMPT
        prof.writer_system = ""
        prof.writer_user = ""
        prof.save()
        s, u, leg = resolve_prompt_parts(prof, "writer")
        self.assertIsNone(leg)
        self.assertEqual(s, DEFAULT_WRITER_SYSTEM)
        self.assertEqual(u, DEFAULT_WRITER_USER)

    def test_resolve_jd_cleanse_uses_code_defaults_when_profile_empty(self):
        from resume_app.prompt_store import resolve_prompt_parts
        from resume_app.models import SystemPromptProfile

        prof = SystemPromptProfile.get_solo()
        s, u, leg = resolve_prompt_parts(prof, "jd_cleanse")
        self.assertIsNone(leg)
        self.assertIn("core job signal", s.lower())
        self.assertIn("{job_description}", u)
        self.assertIn("{title}", u)


class LlmRateLimitTestCase(TestCase):
    def test_acquire_when_disabled_returns_noop(self):
        from resume_app.llm_rate_limit import acquire_llm_slot

        user = create_user("rluser")
        with patch("resume_app.llm_rate_limit._get_limits", return_value=None):
            rec, rel = acquire_llm_slot("Groq", "llama", 42, user=user)
        rec(10)
        rel()


class BuildOptimizerPromptStateTestCase(TestCase):
    def test_build_state_includes_split_writer_keys(self):
        from types import SimpleNamespace

        from resume_app.prompt_store import build_optimizer_graph_prompt_state

        user = create_user("buildstate")
        request = SimpleNamespace(user=user, auth=None, session={})

        st = build_optimizer_graph_prompt_state(None, request)
        self.assertIn("writer_prompt_system", st)
        self.assertIn("writer_prompt_user", st)
        self.assertIn("writer_prompt_legacy", st)
        self.assertTrue(st["writer_prompt_system"] or st["writer_prompt_user"] or st["writer_prompt_legacy"])


class SaveOptimizedDraftTestCase(TestCase):
    def test_save_draft_completed(self):
        from resume_app.models import JobDescription, OptimizedResume, UserResume
        from resume_app.services import save_optimized_draft_content

        user = create_user("savedraft")
        ur = UserResume.objects.create(owner=user, file="test.pdf", original_filename="t.pdf")
        jd = JobDescription.objects.create(content="JD")
        opt = OptimizedResume.objects.create(
            owner=user,
            original_resume=ur,
            job_description=jd,
            status=OptimizedResume.STATUS_COMPLETED,
            optimized_content="Original",
        )
        updated = save_optimized_draft_content(opt.id, "Edited final draft", user=user)
        self.assertEqual(updated.optimized_content, "Edited final draft")

    def test_save_draft_rejects_empty(self):
        from resume_app.models import JobDescription, OptimizedResume, UserResume
        from resume_app.services import DraftSaveError, save_optimized_draft_content

        user = create_user("savedraft2")
        ur = UserResume.objects.create(owner=user, file="test.pdf", original_filename="t.pdf")
        jd = JobDescription.objects.create(content="JD")
        opt = OptimizedResume.objects.create(
            owner=user,
            original_resume=ur,
            job_description=jd,
            status=OptimizedResume.STATUS_COMPLETED,
            optimized_content="x",
        )
        with self.assertRaises(DraftSaveError):
            save_optimized_draft_content(opt.id, "   ", user=user)

    def test_export_uses_saved_draft_not_original(self):
        """Regression: after Save draft, PDF/Word must export the edited text."""
        from resume_app.models import JobDescription, OptimizedResume, UserResume
        from resume_app.services import save_optimized_draft_content

        user = create_user("savedraftexport")
        ur = UserResume.objects.create(owner=user, file="test.pdf", original_filename="t.pdf")
        jd = JobDescription.objects.create(content="JD")
        opt = OptimizedResume.objects.create(
            owner=user,
            original_resume=ur,
            job_description=jd,
            status=OptimizedResume.STATUS_COMPLETED,
            optimized_content="## Original Heading\nOld body",
        )
        save_optimized_draft_content(opt.id, "## Edited Heading\nUNIQUE_EXPORT_MARKER_XYZ", user=user)

        client = Client()
        client.force_login(user)
        captured = {}

        def _capture_pdf(content):
            captured["content"] = content
            import io

            return io.BytesIO(b"%PDF-1.4 fake")

        with patch("resume_app.api._build_export_pdf", side_effect=_capture_pdf):
            resp = client.get(f"/api/resume/export/{opt.id}/pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("no-store", resp.get("Cache-Control", ""))
        self.assertIn("UNIQUE_EXPORT_MARKER_XYZ", captured.get("content", ""))
        self.assertNotIn("Old body", captured.get("content", ""))


class AtsJudgeProfilePromptTestCase(TestCase):
    def setUp(self):
        from resume_app.models import AtsJudgeProfile

        self.user = create_user("atsjudge")
        AtsJudgeProfile.objects.all().delete()
        self.strict = AtsJudgeProfile.objects.create(
            owner=None,
            name="Strict",
            slug="strict",
            ats_judge_system="STRICT_SYS",
            ats_judge_user="STRICT_USR {optimized_resume}",
        )
        self.default = AtsJudgeProfile.objects.create(
            owner=None,
            name="Default",
            slug="default-alt",
            is_default=True,
        )

    def test_resolve_ats_judge_parts_split(self):
        from resume_app.prompt_store import resolve_ats_judge_parts

        s, u, leg = resolve_ats_judge_parts(self.strict)
        self.assertEqual(s, "STRICT_SYS")
        self.assertIn("STRICT_USR", u)
        self.assertIsNone(leg)

    def test_build_state_uses_profile_id(self):
        from types import SimpleNamespace

        from resume_app.prompt_store import build_optimizer_graph_prompt_state

        request = SimpleNamespace(user=self.user, auth=None, session={})
        st = build_optimizer_graph_prompt_state(
            None,
            request,
            ats_judge_profile_id=self.strict.pk,
        )
        self.assertEqual(st["ats_judge_prompt_system"], "STRICT_SYS")
        self.assertIn("STRICT_USR", st["ats_judge_prompt_user"])

    def test_resolve_effective_id_workflow_over_default(self):
        from resume_app.models import OptimizerWorkflow
        from resume_app.prompt_store import resolve_effective_ats_judge_profile_id

        wf = OptimizerWorkflow.objects.create(
            owner=None,
            name="WF",
            steps=["writer", "ats_judge", "recruiter_judge"],
            ats_judge_profile=self.strict,
        )
        eff = resolve_effective_ats_judge_profile_id(workflow=wf, user=self.user)
        self.assertEqual(eff, self.strict.pk)
        eff_run = resolve_effective_ats_judge_profile_id(
            ats_judge_profile_id=self.default.pk,
            workflow=wf,
            user=self.user,
        )
        self.assertEqual(eff_run, self.default.pk)

    def test_prompt_override_wins_over_profile(self):
        from types import SimpleNamespace

        from resume_app.prompt_store import build_optimizer_graph_prompt_state

        request = SimpleNamespace(user=self.user, auth=None, session={})
        st = build_optimizer_graph_prompt_state(
            {"ats_judge": "OVERRIDE_LEGACY"},
            request,
            ats_judge_profile_id=self.strict.pk,
        )
        self.assertEqual(st["ats_judge_prompt_legacy"], "OVERRIDE_LEGACY")


class AtsJudgeParserTestCase(TestCase):
    def test_parse_ats_judge_fallback_full_schema(self):
        from resume_app.parsers import parse_ats_judge_fallback

        payload = json.dumps(
            {
                "ats_match_score": 82,
                "missing_keywords": ["Kubernetes", "CI/CD"],
                "formatting_issues": ["Two-column layout"],
                "strategic_feedback": "Add cloud keywords to summary.",
            }
        )
        result, raw = parse_ats_judge_fallback(payload)
        self.assertEqual(result.ats_match_score, 82)
        self.assertEqual(result.missing_keywords, ["Kubernetes", "CI/CD"])
        self.assertEqual(result.formatting_issues, ["Two-column layout"])
        self.assertIn("cloud keywords", result.strategic_feedback)
        self.assertIn("Missing keywords", result.feedback_text())
        self.assertIsNotNone(raw)

    def test_parse_ats_judge_fallback_legacy_score_key(self):
        from resume_app.parsers import parse_ats_judge_fallback

        payload = json.dumps({"ats_score": 75, "feedback": "Legacy shape"})
        result, _ = parse_ats_judge_fallback(payload)
        self.assertEqual(result.ats_match_score, 75)
        self.assertIn("Legacy shape", result.strategic_feedback)

    def test_parse_ats_judge_fallback_unwraps_last_ats_json_wrapper(self):
        from resume_app.parsers import parse_ats_judge_fallback

        payload = json.dumps(
            {
                "last_ats_json": {
                    "ats_match_score": 88,
                    "missing_keywords": ["Terraform"],
                    "formatting_issues": [],
                    "strategic_feedback": "Add IaC keywords.",
                }
            }
        )
        result, raw = parse_ats_judge_fallback(payload)
        self.assertEqual(result.ats_match_score, 88)
        self.assertEqual(result.missing_keywords, ["Terraform"])
        self.assertIn("IaC", result.strategic_feedback)
        self.assertEqual(raw["ats_match_score"], 88)

    def test_coerce_structured_judge_result_none_does_not_parse_literal_none(self):
        from resume_app.parsers import AtsJudgeResult, coerce_structured_judge_result, parse_ats_judge_fallback

        result, raw = coerce_structured_judge_result(
            None, AtsJudgeResult, parse_ats_judge_fallback, "ats_judge"
        )
        self.assertEqual(result.ats_match_score, 70)
        self.assertIn("Could not parse", result.strategic_feedback)
        self.assertIsNone(raw)

    def test_coerce_structured_judge_result_from_dict(self):
        from resume_app.parsers import AtsJudgeResult, coerce_structured_judge_result, parse_ats_judge_fallback

        payload = {
            "ats_match_score": 91,
            "missing_keywords": ["GraphQL"],
            "formatting_issues": ["Tables"],
            "strategic_feedback": "Surface API design experience.",
        }
        result, raw = coerce_structured_judge_result(
            payload, AtsJudgeResult, parse_ats_judge_fallback, "ats_judge"
        )
        self.assertIsInstance(result, AtsJudgeResult)
        self.assertEqual(result.ats_match_score, 91)
        self.assertEqual(raw["missing_keywords"], ["GraphQL"])

    def test_coerce_structured_judge_result_python_repr_dict_string(self):
        from resume_app.parsers import AtsJudgeResult, coerce_structured_judge_result, parse_ats_judge_fallback

        payload = {
            "ats_match_score": 83,
            "missing_keywords": [],
            "formatting_issues": [],
            "strategic_feedback": "Good match overall.",
        }
        result, _ = coerce_structured_judge_result(
            str(payload), AtsJudgeResult, parse_ats_judge_fallback, "ats_judge"
        )
        self.assertEqual(result.ats_match_score, 83)
        self.assertIn("Good match", result.strategic_feedback)

    def test_resolve_judge_scores_from_structured_ats(self):
        from resume_app.agents import _resolve_judge_scores
        from resume_app.parsers import AtsJudgeResult

        data = AtsJudgeResult(
            ats_match_score=90,
            missing_keywords=["Rust"],
            strategic_feedback="Mention systems programming.",
        )
        score, feedback, json_out = _resolve_judge_scores(data, None)
        self.assertEqual(score, 90)
        self.assertIn("Rust", feedback)
        self.assertEqual(json_out["ats_match_score"], 90)
        self.assertEqual(json_out["missing_keywords"], ["Rust"])


class JobSourcesDateFilterTestCase(TestCase):
    def test_parse_date_posted_from_iso_string(self):
        from resume_app.job_sources import _parse_date_posted

        dt = _parse_date_posted("2026-05-28T12:00:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 5)
        self.assertEqual(dt.day, 28)

    def test_filter_rows_by_max_age_drops_stale(self):
        from datetime import timedelta

        from django.utils import timezone

        from resume_app.job_sources import filter_rows_by_max_age

        now = timezone.now()
        rows = [
            {"title": "Fresh", "date_posted": now - timedelta(hours=24)},
            {"title": "Stale", "date_posted": now - timedelta(days=10)},
            {"title": "Unknown"},
        ]
        kept = filter_rows_by_max_age(rows, hours_old=168)
        titles = [r["title"] for r in kept]
        self.assertEqual(titles, ["Fresh", "Unknown"])


class JobListingUpsertTestCase(TestCase):
    def test_upsert_sets_posted_at_on_create(self):
        posted = timezone.now() - timedelta(days=2)
        row = {
            "source": "jobspy_linkedin",
            "external_id": "upsert-posted-1",
            "title": "Engineer",
            "company_name": "Acme",
            "location": "Remote",
            "description": "Build things",
            "job_url": "https://example.com/j/2",
            "date_posted": posted,
        }
        job, created = upsert_job_listing_from_fetch(row)
        self.assertTrue(created)
        self.assertIsNotNone(job.posted_at)
        self.assertEqual(job.posted_at, posted)

    def test_upsert_sets_fetched_at_on_create(self):
        row = {
            "source": "jobspy_indeed",
            "external_id": "upsert-create-1",
            "title": "Engineer",
            "company_name": "Acme",
            "location": "Remote",
            "description": "Build things",
            "job_url": "https://example.com/j/1",
        }
        job, created = upsert_job_listing_from_fetch(row)
        self.assertTrue(created)
        self.assertIsNotNone(job.fetched_at)

    def test_upsert_update_does_not_null_fetched_at(self):
        row = {
            "source": "jobspy_indeed",
            "external_id": "upsert-update-1",
            "title": "Engineer",
            "company_name": "Acme",
            "location": "",
            "description": "",
            "job_url": "",
        }
        job, _ = upsert_job_listing_from_fetch(row)
        original = job.fetched_at
        row["title"] = "Senior Engineer"
        job2, created = upsert_job_listing_from_fetch(row)
        self.assertFalse(created)
        self.assertEqual(job.id, job2.id)
        self.assertEqual(job2.title, "Senior Engineer")
        self.assertEqual(job2.fetched_at, original)

    def test_upsert_repairs_legacy_empty_fetched_at(self):
        """Django 5 update_or_create breaks when fetched_at is stored as '' in SQLite."""
        from django.db import connection

        job = JobListing.objects.create(
            source="jobspy_indeed",
            external_id="legacy-empty-fetched-at",
            title="Old",
            company_name="Co",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE resume_app_joblisting SET fetched_at = '' WHERE id = %s",
                [job.pk],
            )
        job.refresh_from_db()
        self.assertIsNone(job.fetched_at)
        row = {
            "source": "jobspy_indeed",
            "external_id": "legacy-empty-fetched-at",
            "title": "Updated",
            "company_name": "Co",
            "location": "",
            "description": "",
            "job_url": "",
        }
        job2, created = upsert_job_listing_from_fetch(row)
        self.assertFalse(created)
        self.assertEqual(job2.title, "Updated")
        self.assertIsNotNone(job2.fetched_at)


class TrackListLibraryResumeTestCase(TenantTestCase):
    def setUp(self):
        super().setUp()
        from resume_app.experience import set_experience_mode
        from resume_app.models import UserExperienceSettings, Track

        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        set_experience_mode(self.user, UserExperienceSettings.ExperienceMode.POWER)
        Track.objects.get_or_create(
            owner=self.user,
            slug="ic",
            defaults={"label": "IC", "is_default": False},
        )
        Track.objects.get_or_create(
            owner=self.user,
            slug="mgmt",
            defaults={"label": "Management", "is_default": False},
        )

    def test_track_list_shows_only_library_resumes(self):
        library = UserResume.objects.create(
            owner=self.user,
            file="library.pdf",
            original_filename="library.pdf",
            is_library=True,
        )
        ephemeral = UserResume.objects.create(
            owner=self.user,
            file="ephemeral.pdf",
            original_filename="ephemeral.pdf",
            is_library=False,
        )
        response = self.client.get("/jobs/tracks/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, library.original_filename)
        self.assertNotContains(response, ephemeral.original_filename)

    def test_track_list_shows_stats_and_default_profile(self):
        track_count = Track.objects.for_user(self.user).count()
        unique_resumes = _count_unique_library_resumes(UserResume.objects.for_user(self.user))
        default = Track.objects.for_user(self.user).filter(is_default=True).first()
        response = self.client.get("/jobs/tracks/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Search profiles")
        self.assertContains(response, "Unique resumes")
        self.assertContains(response, "Total profiles")
        self.assertContains(response, "Default profile")
        self.assertContains(response, str(track_count))
        self.assertContains(response, str(unique_resumes))
        if default:
            self.assertContains(response, default.label)

    def test_track_list_sort_by_slug(self):
        Track.objects.create(owner=self.user, slug="aaa", label="Zebra track")
        Track.objects.create(owner=self.user, slug="zzz", label="Alpha track")
        response = self.client.get("/jobs/tracks/?sort=slug")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertLess(content.index("slug: aaa"), content.index("slug: zzz"))

    def test_track_list_shows_resume_filename_on_track(self):
        UserResume.objects.create(
            owner=self.user,
            file="dallas.pdf",
            original_filename="N_Principal_Dallas.pdf",
            track="ic",
            is_library=True,
        )
        response = self.client.get("/jobs/tracks/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "N_Principal_Dallas.pdf")

    def test_unique_resume_stat_dedupes_by_filename(self):
        UserResume.objects.create(
            owner=self.user,
            file="a.pdf",
            original_filename="Principal.pdf",
            track="ic",
            is_library=True,
        )
        UserResume.objects.create(
            owner=self.user,
            file="b.pdf",
            original_filename="principal.pdf",
            track="mgmt",
            is_library=True,
        )
        response = self.client.get("/jobs/tracks/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unique resumes")
        # Two rows, one distinct filename
        self.assertContains(response, ">1<")
        self.assertContains(response, "(2 uploads)")

    def test_edit_track_updates_attributes(self):
        track = Track.objects.for_user(self.user).get(slug="ic")
        response = self.client.post(
            "/jobs/tracks/",
            {
                "action": "edit_track",
                "original_slug": "ic",
                "slug": "ic",
                "label": "IC Updated",
                "description": "New description",
                "is_default": "1",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        track.refresh_from_db()
        self.assertEqual(track.label, "IC Updated")
        self.assertEqual(track.description, "New description")
        self.assertTrue(track.is_default)

    def test_edit_track_renames_slug_cascades_to_resume(self):
        UserResume.objects.create(
            owner=self.user,
            file="linked.pdf",
            original_filename="linked.pdf",
            track="mgmt",
            is_library=True,
        )
        response = self.client.post(
            "/jobs/tracks/",
            {
                "action": "edit_track",
                "original_slug": "mgmt",
                "slug": "management",
                "label": "Management",
                "description": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Track.objects.for_user(self.user).filter(slug="mgmt").exists())
        self.assertTrue(Track.objects.for_user(self.user).filter(slug="management").exists())
        resume = UserResume.library().for_user(self.user).get(original_filename="linked.pdf")
        self.assertEqual(resume.track, "management")

    def test_track_list_rejects_oversized_upload(self):
        oversized = SimpleUploadedFile(
            "big.pdf",
            b"%PDF" + (b"0" * (MAX_TRACK_RESUME_UPLOAD_BYTES + 1)),
            content_type="application/pdf",
        )
        before = UserResume.library().for_user(self.user).count()
        response = self.client.post(
            "/jobs/tracks/",
            {"action": "upload_resume", "resume_file": oversized},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10MB")
        self.assertEqual(UserResume.library().for_user(self.user).count(), before)


class GeneratedResumeCleanupTestCase(TestCase):
    def test_purge_removes_old_ephemeral_resumes(self):
        from .resume_cleanup import purge_generated_user_resumes

        user = create_user("cleanup")
        old = UserResume.objects.create(
            owner=user,
            file="old.pdf",
            original_filename="old.pdf",
            is_library=False,
        )
        UserResume.objects.filter(pk=old.pk).update(
            uploaded_at=timezone.now() - timedelta(days=10),
        )
        keep = UserResume.objects.create(
            owner=user,
            file="new.pdf",
            original_filename="new.pdf",
            is_library=False,
        )
        library = UserResume.objects.create(
            owner=user,
            file="lib.pdf",
            original_filename="lib.pdf",
            is_library=True,
        )
        UserResume.objects.filter(pk=library.pk).update(
            uploaded_at=timezone.now() - timedelta(days=10),
        )

        removed = purge_generated_user_resumes(retention_days=7)
        self.assertEqual(removed, 1)
        self.assertFalse(UserResume.objects.filter(pk=old.pk).exists())
        self.assertTrue(UserResume.objects.filter(pk=keep.pk).exists())
        self.assertTrue(UserResume.objects.filter(pk=library.pk).exists())


class SystemOptimizerWorkflowTestCase(TestCase):
    """Admin-managed workflows (owner=null) are visible to all subscribers."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from resume_app.models import OptimizerWorkflow

        User = get_user_model()
        self.admin = User.objects.create_user(username="wf_admin", password="pass")
        self.admin.is_staff = True
        self.admin.save(update_fields=["is_staff"])
        self.subscriber = User.objects.create_user(username="wf_sub", password="pass")
        self.system_wf = OptimizerWorkflow.objects.create(
            owner=None,
            name="JD-Rec-ATS-Writer-ATS",
            steps=["jd_cleanse", "recruiter_judge", "ats_judge", "writer", "ats_judge"],
        )
        self.personal_wf = OptimizerWorkflow.objects.create(
            owner=self.subscriber,
            name="Private legacy",
            steps=["writer"],
        )

    def test_list_shows_system_not_personal(self):
        from resume_app.prompt_store import list_optimizer_workflows

        names = {w.name for w in list_optimizer_workflows(self.subscriber)}
        self.assertIn("JD-Rec-ATS-Writer-ATS", names)
        self.assertNotIn("Private legacy", names)

    def test_subscriber_can_resolve_system_workflow(self):
        from resume_app.prompt_store import get_optimizer_workflow_by_id

        wf = get_optimizer_workflow_by_id(self.system_wf.pk, self.subscriber)
        self.assertIsNotNone(wf)
        self.assertEqual(wf.name, "JD-Rec-ATS-Writer-ATS")
        self.assertIsNone(get_optimizer_workflow_by_id(self.personal_wf.pk, self.subscriber))

    def test_subscriber_forbidden_from_workflow_manage_ui(self):
        self.client.force_login(self.subscriber)
        resp = self.client.get("/workspace/workflows/")
        self.assertEqual(resp.status_code, 403)

    def test_staff_sees_workflow_list(self):
        self.client.force_login(self.admin)
        resp = self.client.get("/workspace/workflows/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "JD-Rec-ATS-Writer-ATS")
