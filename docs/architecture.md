# Architecture

ResumeElite is a Django 5.2 monolith with a server-rendered HTML UI, Django Ninja JSON APIs, Huey background workers, and LLM/browser automation integrations.

## Runtime Stack

- Web framework: Django 5.2.
- API framework: Django Ninja under `/api/resume/`.
- Auth: Django sessions plus global login middleware.
- Background work: Huey with Redis.
- Database: SQLite by default for local/tests; MySQL/MariaDB when `MYSQL_DATABASE` is set (PyMySQL).
- AI orchestration: LangGraph and LangChain.
- Job search: JobSpy, Dice JSON search API (HTML fallback), Levels.fyi encrypted jobs API, and Adzuna.
- Browser automation: Playwright and browser-use.
- UI: Django templates with Tailwind CDN and vanilla JavaScript `fetch()`. Shared brand tokens live in `resume_app/templates/resume_app/_tailwind_theme.html` (forest green primary, navy ink, mint accents, Plus Jakarta Sans) and are included by the app shell, public auth base, and landing page.

## Project Layout

- `django_project/manage.py` - Django command entry point.
- `django_project/core/settings.py` - environment, installed apps, middleware, Huey, LLM, and media settings.
- `django_project/core/urls.py` - central URL routing and Ninja API registration.
- `django_project/resume_app/` - main product app.
- `django_project/resume_app/templates/resume_app/` - server-rendered UI templates.
- `django_project/resume_app/apply_agent/` - apply-agent orchestration, browser code, and ATS adapters.
- `django_project/resume_app/docs/` - older feature-specific internal notes.
- `docs/` - root knowledge base for humans and agents.

## Main Modules

- `models.py` - domain model definitions.
- `views.py` - optimizer, settings, job search, tracks, automation, and misc HTML views.
- `pipeline_board.py` - pipeline board pages and stage actions.
- `apply_views.py` - apply-agent HTML dashboard, review, and profile pages.
- `api.py` - core resume optimizer and LLM JSON endpoints.
- `jobs_api.py` - job search, pipeline, match, prep, and job-feedback JSON endpoints.
- `apply_api.py` - apply-agent JSON endpoints.
- `tasks.py` - Huey tasks and periodic managers.
- `agents.py` - LangGraph resume optimizer (Writer, ATS judge, Recruiter judge nodes).
- `optimizer_budget.py` - optimizer context assembly: role-focused JD slice, Writer dedupe, judge input caps.
- `llm_gateway.py`, `llm_policy.py`, and `llm_factory.py` - provider selection, invocation, token budgets, concurrency/timeouts, and usage tracking. Detail: `resume_app/docs/LLM_GATEWAY.md`.
- `tenancy.py` - owner-scoped query helpers.
- `middleware.py` - global login enforcement.
- `onboarding.py` - per-user default seeding.

## Request Flow

Browser requests follow two paths:

1. HTML pages are handled by Django views and rendered from templates.
2. Interactive UI actions call `/api/resume/*` Ninja endpoints or legacy JSON views.

Long-running work is pushed into Huey. UI pages usually create a durable DB record, enqueue a task, then poll a status endpoint until completion.

## Background Task Flow

Huey workers consume Redis-backed tasks. Durable state is stored in Django models so work can be resumed or inspected after process restarts.

Key task families:

- `optimize_resume_task` writes `OptimizedResume` and `AgentLog`.
- `run_job_search_task` writes `JobListing`, `PipelineEntry`, and `JobSearchTaskRun`.
- `evaluate_vetting_matching_task` updates vetting scores on `PipelineEntry`.
- `pipeline_manager` refreshes metrics, prunes weak fits, and promotes candidates.
- `cleanup_manager` purges stale pipeline and generated-resume data.
- `run_apply_agent_step` and `apply_agent_heartbeat` drive application attempts.

## Resume Optimizer

Default graph: **Writer → ATS judge → Recruiter judge** (single pass, `agents.create_workflow`).

- **Context:** `optimizer_budget.build_optimizer_context_state()` produces `writer_job_description` (role-focused JD via `embeddings.extract_role_description`), `judge_job_description`, truncated resume fields, and `optimizer_context_budget` char counts persisted on `OptimizedResume.optimizer_context_snapshot`.
- **Writer / Judges / optimizer JD-cleanse step:** Cloud-only (`allow_local=False`). **Ollama Local is never used inside the resume optimizer graph**; missing cloud LLM fails the run with a clear error.
- **Pipeline / vetting / other product LLM:** Prefer or require **Ollama Local** (JD cleanse + vetting match are local-only).
- **Detail:** `django_project/resume_app/docs/OPTIMIZER_PAGE.md`.

## Auth And Tenancy

`LoginRequiredMiddleware` protects the app by default. Public prefixes are limited to accounts, admin, and static paths. API routes return JSON `401` when unauthenticated; HTML routes redirect to login. When `REQUIRE_EMAIL_VERIFICATION` is enabled, unverified users are redirected to Settings → Account (API returns `403`).

Most product models include an `owner` FK to `auth.User`. Code should fetch owned data through `objects.for_user(user)` or `get_owned_or_404()`. Shared global entities, especially `JobListing` and `JobDescription`, must not carry user-specific state directly.

Media under `/media/` is authenticated **and owner-authorized** via `resume_app.media_access` (resume paths, apply-agent attempt paths, and pipeline extract runs with `owner_id` in meta). LLM provider key resolution and rate-limit preference/cooldown buckets are owner-scoped (BYOK) or platform-scoped (env keys).

Plans and quotas live in `Plan` / `Subscription` / `UsageCounter` (`entitlements.py`). Storage is hard-enforced via `storage_quota.py` against `Plan.storage_mb` (resumes + apply-agent media + owned pipeline extract runs). Stripe Checkout/Portal/webhooks are in `billing.py` + `/billing/` (`invoice.payment_failed` → past_due Free entitlements). Customer API keys (`CustomerApiKey`) authenticate Ninja via `Authorization: Bearer`. Abuse throttling is in `AbuseThrottleMiddleware` (Redis Django cache in multi-worker prod). Optional S3 media via `AWS_STORAGE_BUCKET_NAME` + django-storages; serve/delete/quota go through `default_storage`. Fernet supports `FERNET_KEYS` rotation. Public legal pages at `/legal/privacy/` and `/legal/terms/`.

Account lifecycle lives in `resume_app.account` / `account_views`: signup with unique email (app checks + DB unique index on non-blank `auth_user.email`), verification tokens, password reset, export, and deletion with media cleanup.

During django-hijack impersonation, `request.user` is the target user. This intentionally makes normal owner-scoped queries show the impersonated user's data.

## External Integrations

- LLM providers: OpenAI, Anthropic, Groq, Google Gemini, Ollama local/cloud, and OpenRouter.
- Job sources: JobSpy, Dice (JSON API + HTML fallback), Levels.fyi (encrypted jobs API), and Adzuna.
- Apply automation: Playwright deterministic adapters plus browser-use generic fallback.
- Redis: Huey queue, provider RPM/TPM, LLM concurrency, platform token burn counters, and job-pin keys.
- Media files: resume uploads, generated documents, and apply-agent screenshots.

## LLM Gateway (summary)

Product LangChain calls go through `invoke_llm_messages` (selection, pin, failover, quotas, usage). Browser-use uses `llm_policy.wrap_browser_use_llm`. Daily **request** and **token** quotas are plan-scoped; platform/env keys also hit a shared daily token ceiling. Settings → Usage shows today’s limits plus lifetime analytics. Full reference: [`resume_app/docs/LLM_GATEWAY.md`](../django_project/resume_app/docs/LLM_GATEWAY.md).

## Architecture Notes For Agents

- Prefer existing Django view/API/task boundaries over new parallel mechanisms.
- Keep long-running work in Huey and persist progress in models.
- Preserve owner scoping when touching user data.
- Treat `JobListing` as a shared catalog; put user-specific state in related owned models.
- Do not add new LLM invocation paths that bypass `llm_gateway.py` (or `llm_policy` for non-LangChain callers such as browser-use). See `LLM_GATEWAY.md`.
- Do not add browser automation that bypasses the apply-agent orchestration state machine.
