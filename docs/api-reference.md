# API And Route Reference

This document catalogs the complete route surface for HireEdge, registered across `django_project/core/urls.py`, `resume_app/api.py`, and `resume_app/jobs_api.py`.

---

## 1. Authentication & Security Policies

- **Global Protection:** `LoginRequiredMiddleware` protects the site. Unauthenticated HTML requests redirect to `/accounts/login/`; unauthenticated `/api/*` requests return JSON `401`.
- **Public Exceptions:** `/` is public because the resolved view is `landing_view`. Prefix exemptions in `LOGIN_EXEMPT_URL_PREFIXES` are `/accounts/`, `/admin/`, `/static/`, `/health/`, `/billing/stripe/webhook/`, and `/legal/`. `/billing/` and `/getting-started/` require login. `/favicon.ico` is not exempt.
- **Email verification:** When `REQUIRE_EMAIL_VERIFICATION` is on, an unverified non-staff user is sent to `/accounts/verify-email/` (resend + Settings link). Unverified `/api/*` requests return JSON `403`. Exempt prefixes for that check include `/accounts/`, `/admin/`, `/static/`, `/media/`, `/legal/`, and `/settings/`.
- **API Authentication:** Supports Django session auth or Bearer token auth (`Authorization: Bearer <token>`) via `CustomerApiKey` (`SessionOrApiKeyAuth`).
- **Media Authorization:** `/media/<path>` requests are routed through `serve_media_view`, which validates that the requesting user owns the requested asset.

---

## 2. Server-Rendered HTML & Subsystem Routes

### 2.1 Accounts, Billing & Staff
- `GET /` — Public landing page.
- `GET/POST /accounts/login/` — User login.
- `POST /accounts/logout/` — User logout.
- `GET/POST /accounts/signup/` — Registration (when `SIGNUP_ENABLED=True`).
- `GET/POST /accounts/password-reset/` and `GET /accounts/password-reset/done/` — Request a reset.
- `GET/POST /accounts/reset/<uidb64>/<token>/` and `GET /accounts/reset/done/` — Confirm and complete the reset. There is no `/accounts/password-reset/confirm/` or `/accounts/password-reset/complete/`.
- `GET /accounts/verify-email/` — Email verification interstitial (check inbox / resend). Login required.
- `GET /accounts/verify-email/<uidb64>/<token>/` — Confirm account email from the mailed link.
- `POST /accounts/resend-verification/` — Resend verification email.
- `GET/POST /getting-started/` — Legacy onboarding URL. Redirects to `/jobs/profiler/` (or Cockpit when setup is complete). Power users go to Cockpit.
- `GET/POST /billing/` — Plan quotas, usage ledger, and Stripe checkout/portal integration.
- `POST /billing/stripe/webhook/` — Public Stripe webhook receiver (`csrf_exempt`; verifies signatures in production).
- `GET /staff/users/` — Staff dashboard for user search, quota auditing, and plan adjustment.
- `POST /staff/users/<user_id>/action/` — Staff administrative actions (suspend, activate, set plan).
- `POST /hijack/acquire/` and `POST /hijack/release/` — Staff impersonation via `django-hijack` (audited in `ImpersonationAuditLog`). Acquire posts `user_pk`.
- `/admin/` — Django admin console.
- `GET /legal/privacy/`, `GET /legal/terms/` — Public compliance pages (prefix `/legal/`).
- `GET /health/`, `GET /health/live` — Public liveness JSON.
- `GET /health/ready` — Public readiness JSON (database and cache).
- `GET /media/<path>` — Authenticated media. `serve_media_view` checks ownership.

---

### 2.2 Tab 1: Target & Discover
- `GET /jobs/profiler/` — **AI Resume Profiler** view; parses candidate resume, presents career analysis, and scaffolds search profiles.
- `POST /jobs/profiler/api/analyze/` — Analyzes uploaded resume PDF; saves extraction to `CareerProfileAnalysis(analysis_data)`.
- `POST /jobs/profiler/api/provision/` — Creates or updates `SearchProfile` records and associates the source `UserResume`.
- `POST /jobs/profiler/api/preview-matches/` — Runs live multi-board preview query across job boards for target titles without writing to the database.
- `GET/POST /jobs/search/` — **Find jobs** view. Live query across Indeed, LinkedIn, Greenhouse, BuiltIn, Levels.fyi, and Dice. Card CTA is **Save to Cockpit** / **In Cockpit**. Unbiased mode is the “None (Unbiased / No Profile)” scoring option.
- `GET /jobs/tracks/` — Search profile management and resume assignment.
- `POST /jobs/tracks/<slug>/delete/` — Delete a search profile.

---

### 2.3 Tab 2: Career Cockpit & Job Automation
- `GET/POST /jobs/cockpit/` — **Career Cockpit**. GET renders the tri-pane workstation. POST runs board actions (`save`, `like`, `dislike`, `delete`, search-profile saves). `openAutomationDrawer()` opens **Pipeline Automation Policies** (purge, promote, retention). Search schedule, boards, and Run Search Now live on the left rail and in the Search Profile drawer (`openEditProfileDrawer`).
- `POST /jobs/cockpit/api/skill-radar/` — Run or force Skill Radar diagnostics (`job_id`, `track`, `force`). The view reads `track`, not `profile`. The cockpit client currently calls this URL with `profile=`.
- `GET /jobs/cockpit/api/skill-radar/?job_id=<id>&track=<slug>` — Read cached or persisted Skill Radar diagnostics. See [Skill Radar Diagnostics](features/skill-radar-diagnostics.md). Primary cache key is `cockpit_llm_skill_radar_{user}_{resume|none}_{job}` (7 days). `cockpit_ollama_skill_radar_…` is a read alias.
- `POST /jobs/tasks/<task_id>/run/` — Trigger manual search run immediately (sets atomic pending key + 3m cooldown).
- `GET /jobs/tasks/<task_id>/status/` — JSON status endpoint polled by browser during active search.
- `POST /jobs/tasks/<task_id>/toggle/` — Toggle task active/inactive status.
- `GET/POST /jobs/tasks/new/` — Create a scheduled search task.
- `GET/POST /jobs/tasks/<task_id>/edit/` — Edit an existing search task.
- `GET /jobs/tracks/scheduled-runs/<run_id>/details-json/` — Disposition breakdown JSON for the Cockpit inspection modal.
- `GET /jobs/tracks/scheduled-runs/<run_id>/download-csv/` — Download run job dispositions as CSV.
- `GET /jobs/automation/` (`job_automation`) — **302** to `/jobs/cockpit/?drawer=automation`, keeping the original query string. Not a permanent redirect, and not a bare cockpit URL. Any logged-in user can open that drawer; the left-rail Pipeline Policies card is staff and Power only.

---

### 2.4 Tab 3: Tracker & Momentum Analytics
- `GET /performance/` — **Tracker: Career Momentum & Application Outcomes**; active streak tracking, weekly target progress bar, and interview loop management.
- `GET /performance/api/metrics/` — Progressive hydration JSON endpoint for metrics (accepts `?refresh=1`).
- Weekly application target is `AppAutomationSettings.weekly_target_applications` (model default 10). `update_dashboard_settings_api` exists in `views.py` and is **not registered**. `POST /performance/settings/` is not a route.
- `GET/POST /system/fit-inspector/` — Staff diagnostics for embeddings and scoring. Non-staff users are **302**’d to the Cockpit: `drawer=inspector` and `stage=vetting` when `job_id` is present; otherwise Cockpit New (`stage=pipeline`) with no drawer.

---

### 2.5 Kanban Stages & Legacy Pipeline Routes
- `GET /jobs/pipeline/`, `/jobs/vetting/`, `/jobs/applying/`, `/jobs/done/` — **302** to `/jobs/cockpit/?stage=pipeline|vetting|applying|done`. Other query params are kept; `stage` is overwritten. These GETs do not render a board.
- `POST` on those four URLs is handled by `pipeline_board_view` (the action runs, then a redirect).
- `GET /jobs/<job_listing_id>/focus-breakdown/` — Staff-only focus score diagnostics (non-staff **403**).
- `GET /jobs/<job_listing_id>/focus-breakdown/<liked_job_id>/` — Staff **302** to `/system/fit-inspector/?job_id=<job_listing_id>`. `liked_job_id` is dropped. Non-staff **403** before that redirect.
- `GET /jobs/pipeline/<entry_id>/applied-resume/` — Applied-resume JSON.
- `GET /jobs/pipeline/<entry_id>/export/pdf/` and `.../export/docx/` — Applied resume exports.
- `GET/POST /jobs/vetting/match-debug/<job_listing_id>/` — Single-job resume match debugging.

---

### 2.6 Resume Optimizer & Prompt Studio
- `GET/POST /resume/optimizer/` — Main AI resume optimizer interface with decoupled workflows: Save Draft (`action="save_draft"`, `Ctrl+S`, stays in `Applying`), I Submitted This (`action="approve_and_mark_applied"`, `Ctrl+Enter`, moves to `Applied`), 1-click external Employer Post, and dynamic "Back to Cockpit" (`Esc`) return loop.
- `GET /resume/status/<resume_id>/` — Polling status endpoint for active optimization runs.
- `POST /resume/status/<resume_id>/draft/` — Save edited resume text draft.
- `GET /resume/optimizer/context/<resume_id>/` — Inspect `optimizer_context_snapshot` token budgets.
- `GET/POST /resume/prompts/` — **Prompt Studio & Library Workstation**; comprehensive prompt IDE for inspecting, versioning, routing, and testing prompt definitions across the platform (`writer`, `resume_judge`, `matching`, `insights`, `jd_cleanse`, `cover_letter`, `interview_prep`, `skill_radar`, `career_profiler`, `pipeline_skill_extract`, `pipeline_resume_refine`) and custom `AtsJudgeProfile` entries. Designed with a 3-pane responsive layout that adapts to vertical/portrait displays (left navigation rail, stacked editor, and in-authoring test bench) and mobile viewport drawers (`< lg`).
- `POST /resume/prompts/api/save/` — Persists prompt modifications and writes revision record to `PromptVersionHistory`.
- `POST /resume/prompts/api/set-model/` — Saves user provider and model preference to `TenantPromptModelPreference`.
- `POST /resume/prompts/api/test-run/` — Executes test completion of an edited prompt via `resume_app/llm/gateway.py`.
- `GET /resume/prompts/api/history/` — Returns JSON array of revisions from `PromptVersionHistory` for a given prompt key.
- `POST /resume/prompts/api/restore/` — Restores previous version from `PromptVersionHistory` into active prompt template.
- `POST /resume/prompts/api/delete-version/` — Deletes a single version row from `PromptVersionHistory`.
- `POST /resume/prompts/api/clear-history/` — Deletes all version rows in `PromptVersionHistory` for the given prompt key.
- `GET /resume/prompts/api/resume-content/` — Reads and returns raw text of candidate's resume for test fixtures.
- `GET /resume/prompts/api/job-content/` — Reads and returns raw description text of a job listing for test fixtures.
- `GET/POST /resume/llm-test/` — Developer test view for LLM completions.
- `GET /workspace/workflows/` — List system-wide optimizer workflows (staff).
- `GET/POST /workspace/workflows/new/` — Create optimizer workflow (staff).
- `GET/POST /workspace/workflows/<workflow_id>/edit/` — Edit optimizer workflow (staff).
- `POST /workspace/workflows/<workflow_id>/delete/` — Delete optimizer workflow. Other methods redirect to the list.
- `GET/POST /settings/` — User settings (LLM keys, model preferences, usage, stop controls; App Automation tab is staff-only).

---

### 2.7 Huey Queue Monitor (Staff Only)
- `GET /jobs/huey/` — Huey worker dashboard (queue depths, worker health, periodic tasks).
- `POST /jobs/huey/periodic/<task_name>/revoke/` — Pause periodic task.
- `POST /jobs/huey/periodic/<task_name>/restore/` — Restore periodic task.
- `POST /jobs/huey/flush-queue/` — Flush pending task queue.
- `POST /jobs/huey/run-cleanup/` — Trigger database and resume file cleanup.
- `POST /jobs/huey/task/<task_name>/run/` — Execute specific task immediately.

---

## 3. Django Ninja REST API: `/api/resume/`

Mounted in `resume_app/api.py`.

### 3.1 Optimizer & LLM Operations
- `POST /api/resume/optimize` — Upload source PDF and job description; enqueues `optimize_resume_task`.
- `GET /api/resume/status/{resume_id}` — Retrieve optimization run state, scores, and logs.
- `POST /api/resume/status/{resume_id}/draft` — Persist modified resume markdown.
- `POST /api/resume/status/{resume_id}/generate-cover-letter` — Generate tailored cover letter.
- `POST /api/resume/status/{resume_id}/save-cover-letter` — Persist edited cover letter.
- `POST /api/resume/status/{resume_id}/cancel` — Cancel active optimization task.
- `POST /api/resume/run-step` — Execute an isolated step (`writer`, `ats_judge`, `recruiter_judge`, or `jd_cleanse`).
- `POST /api/resume/fit-check` — Score resume fit against a job description.
- `POST /api/resume/llm/complete` — Generic LLM completion. Requires `api_access`. Pro and BYOK require a user key (`byok_required_for_completion`). Power allows platform keys. The daily token cap is `Plan.llm_tokens_per_day` when that column is non-zero (seeded Power is 500,000). `LLM_DAILY_TOKEN_LIMIT_BY_PLAN` is used only when the column is 0 and the slug is in that map.
- `POST /api/resume/llm/connect` — Validate and store encrypted provider API keys.
- `GET /api/resume/llm/models` — Retrieve available models for a provider.
- `POST /api/resume/llm/set-default-model` — Set default provider/model preference.
- `GET /api/resume/export/{resume_id}/pdf` — Export tailored resume as PDF.
- `GET /api/resume/export/{resume_id}/docx` — Export tailored resume as Word document.
- `GET /api/resume/export/{resume_id}/cover-letter/pdf` and `.../cover-letter/docx` — Export the cover letter.
- `GET /api/resume/prompts` — Effective prompt templates.
- `GET/POST/PUT/DELETE /api/resume/ats-judge-profiles` and `.../ats-judge-profiles/{profile_id}` — ATS judge profile CRUD (`PUT` and `DELETE` on the id).
- `GET/POST/PUT/DELETE /api/resume/workflows` and `.../workflows/{workflow_id}` — Optimizer workflow CRUD.
- `GET /api/docs` and `GET /api/openapi.json` — Ninja docs. They are not login-exempt, so an anonymous caller receives JSON `401`.

---

## 4. Django Ninja REST API: `/api/resume/jobs/`

Mounted in `resume_app/jobs_api.py`.

### 4.1 Jobs, Pipeline & Feedback Actions (With Exact Side Effects)

- `GET /api/resume/jobs/pipeline` — Retrieve user's pipeline jobs for active stage/profile.
- `POST /api/resume/jobs/pipeline/delete` — Soft-delete via `mark_deleted()` (`stage='deleted'`, `removed_at` set). Query args are `job_listing_id` and `track`.
- `POST /api/resume/jobs/search` — Execute multi-board search query across external sources. Ingests and upserts global `JobListing` records into the catalog (assigning durable integer IDs and deduplication URLs), but performs ZERO writes to the candidate's personal pipeline or preference centroid.
- `POST /api/resume/jobs/ai-match` — Run batch LLM fit evaluations and return candidate match probabilities.
- `POST /api/resume/jobs/insights` — Extract aggregated skill insights across multiple jobs.
- `GET /api/resume/jobs/{job_listing_id}` — Retrieve global job listing details.
- `POST /api/resume/jobs/fetch-description` — Fetch and enrich full job description from source URL.
- `POST /api/resume/jobs/{job_listing_id}/save` — **Find jobs → Cockpit**. Optional query params `track` and `profile`.
  - *Side Effect:* Writes `JobListingAction.action='saved'`. A new or restored row lands in `vetting` unless the restored stage is already `applying` or `done`. An active row keeps its stage. There is no `is_saved` column.
- `POST /api/resume/jobs/{job_listing_id}/unsave` — Deletes `action='saved'`. Soft-deletes the pipeline row only when it is not `applying` or `done`.
- `POST /api/resume/jobs/{job_listing_id}/like` — `action='liked'` and a liked embedding. Clears opposing dislike on that profile. Stage is unchanged.
- `POST /api/resume/jobs/{job_listing_id}/unlike` — Deletes the liked action and liked embedding for the profile.
- `POST /api/resume/jobs/{job_listing_id}/dislike` — `action='disliked'` and a dislike embedding. Does **not** change `PipelineEntry`. Board `action="dislike"` is the path that also calls `mark_deleted()`.
- `POST /api/resume/jobs/{job_listing_id}/hide` — `action='hidden'`. No embedding.
- `POST /api/resume/jobs/{job_listing_id}/unhide` — Clears hide (and dislike) for the profile.
- `POST /api/resume/jobs/{job_listing_id}/mark-applied` — Sets `JobMatchResult.status='applied'` for `(job, resume)`. Returns 404 when no match row exists. Does not move the pipeline.
- `GET /api/resume/jobs/pipeline-entry/{pipeline_entry_id}/interview-prep` — Read `PipelineEntry.interview_prep`.
- `POST /api/resume/jobs/pipeline-entry/{pipeline_entry_id}/generate-interview-prep` — Generate prep into `PipelineEntry.interview_prep`.
- `POST /api/resume/jobs/pipeline-entry/{pipeline_entry_id}/save-interview-prep` — Save edited prep to `PipelineEntry.interview_prep`.
- `POST /api/resume/jobs/pipeline-entry/{pipeline_entry_id}/log-interview` — Create an `EmployerInterviewEvent`.
- `GET /api/resume/jobs/disqualifiers` — List `UserDisqualifier` phrases.
- `POST /api/resume/jobs/disqualifiers` — Create one exclusion phrase.
- `DELETE /api/resume/jobs/disqualifiers/{disqualifier_id}` — Delete one phrase.
- `GET /api/resume/jobs/resumes` — Resume options for the active user.
- `POST /api/resume/jobs/search/async` and `GET /api/resume/jobs/search/async/{task_id}/status` — Async search plus status.
- `GET /api/resume/jobs/pipeline/applied-jobs` — Applied pipeline rows.
- `POST /api/resume/jobs/pipeline-resume-summary/start`, `GET .../status`, `POST .../stop` — Pipeline resume summary job.
- `GET /api/resume/jobs/matches` — Stored match payloads.
- `POST /api/resume/jobs/run-keyword-search` — Keyword search run.
- `GET /api/resume/jobs/saved` and `GET /api/resume/jobs/disliked` — Saved and disliked listings.
- `GET /api/resume/jobs/focus-breakdown/{job_listing_id}` — Focus breakdown JSON.
- `POST /api/resume/jobs/{job_listing_id}/match` — Score one listing against a resume.
