# API And Route Reference

This document catalogs the complete route surface for HireEdge, registered across `django_project/core/urls.py`, `resume_app/api.py`, and `resume_app/jobs_api.py`.

---

## 1. Authentication & Security Policies

- **Global Protection:** `LoginRequiredMiddleware` protects the site. Unauthenticated HTML requests redirect to `/accounts/login/`; unauthenticated `/api/*` requests return JSON `401`.
- **Public Exceptions:** `/` (landing), `/accounts/*` (auth flows), `/admin/`, `/static/`, `/legal/privacy/`, and `/legal/terms/`.
- **API Authentication:** Supports Django session auth or Bearer token auth (`Authorization: Bearer <token>`) via `CustomerApiKey` (`SessionOrApiKeyAuth`).
- **Media Authorization:** `/media/<path>` requests are routed through `serve_media_view`, which validates that the requesting user owns the requested asset.

---

## 2. Server-Rendered HTML & Subsystem Routes

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

### 2.2 Tab 1: Target & Discover
- `GET /jobs/profiler/` — **AI Resume Profiler** view; parses candidate resume, presents career analysis, and scaffolds search profiles.
- `POST /jobs/profiler/api/analyze/` — Analyzes uploaded resume PDF; saves extraction to `CareerProfileAnalysis(analysis_data)`.
- `POST /jobs/profiler/api/provision/` — Creates or updates `SearchProfile` records and associates the source `UserResume`.
- `POST /jobs/profiler/api/preview-matches/` — Runs live multi-board preview query across job boards for target titles without writing to the database.
- `GET/POST /jobs/search/` — **Search Sandbox (Unbiased)** view; query live jobs across Indeed, LinkedIn, Greenhouse, BuiltIn, Levels.fyi, and Dice with 1-click `+ Add to Review` bridge.
- `GET /jobs/tracks/` — Search profile management and resume assignment.
- `POST /jobs/tracks/<slug>/delete/` — Delete a search profile.

---

### 2.3 Tab 2: Career Cockpit & Job Automation
- `GET /jobs/cockpit/` — **Career Cockpit Tri-Pane Workstation**; primary triage hub for search profiles, 4-stage pipeline, and off-canvas slide-over Automation Drawer (`openAutomationDrawer()`).
- `GET /jobs/cockpit/api/skill-radar/` — Skill frequency radar JSON for the active search profile.
- `POST /jobs/tasks/<task_id>/run/` — Trigger manual search run immediately (sets atomic pending key + 3m cooldown).
- `GET /jobs/tasks/<task_id>/status/` — JSON status endpoint polled by browser during active search.
- `POST /jobs/tasks/<task_id>/toggle/` — Toggle task active/inactive status.
- `GET/POST /jobs/tasks/new/` — Create a scheduled search task.
- `GET/POST /jobs/tasks/<task_id>/edit/` — Edit an existing search task.
- `GET /jobs/tracks/scheduled-runs/<run_id>/details-json/` — Disposition breakdown JSON for the Cockpit inspection modal.
- `GET /jobs/tracks/scheduled-runs/<run_id>/download-csv/` — Download run job dispositions as CSV.
- `GET /jobs/automation/` — **Decommissioned legacy route; permanently redirects to `/jobs/cockpit/`**.

---

### 2.4 Tab 3: Tracker & Momentum Analytics
- `GET /performance/` — **Tracker: Career Momentum & Application Outcomes**; active streak tracking, weekly target progress bar, and interview loop management.
- `GET /performance/api/metrics/` — Progressive hydration JSON endpoint for metrics (accepts `?refresh=1`).
- `POST /performance/settings/` — Update performance dashboard tracking settings and weekly target goals.
- `GET /system/fit-inspector/` — Diagnostics for semantic embeddings, BM25 keywords, and recruiter scoring.

---

### 2.5 Kanban Stages & Legacy Pipeline Routes
- `GET/POST /jobs/pipeline/` — Pipeline Kanban board (Stage: New).
- `GET/POST /jobs/vetting/` — Vetting Kanban board (Stage: Review).
- `GET/POST /jobs/applying/` — Applying Kanban board (Stage: Tailoring).
- `GET/POST /jobs/done/` — Done Kanban board (Stage: Applied / Completed).
- `GET /jobs/<job_listing_id>/focus-breakdown/` — Staff-only focus score diagnostics.
- `GET /jobs/<job_listing_id>/focus-breakdown/<liked_job_id>/` — Staff-only focus alignment analysis.
- `GET/POST /jobs/vetting/match-debug/<job_listing_id>/` — Single-job resume match debugging.

---

### 2.6 Resume Optimizer & Prompt Studio
- `GET/POST /resume/optimizer/` — Main AI resume optimizer interface with decoupled workflows: Save Draft (`action="save_draft"`, `Ctrl+S`, stays in `Applying`), I Submitted This (`action="approve_and_mark_applied"`, `Ctrl+Enter`, moves to `Applied`), 1-click external Employer Post, and dynamic "Back to Cockpit" (`Esc`) return loop.
- `GET /resume/status/<resume_id>/` — Polling status endpoint for active optimization runs.
- `POST /resume/status/<resume_id>/draft/` — Save edited resume text draft.
- `GET /resume/optimizer/context/<resume_id>/` — Inspect `optimizer_context_snapshot` token budgets.
- `GET/POST /resume/prompts/` — **Prompt Studio & Library Workstation**; comprehensive prompt IDE for inspecting, versioning, routing, and testing all 14 system/user prompt definitions across the platform (`writer`, `ats_judge`, `recruiter_judge`, `matching`, `insights`, `jd_cleanse`, `job_authenticity`, `fit_check`, `cover_letter`, `interview_prep`, `skill_radar`, `career_profiler`, `pipeline_skill_extract`, `pipeline_resume_refine`) and custom `AtsJudgeProfile` entries. Designed with a 3-pane responsive layout that adapts to vertical/portrait displays (left navigation rail, stacked editor, and in-authoring test bench) and mobile viewport drawers (`< lg`).
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
- `GET/POST /workspace/workflows/<id>/edit/` — Edit optimizer workflow (staff).
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
- `POST /api/resume/llm/complete` — Generic LLM completion (requires `api_access` plan permission).
- `POST /api/resume/llm/connect` — Validate and store encrypted provider API keys.
- `GET /api/resume/llm/models` — Retrieve available models for a provider.
- `POST /api/resume/llm/set-default-model` — Set default provider/model preference.
- `GET /api/resume/export/{resume_id}/pdf` — Export tailored resume as PDF.
- `GET /api/resume/export/{resume_id}/docx` — Export tailored resume as Word document.

---

## 4. Django Ninja REST API: `/api/resume/jobs/`

Mounted in `resume_app/jobs_api.py`.

### 4.1 Jobs, Pipeline & Feedback Actions (With Exact Side Effects)

- `GET /api/resume/jobs/pipeline` — Retrieve user's pipeline jobs for active stage/profile.
- `POST /api/resume/jobs/pipeline/delete` — Soft-delete pipeline entry (`PipelineEntry.delete()`).
- `POST /api/resume/jobs/search` — Execute multi-board search query across external sources without database writes.
- `POST /api/resume/jobs/ai-match` — Run batch LLM fit evaluations and return candidate match probabilities.
- `POST /api/resume/jobs/insights` — Extract aggregated skill insights across multiple jobs.
- `GET /api/resume/jobs/{job_listing_id}` — Retrieve global job listing details.
- `POST /api/resume/jobs/fetch-description` — Fetch and enrich full job description from source URL.
- `POST /api/resume/jobs/{job_listing_id}/save?track={slug}` — **Sandbox to Cockpit Bridge**:
  - *Side Effect:* Creates or updates `PipelineEntry(owner, job, track, stage='vetting', is_saved=True, saved_at=now())` and writes `JobListingAction(action_type='save')`. Binds the opportunity directly into the active `SearchProfile` slug.
- `POST /api/resume/jobs/{job_listing_id}/unsave` — **Unsave Job**:
  - *Side Effect:* Deletes the corresponding `PipelineEntry` and deletes `JobListingAction(action_type='save')`.
- `POST /api/resume/jobs/{job_listing_id}/like` — **Like Job**:
  - *Side Effect:* Creates `JobListingAction(action_type='like')`, removes any opposing dislike action, and triggers recalculation of the tenant's preference centroid (`JobListingTrackMetrics.centroid_embedding`).
- `POST /api/resume/jobs/{job_listing_id}/unlike` — **Unlike Job**:
  - *Side Effect:* Deletes `JobListingAction(action_type='like')` and recalculates the preference centroid vector.
- `POST /api/resume/jobs/{job_listing_id}/dislike` — **Dislike Job**:
  - *Side Effect:* Creates `JobListingAction(action_type='dislike')`, deletes any active `PipelineEntry`, and recalculates the preference centroid to deprioritize similar roles.
- `POST /api/resume/jobs/{job_listing_id}/hide` — **Hide Job**:
  - *Side Effect:* Creates `JobListingAction(action_type='hide')` to suppress card from search results without modifying the preference centroid vector.
- `POST /api/resume/jobs/{job_listing_id}/unhide` — **Unhide Job**:
  - *Side Effect:* Deletes `JobListingAction(action_type='hide')` to restore card visibility in search results.
- `POST /api/resume/jobs/{job_listing_id}/mark-applied` — **Mark Applied**:
  - *Side Effect:* Updates `PipelineEntry.stage = 'done'`, sets `is_applied = True`, `applied_at = now()`, and creates `JobListingAction(action_type='applied')`.
- `GET /api/resume/jobs/pipeline-entry/{id}/interview-prep` — Retrieve saved interview preparation questions from `PipelineEntry`.
- `POST /api/resume/jobs/pipeline-entry/{id}/generate-interview-prep` — Generate customized interview preparation using LLM; writes to `PipelineEntry.interview_prep_questions`.
- `POST /api/resume/jobs/pipeline-entry/{id}/save-interview-prep` — Save user-edited interview preparation questions to `PipelineEntry`.
- `GET /api/resume/jobs/disqualifiers` — List user exclusion keyword rules from `UserDisqualifier`.
- `POST /api/resume/jobs/disqualifiers` — Create exclusion keyword rule in `UserDisqualifier`.
- `DELETE /api/resume/jobs/disqualifiers/{id}` — Delete exclusion rule from `UserDisqualifier`.
- `GET /api/resume/jobs/{job_listing_id}/authenticity` — Retrieve cached or persisted labor compliance & posting authenticity audit for a job listing (`authenticity_verdict`, `authenticity_score`, `authenticity_confidence`, `authenticity_rationale`). For confidentiality and IP protection, the detailed 10-point audit checklist is sanitized for non-staff callers while available to staff.
- `POST /api/resume/jobs/{job_listing_id}/audit-authenticity` — Trigger on-demand 10-point labor compliance and authenticity audit (statutory DOL ETA-9089 PERM phrasing, phantom pipelines, short lifespan $\le 4$ calendar days delta, statutory minimum compliance windows, generic JFA boilerplate, internal promotions, contractor conversions, hyper-specific OKRs, degree equations, flat salary bands, and flexible skill requirements); persists verdict, score, confidence, rationale, and audit trail to `JobListing`.
