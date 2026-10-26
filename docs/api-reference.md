# API And Route Reference

Routes are registered in `django_project/core/urls.py`. The site uses server-rendered Django views for pages and Django Ninja JSON endpoints under `/api/resume/`.

## Authentication Behavior

The app uses Django session authentication. `LoginRequiredMiddleware` protects the site by default.

- Unauthenticated HTML requests redirect to `/accounts/login/`.
- Unauthenticated `/api/*` requests return JSON `401`.
- Public routes include `/` (landing), `/accounts/*`, `/admin/`, and `/static/`. `/media/` requires auth and owner authorization.
- Data access should remain owner-scoped unless a model is intentionally global.

## HTML Routes

### Accounts And Landing

- `GET /` - public landing page; redirects authenticated users into the workspace.
- `GET/POST /accounts/login/` - login.
- `POST /accounts/logout/` - logout.
- `GET/POST /accounts/signup/` - signup when `SIGNUP_ENABLED` is true; seeds default user data and sends verification email.
- `GET/POST /accounts/password-reset/` (+ done / confirm / complete) - password reset.
- `GET /billing/` - plan usage and Stripe checkout/portal.
- `POST /billing/stripe/webhook/` - Stripe webhooks (signature required when `DEBUG=False`; unsigned JSON allowed only in DEBUG).
- `GET /legal/privacy/`, `GET /legal/terms/` - public legal pages.
- `GET /accounts/verify-email/<uidb64>/<token>/` - confirm account email.
- `POST /accounts/resend-verification/` - resend verification email (authenticated).

### Resume And LLM

- `GET/POST /resume/optimizer/` - optimizer home.
- `GET /resume/status/<resume_id>/` - optimization status JSON for the HTML UI.
- `POST /resume/status/<resume_id>/draft/` - save edited optimized text.
- `GET /resume/optimizer/context/<resume_id>/` - debug writer context (`optimizer_context_snapshot`: JD/resume char budgets from last run).
- `GET/POST /settings/` - LLM keys, model preferences, usage, and automation settings.
- `GET/POST /resume/prompts/` - staff-only system Prompt Library and global ATS judge profiles.
- `GET/POST /resume/llm-test/` - developer LLM test page when enabled.

### Workflows

- `GET /workspace/workflows/` - list system-wide workflows (staff).
- `GET/POST /workspace/workflows/new/` - create system-wide workflow (staff).
- `GET/POST /workspace/workflows/<id>/edit/` - edit system-wide workflow (staff).
- `POST /workspace/workflows/<id>/delete/` - delete system-wide workflow (staff).

### Jobs And Pipeline

- `GET/POST /jobs/search/` - search external jobs and record feedback.
- `GET /jobs/<job_listing_id>/focus-breakdown/` - staff-only focus score debug.
- `GET /jobs/<job_listing_id>/focus-breakdown/<liked_job_id>/` - staff-only focus alignment debug.
- `GET/POST /jobs/pipeline/` - pipeline stage board.
- `GET/POST /jobs/vetting/` - vetting stage board.
- `GET/POST /jobs/applying/` - applying stage board.
- `GET/POST /jobs/done/` - done stage board.
- `GET/POST /jobs/vetting/match-debug/<job_listing_id>/` - one-job matching debug.

### Apply Agent

- `GET/POST /jobs/apply-agent/` - dashboard and start actions.
- `GET/POST /jobs/apply-agent/<attempt_id>/` - review, approve, reject, or override URL.
- `GET/POST /jobs/apply-agent/profile/` - applicant profile and apply-agent LLM settings.

### Tracks, Tasks, And Huey

- `GET/POST /jobs/tracks/` - manage search profiles and resume library (available in normal and power modes).
- `POST /jobs/tracks/<slug>/delete/` - delete a search profile.
- `GET /jobs/automation/` - scheduled search tasks.
- `GET/POST /jobs/tasks/new/` - create scheduled search task.
- `GET/POST /jobs/tasks/<id>/edit/` - edit task.
- `POST /jobs/tasks/<id>/run/` - enqueue task immediately.
- `POST /jobs/tasks/<id>/toggle/` - activate or deactivate task.
- `GET /jobs/huey/` - Huey monitor.
- `POST /jobs/huey/periodic/<task_name>/revoke/` - pause periodic task.
- `POST /jobs/huey/periodic/<task_name>/restore/` - restore periodic task.
- `POST /jobs/huey/flush-queue/` - flush pending queue.
- `POST /jobs/huey/run-cleanup/` - run cleanup.
- `POST /jobs/huey/task/<task_name>/run/` - run known Huey task.

### Staff

- `GET /staff/users/` - staff user search and hijack controls.
- `/hijack/` - django-hijack acquire/release routes.
- `/admin/` - Django admin.

## JSON API: `/api/resume/`

Defined mainly in `resume_app/api.py`.

- `POST /api/resume/llm/complete` - generic LLM completion (requires plan `api_access`; input capped by `LLM_COMPLETE_MAX_INPUT_CHARS`).
- `POST /api/resume/run-step` - run one optimizer step (`writer`, `ats_judge`, `recruiter_judge`, or `jd_cleanse`).
- `POST /api/resume/fit-check` - score resume fit against a JD.
- `GET /api/resume/prompts` - default prompt templates.
- `POST /api/resume/optimize` - upload PDF/JD and enqueue optimization.
- `GET /api/resume/status/{resume_id}` - owner-scoped optimization status.
- `POST /api/resume/status/{resume_id}/draft` - save draft.
- `POST /api/resume/status/{resume_id}/generate-cover-letter` - generate cover letter.
- `POST /api/resume/status/{resume_id}/save-cover-letter` - save edited cover letter.
- `POST /api/resume/status/{resume_id}/cancel` - cancel queued/running optimization.
- `GET /api/resume/ats-judge-profiles[...]` - list/read system ATS profiles; `POST/PUT/DELETE` are staff-only.
- `GET/POST/PUT/DELETE /api/resume/workflows[...]` - list/get for all users; create/update/delete staff-only (system-wide).
- `POST /api/resume/llm/connect` - validate and store provider key.
- `GET /api/resume/llm/models` - list models for a provider.
- `POST /api/resume/llm/set-default-model` - set default model.
- `GET /api/resume/export/{resume_id}/pdf` - export PDF.
- `GET /api/resume/export/{resume_id}/docx` - export DOCX.

## JSON API: `/api/resume/jobs/`

Defined in `resume_app/jobs_api.py`.

- `GET /api/resume/jobs/resumes` - library resumes.
- `GET /api/resume/jobs/pipeline` - owner pipeline jobs.
- `POST /api/resume/jobs/pipeline/delete` - soft-delete pipeline entry.
- `POST /api/resume/jobs/search` - external job search.
- `POST /api/resume/jobs/ai-match` - LLM match batch.
- `POST /api/resume/jobs/insights` - multi-job insights.
- `GET /api/resume/jobs/pipeline-entry/{id}/interview-prep` - fetch prep.
- `POST /api/resume/jobs/pipeline-entry/{id}/generate-interview-prep` - generate prep.
- `POST /api/resume/jobs/pipeline-entry/{id}/save-interview-prep` - save prep.
- `POST /api/resume/jobs/pipeline-resume-summary/start` - start batch skill extraction.
- `GET /api/resume/jobs/pipeline-resume-summary/status` - poll extraction.
- `POST /api/resume/jobs/pipeline-resume-summary/stop` - stop extraction.
- `GET /api/resume/jobs/matches` - saved match results.
- `POST /api/resume/jobs/run-keyword-search` - keyword search and fit checks.
- `GET /api/resume/jobs/saved` - saved jobs.
- `GET /api/resume/jobs/disliked` - disliked jobs.
- `GET/POST/DELETE /api/resume/jobs/disqualifiers[...]` - disqualifier CRUD.
- `GET /api/resume/jobs/focus-breakdown/{job_listing_id}` - staff-only focus debug.
- `GET /api/resume/jobs/{job_listing_id}` - global job detail (Dice listings with short summaries are enriched from the job-detail page).
- `POST /api/resume/jobs/fetch-description` - Auto-fill full job description from a Dice job-detail URL.
- `POST /api/resume/jobs/{job_listing_id}/match` - fit check and save result.
- `POST /api/resume/jobs/{job_listing_id}/mark-applied` - mark applied.
- `POST /api/resume/jobs/{job_listing_id}/like` - like job (preference signal; Find jobs UI toggles in place).
- `POST /api/resume/jobs/{job_listing_id}/unlike` - clear like for this job/profile.
- `POST /api/resume/jobs/{job_listing_id}/dislike` - dislike job (exclude + FIT preference; Find jobs removes the card).
- `POST /api/resume/jobs/{job_listing_id}/hide` - hide from search only (no FIT impact).
- `POST /api/resume/jobs/{job_listing_id}/unhide` - restore a hidden/disliked job to search.
- `POST /api/resume/jobs/{job_listing_id}/save` - save job to favourites and ensure a My jobs pipeline entry (new saves land in Review/`vetting`; existing active stages are left unchanged). Find jobs UI uses AJAX; no page reload.
- `POST /api/resume/jobs/{job_listing_id}/unsave` - unsave job and soft-delete its pipeline entry for the active profile.

## JSON API: `/api/resume/apply/`

Defined in `resume_app/apply_api.py`.

- `POST /api/resume/apply/start` - create attempts for Applying-stage pipeline entries.
- `GET /api/resume/apply/{attempt_id}` - attempt status and steps.
- `POST /api/resume/apply/{attempt_id}/approve` - approve attempt.
- `POST /api/resume/apply/{attempt_id}/reject` - reject attempt.
- `POST /api/resume/apply/{attempt_id}/override-url` - set URL and rerun.

## API Implementation Notes

- Session auth is the primary API auth mechanism.
- Owner-scoped endpoints should use `api_user(request)` and `get_owned_or_404()`.
- `JobListing` detail is global; user-specific decisions belong in related owned models.
- Apply-agent attempts should be scoped through `pipeline_entry__owner`.
- For new long-running API actions, create durable state first, enqueue Huey work, then expose a pollable status route.
