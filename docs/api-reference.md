# API And Route Reference

This document catalogs the complete route surface for HireEdge, registered across `django_project/core/urls.py`, `resume_app/api.py`, and `resume_app/jobs_api.py`.

---

## 1. Authentication & Security Policies

- **Global Protection:** `LoginRequiredMiddleware` protects the site. Unauthenticated HTML requests redirect to `/accounts/login/`; unauthenticated `/api/*` requests return JSON `401`.
- **Public Exceptions:** `/` (landing), `/accounts/*` (auth flows), `/admin/`, `/static/`, `/legal/privacy/`, and `/legal/terms/`.
- **API Authentication:** Supports Django session auth or Bearer token auth (`Authorization: Bearer <token>`) via `CustomerApiKey` (`SessionOrApiKeyAuth`).
- **Media Authorization:** `/media/<path>` requests are routed through `serve_media_view`, which validates that the requesting user owns the requested asset.

---

## 2. Server-Rendered HTML Routes

### 2.1 Accounts, Billing & Staff
- `GET /` — Public landing page.
- `GET/POST /accounts/login/` — User login.
- `POST /accounts/logout/` — User logout.
- `GET/POST /accounts/signup/` — Registration (when `SIGNUP_ENABLED=True`).
- `GET/POST /accounts/password-reset/` (+ `done/`, `confirm/`, `complete/`) — Standard password reset flow.
- `GET /accounts/verify-email/<uidb64>/<token>/` — Confirm account email.
- `POST /accounts/resend-verification/` — Resend verification email.
- `GET /getting-started/` — Onboarding checklist.
- `GET /billing/` — Plan quotas, usage ledger, and Stripe checkout/portal integration.
- `POST /billing/stripe/webhook/` — Stripe webhook receiver (verifies signatures in production).
- `GET /staff/users/` — Staff dashboard for user search, quota auditing, and plan adjustment.
- `POST /staff/users/<user_id>/action/` — Staff administrative actions (suspend, activate, set plan).
- `/hijack/` — Staff user impersonation via `django-hijack` (audited in `ImpersonationAuditLog`).
- `/admin/` — Django admin console.
- `GET /legal/privacy/`, `GET /legal/terms/` — Public compliance pages.

---

### 2.2 Career Cockpit & Job Automation
- `GET /jobs/cockpit/` — **Career Cockpit dashboard**; central hub for search profiles, task schedules, manual triggers, and run logs.
- `GET /jobs/cockpit/api/skill-radar/` — Skill frequency radar JSON for the active search profile.
- `POST /jobs/tasks/<task_id>/run/` — Trigger manual search run immediately (sets atomic pending key + 60m cooldown).
- `GET /jobs/tasks/<task_id>/status/` — JSON status endpoint polled by browser during active search.
- `POST /jobs/tasks/<task_id>/toggle/` — Toggle task active/inactive status.
- `GET/POST /jobs/tasks/new/` — Create a scheduled search task.
- `GET/POST /jobs/tasks/<task_id>/edit/` — Edit an existing search task.
- `GET /jobs/tracks/scheduled-runs/<run_id>/details-json/` — Disposition breakdown JSON for the Cockpit inspection modal.
- `GET /jobs/tracks/scheduled-runs/<run_id>/download-csv/` — Download run job dispositions as CSV.
- `GET /jobs/automation/` — **Decommissioned legacy route; redirects to `/jobs/cockpit/`**.

---

### 2.3 Job Search & Pipeline Management
- `GET/POST /jobs/search/` — Search external jobs (Indeed, LinkedIn, Greenhouse, BuiltIn, Levels.fyi, Dice) and submit feedback.
- `GET/POST /jobs/pipeline/` — Pipeline Kanban board (Stage: New).
- `GET/POST /jobs/vetting/` — Vetting Kanban board (Stage: Review).
- `GET/POST /jobs/applying/` — Applying Kanban board (Stage: Tailoring).
- `GET/POST /jobs/done/` — Done Kanban board (Stage: Applied / Completed).
- `GET /jobs/tracks/` — Search profile management and resume assignment.
- `POST /jobs/tracks/<slug>/delete/` — Delete a search profile.
- `GET /jobs/<job_listing_id>/focus-breakdown/` — Staff-only focus score diagnostics.
- `GET /jobs/<job_listing_id>/focus-breakdown/<liked_job_id>/` — Staff-only focus alignment analysis.
- `GET/POST /jobs/vetting/match-debug/<job_listing_id>/` — Single-job resume match debugging.

---

### 2.4 Performance Dashboard & Fit Diagnostics
- `GET /performance/` — Momentum & application conversion dashboard.
- `GET /performance/api/metrics/` — Progressive hydration JSON endpoint for metrics (accepts `?refresh=1`).
- `POST /performance/settings/` — Update performance dashboard tracking settings.
- `GET /system/fit-inspector/` — Diagnostics for semantic embeddings, BM25 keywords, and recruiter scoring.

---

### 2.5 Resume Optimizer & Prompts
- `GET/POST /resume/optimizer/` — Main AI resume optimizer interface.
- `GET /resume/status/<resume_id>/` — Polling status endpoint for active optimization runs.
- `POST /resume/status/<resume_id>/draft/` — Save edited resume text draft.
- `GET /resume/optimizer/context/<resume_id>/` — Inspect `optimizer_context_snapshot` token budgets.
- `GET/POST /resume/prompts/` — Staff prompt library and global ATS judge profile editor.
- `GET/POST /resume/llm-test/` — Developer test view for LLM completions.
- `GET /workspace/workflows/` — List system-wide optimizer workflows (staff).
- `GET/POST /workspace/workflows/new/` — Create optimizer workflow (staff).
- `GET/POST /workspace/workflows/<id>/edit/` — Edit optimizer workflow (staff).
- `GET/POST /settings/` — User settings (LLM keys, model preferences, usage, stop controls).

---

### 2.6 Huey Queue Monitor (Staff Only)
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
- `POST /api/resume/llm/complete` — Generic LLM completion (requires `api_access` plan permission).
- `POST /api/resume/llm/connect` — Validate and store encrypted provider API keys.
- `GET /api/resume/llm/models` — Retrieve available models for a provider.
- `POST /api/resume/llm/set-default-model` — Set default provider/model preference.
- `GET /api/resume/export/{resume_id}/pdf` — Export tailored resume as PDF.
- `GET /api/resume/export/{resume_id}/docx` — Export tailored resume as Word document.

---

## 4. Django Ninja REST API: `/api/resume/jobs/`

Mounted in `resume_app/jobs_api.py`.

### 4.1 Jobs, Pipeline & Feedback Actions
- `GET /api/resume/jobs/pipeline` — Retrieve user's pipeline jobs for active stage/profile.
- `POST /api/resume/jobs/pipeline/delete` — Soft-delete pipeline entry.
- `POST /api/resume/jobs/search` — Execute multi-board search query.
- `POST /api/resume/jobs/ai-match` — Run batch LLM fit evaluations.
- `POST /api/resume/jobs/insights` — Extract aggregated skill insights across multiple jobs.
- `GET /api/resume/jobs/{job_listing_id}` — Retrieve global job listing details.
- `POST /api/resume/jobs/fetch-description` — Fetch and enrich full job description from source URL.
- `POST /api/resume/jobs/{job_listing_id}/save` — Save job to Favourites and add to pipeline in `Vetting` stage.
- `POST /api/resume/jobs/{job_listing_id}/unsave` — Unsave job and remove pipeline entry.
- `POST /api/resume/jobs/{job_listing_id}/like` — Record like action; updates preference centroid vector.
- `POST /api/resume/jobs/{job_listing_id}/unlike` — Clear like action.
- `POST /api/resume/jobs/{job_listing_id}/dislike` — Record dislike action; deprioritizes similar jobs and hides card.
- `POST /api/resume/jobs/{job_listing_id}/hide` — Hide listing from search without altering preference centroid.
- `POST /api/resume/jobs/{job_listing_id}/unhide` — Restore previously hidden listing.
- `POST /api/resume/jobs/{job_listing_id}/mark-applied` — Move pipeline entry to `Done` stage.
- `GET /api/resume/jobs/pipeline-entry/{id}/interview-prep` — Retrieve saved interview preparation questions.
- `POST /api/resume/jobs/pipeline-entry/{id}/generate-interview-prep` — Generate customized interview preparation using LLM.
- `POST /api/resume/jobs/pipeline-entry/{id}/save-interview-prep` — Save user-edited interview preparation.
- `GET /api/resume/jobs/disqualifiers` — List user exclusion keyword rules.
- `POST /api/resume/jobs/disqualifiers` — Create exclusion keyword rule.
- `DELETE /api/resume/jobs/disqualifiers/{id}` — Delete exclusion rule.
