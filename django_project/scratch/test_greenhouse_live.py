import os, sys
sys.path.insert(0, os.path.abspath('django_project'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
import django; django.setup()

from resume_app.sourcing.clients.greenhouse_client import (
    fetch_greenhouse_jobs,
    fetch_greenhouse_job_detail,
    extract_greenhouse_job_id_and_board,
    enrich_greenhouse_job_listing_description,
)
from resume_app.job_sources import fetch_jobs, normalize_site_names
from resume_app.sourcing.service import JobIngestionService
from resume_app.models import JobListing

print("=== 1. Testing normalize_site_names ===")
norm = normalize_site_names(["greenhouse", "indeed"])
print("Normalized:", norm)
assert "greenhouse" in norm

print("\n=== 2. Testing extract_greenhouse_job_id_and_board ===")
board, jid = extract_greenhouse_job_id_and_board("https://boards.greenhouse.io/figma/jobs/5426468004?gh_jid=5426468004")
print(f"Extracted: board={board}, jid={jid}")
assert board == "figma"
assert jid == "5426468004"

board, jid = extract_greenhouse_job_id_and_board("greenhouse:stripe:8113337")
print(f"Extracted: board={board}, jid={jid}")
assert board == "stripe"
assert jid == "8113337"

print("\n=== 3. Testing fetch_greenhouse_jobs (Stripe / Python / Remote) ===")
jobs = fetch_greenhouse_jobs("python engineer", location="remote", results_wanted=5, boards=["stripe", "figma", "anthropic"])
print(f"Fetched {len(jobs)} jobs")
for j in jobs:
    print(f" - [{j['company_name']}] {j['title']} ({j['location']})")
    print(f"   URL: {j['job_url']}")
    print(f"   Desc chars: {len(j.get('description', ''))}")
    print(f"   External ID: {j['external_id']}")
assert len(jobs) > 0
assert jobs[0]['source'] == 'greenhouse'

print("\n=== 4. Testing fetch_jobs with site_name=['greenhouse'] ===")
merged = fetch_jobs("software engineer", site_name=["greenhouse"], results_wanted=3)
print(f"Merged count: {len(merged)}")
assert len(merged) > 0
print(f"First result: {merged[0]['title']} at {merged[0]['company_name']}")

print("\n=== 5. Testing JobIngestionService with greenhouse ===")
svc = JobIngestionService()
dtos = svc.fetch_all("manager", site_names=["greenhouse"], results_wanted=3)
print(f"DTOs count: {len(dtos)}")
assert len(dtos) > 0
print(f"DTO 1: {dtos[0].title} at {dtos[0].company_name}")

print("\nALL LIVE TESTS COMPLETED SUCCESSFULLY!")
