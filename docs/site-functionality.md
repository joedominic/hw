# Site Functionality

ResumeElite is a multi-user job-search workspace. It combines resume optimization, job discovery, pipeline management, LLM-assisted preparation, and semi-automated application submission.

## Landing Page

Entry point: `/`

Public marketing page with sign-up and sign-in links. Uses the shared ResumeElite brand theme (forest green primary, navy headings, mint accents, Plus Jakarta Sans). Hero uses a full-bleed product workspace image with brand, headline, supporting line, and CTAs. Further sections: feature cards, how-it-works process strip, and final CTA. Authenticated users are redirected to Getting Started (normal users with incomplete onboarding) or the pipeline board.

### Job Search (Find jobs)

Entry point: `/jobs/search/`

CareerFlow-inspired layout: mint setup banner, search hero with Advanced Filters, recommended results with match badges, and a right sidebar (My Filters, Growth Pulse, Pro tip). Core actions (like / dislike / hide / save / tailor) are unchanged. **Why this fit?** (focus-score breakdown) is staff-only so scoring internals stay off the end-user UI. Search results are de-duplicated by normalized title + company (multi-location postings of the same role collapse to the top-ranked copy).

## Getting Started (Onboarding)

Entry point: `/getting-started/`

Normal-mode users see a checklist after sign-up or login until they connect AI (when required), upload a resume, and run a first job search—or choose **Skip for now**. Power-mode users skip this page.

Experience mode (`normal` vs `power`) controls nav complexity and optimizer defaults — not plan quotas. **Normal users always see Simple** (`normal`); there is no Simple/Advanced toggle for them. Staff may enable Advanced in **Settings → General**, or via the `set_experience_mode` management command. **Search profiles** (resume↔profile assignment) stays in the Jobs nav for all users.

## Primary User Areas

### Resume Optimizer

Entry points: `/resume/optimizer/`

**UI:** Input step uses a CareerFlow-inspired layout — upload + job description cards, collapsible supporting context, configuration card (workflow / ATS / run mode), and a primary **Run optimizer** CTA. Recent library resumes appear under the upload dropzone for one-click selection.

Users upload a resume PDF and paste a job description. The app creates an `OptimizedResume`, enqueues a Huey task, and runs a LangGraph pipeline:

1. **Writer** revises the resume for the target job (strong/remote model).
2. **ATS judge** scores keyword and applicant-tracking fit (prefers local Ollama when configured).
3. **Recruiter judge** scores human readability and role fit (same hybrid routing as ATS).

The default graph is a **single pass** (Writer → ATS → Recruiter → END). Custom workflow step lists run each listed step in order and may include **JD Cleanse** (uses the system JD Cleanse prompt to shrink the posting, then overwrites the job description for every later step). The UI still exposes `max_iterations` / score thresholds for API compatibility, but the compiled graph does not loop back to Writer automatically today.

**Token budgets:** Context is built in `optimizer_budget.py` — role-focused JD excerpt (semantic cleanse, not prefix truncate), deduplicated Writer prompts (skip duplicate source resume / full JD when redundant), and capped judge inputs. See `django_project/resume_app/docs/OPTIMIZER_PAGE.md` for full detail.

The UI polls status, shows agent logs, and supports editing the generated draft. **Export PDF/Word** auto-saves the current editor text first, then downloads (cache-busted). Users can also generate and save a cover letter for a completed optimization.

### Prompt Library And Workflows

Entry points: `/resume/prompts/` (staff) and `/workspace/workflows/` (staff)

Prompts are **system-wide**: one shared set managed by staff in the Prompt Library (backed by `SystemPromptProfile` and global `AtsJudgeProfile` rows). There is no per-user prompt customization. ATS judge profiles are admin-managed globals that users can select at run time. Optimizer workflows are also **system-wide** (`OptimizerWorkflow.owner` null): staff create/edit them under `/workspace/workflows/`; every subscriber sees them in the Optimizer dropdown. Stored score thresholds and iteration limits are retained for compatibility but do not re-enable an automatic Writer loop in the current graph.

### Job Search

Entry point: `/jobs/search/`

Users search external job boards, rank results, and take feedback actions. Search can use JobSpy, Dice, and Adzuna depending on settings and credentials. The app stores shared job listings globally while recording likes, dislikes, saves, matches, embeddings, and track metrics per user.

Important behaviors:

- Search results can be liked, disliked, hidden, saved, or marked applied. On Find jobs: **Like** improves FIT for similar roles; **Dislike** (thumbs down) excludes the job and feeds FIT/preference ranking; **Hide** excludes the job from future searches without affecting FIT; **Save** toggles a red heart and adds the job to **My jobs → Review** (and favourites) for the active search profile. **Unsave** removes the favourite and soft-deletes that pipeline entry. If the job is already on the board from a scheduled Huey search, Save leaves its stage unchanged. Like / dislike / hide / save use AJAX (no search re-run). Preference embeddings for like/dislike are written asynchronously. Submitting Search shows an in-progress overlay so the button cannot be clicked repeatedly while boards are queried.
- Disqualifier phrases filter jobs that should not enter the pipeline.
- Focus and preference scoring use embeddings and user feedback.
- AI match and insight flows compare jobs against selected resumes.

### Pipeline Boards

Entry points: `/jobs/pipeline/`, `/jobs/vetting/`, `/jobs/applying/`, `/jobs/done/`

The pipeline is a Kanban-style workflow backed by `PipelineEntry`.

- `pipeline`: new jobs from scheduled/Huey searches.
- `vetting`: jobs awaiting deeper resume/JD matching (includes Find-jobs saves and shortlisted roles).
- `applying`: jobs ready for application work.
- `done`: applied or completed jobs.

**Simple experience** (all normal users; sidebar: **My jobs**) uses a segmented stage control—**New**, **Review**, **Applying**, **Applied**—Find-jobs-style cards with a green accent bar and stage-specific primary actions (Shortlist, Ready to apply, Mark applied), plus **Like / Dislike** feedback (same preference signals as Find jobs) and a search/filter toolbar. Preference filters, conversion metrics, resume-summary LLM tools, and debug actions stay Advanced/staff-only on the board. **Advanced mode** (staff with power enabled; sidebar: **Dashboard**) keeps the full operator UI.

Normal users must **save at least one search** on Find jobs before My jobs unlocks; until then they see a setup prompt instead of the board.

Users can move, delete, bulk update, and enrich jobs. Background managers can evaluate fit, promote strong candidates, clean weak candidates, and generate interview preparation.

### Search Profiles And Resume Library

Entry point: `/jobs/tracks/` (sidebar: **Search profiles** for all experience modes)

Search profiles (internal model: `Track`) separate job-search contexts for pipeline scoring and resume association. New users are seeded with a single default **General** profile; power users can add more. Normal users use this page to upload PDFs and assign each resume to a profile (e.g. FinCrimes); Find jobs also links here from the sidebar. On Find jobs, **Resume for match** only affects scoring for that search run — permanent resume↔profile assignment lives here.

### Saved Job Searches

Entry point: `/jobs/search/` (Job search page)

Users can save manual search configurations (term, location, sites, profile, resume, min score, model) as named presets. Presets can be loaded, updated, deleted, or scheduled to run automatically. Selecting a saved-search pill keeps that search in context (banner + highlighted pill) even after you change criteria; **Save changes** updates that preset in place (including job sites). **Save as new** creates a separate preset. Saving shows a progress overlay and does **not** re-query job boards — the current result list is kept (via `from_save=1` + session snapshot). Select a saved-search pill to set **Daily / Weekdays / Weekly** plus a run time in the sidebar; the server maps that to cron. Custom cron remains on `/jobs/automation/`.

Saving a preset automatically creates a matching **search profile** (`Track`) named after the preset (e.g. saved search **FinCrimes** → profile slug `fincrimes`). Pipeline entries and fit scoring are scoped to that profile. On **My jobs**, normal users only see profiles that match their saved searches—not legacy IC/Management tracks from power mode.

### Scheduled Job Automation

Entry point: `/jobs/automation/`

Users create `JobSearchTask` records with search terms, location, sites, schedule, and search profile. Huey periodically finds due tasks, fetches jobs, filters and ranks them, creates pipeline entries, **persists Fit/Pref metrics immediately**, optionally auto-promotes strong Pref to Review, and records each `JobSearchTaskRun`. A periodic `pipeline_manager` task refreshes stale metrics and purges weak Pref for all search profiles (including SearchProfile-only slugs).

**Scoring on My jobs:** **Fit %** and **Pref %** come from embedding preference signals (likes/dislikes), not from LLM fit checks. **Interview %** on Review is a separate local-LLM resume↔JD estimate. Keyword/manual match **fit_score** (`JobMatchResult`) is a third path used on Find jobs match flows, not the scheduled pipeline ingest.

### Apply Agent

Entry points: `/jobs/apply-agent/`, `/jobs/apply-agent/<attempt_id>/`, `/jobs/apply-agent/profile/`

The apply agent automates parts of applying to jobs in the Applying stage. It stores applicant profile data, optional site credentials, and resumable `ApplicationAttempt` state.

Prerequisites: enable the agent on the profile page, set name + email, run a Huey worker for this app (`HUEY_NAME`), and use live URL resolution (`APPLY_USE_MOCK_RESOLVER=0`) for real Indeed/LinkedIn listings.

Flow:

1. Start attempt from an Applying pipeline entry (requires a complete applicant profile).
2. Generate or select an optimized resume (optimizer completion nudges waiting attempts).
3. Resolve the application URL and detect the ATS (live Playwright for aggregators; known ATS hosts short-circuit).
4. Fill a dry-run form using deterministic ATS adapters or generic browser-use automation (respects allowed-ATS list and generic-fallback toggle).
5. Wait for user approval when required.
6. Submit or mark failed/rejected.

Steps chain immediately after each advance so progress does not depend only on the 60s heartbeat. Known ATS adapters include Greenhouse, Lever, Ashby, iCIMS, and related flows. Unknown ATS flows require review and do not auto-submit.

### Settings

Entry point: `/settings/`

Settings cover account email/password (Account tab), LLM provider credentials, provider/model preferences, default models, usage (daily plan request/token quotas plus lifetime gateway totals by query/model), pipeline automation thresholds, apply-agent defaults, and LLM stop controls. API keys are stored encrypted per user. Gateway behavior: [`resume_app/docs/LLM_GATEWAY.md`](../django_project/resume_app/docs/LLM_GATEWAY.md).

**Account tab:** change email (with verification), change password, resend verification, customer API keys (Pro+), export account JSON, delete account (username + password confirmation). Password reset is available from the sign-in page (`/accounts/password-reset/`). New signups receive a verification email; existing accounts were marked verified by migration `0022`. Set `REQUIRE_EMAIL_VERIFICATION=1` to hard-gate the app until verified (Settings → Account remains reachable). Non-blank account emails are unique in the database (migration `0025`).

### Billing And Plans

Entry point: `/billing/`

Users see their plan, daily usage (LLM requests, LLM tokens, searches, apply runs), storage used vs `Plan.storage_mb`, and can start Stripe Checkout or the Customer Portal when `STRIPE_*` is configured. Staff can assign plans from `/staff/users/`. Default plans: Free, Pro, Unlimited (seeded by migration `0023`). Quotas are enforced when `SAAS_ENFORCE_QUOTAS=1` (disabled automatically during `manage.py test`). Daily LLM token budgets come from `LLM_DAILY_TOKEN_LIMIT_BY_PLAN` (not a Plan DB column). Storage uploads and apply-agent media writes are blocked when the budget would be exceeded. `past_due` / `unpaid` subscriptions fall back to Free entitlements until payment recovers. Staff/superuser quota bypass requires `SAAS_STAFF_BYPASS_QUOTAS=1` (off by default). Session job-search cache hits still consume search quota. Media serve/delete/quota use Django’s default storage (local or S3). Public legal pages: `/legal/privacy/`, `/legal/terms/`.

### Staff Impersonation

Entry point: `/staff/users/`

Users with `resume_app.can_impersonate_users` can open **Staff** → `/staff/users/` to search accounts, see plan, daily quotas, storage used, lifetime LLM token totals, suspend/activate, assign plans, and impersonate. Django Admin (`/admin/`) also exposes `LLMAppUsageTotals`, `UsageCounter`, and `Subscription` for deeper inspection.

## Background Work

Huey powers long-running and scheduled work:

- Resume optimization.
- Job search task runs.
- Vetting match evaluation.
- Pipeline metric refresh and promotion.
- Generated-resume cleanup.
- Apply-agent heartbeats and browser steps.

Full functionality requires both the Django web process and a Huey worker unless `HUEY_IMMEDIATE=1` is used for local single-process testing.
