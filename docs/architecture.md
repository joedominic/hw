# Architecture

ResumeElite is a Django 5.2 monolith with a server-rendered HTML UI, Django Ninja JSON APIs, Huey background workers, and LLM/browser automation integrations.

## Runtime Stack

- Web framework: Django 5.2.
- API framework: Django Ninja under `/api/resume/`.
- Auth: Django sessions plus global login middleware.
- Background work: Huey with Redis.
- Database: SQLite in local/dev, with WAL and busy timeout configured for worker concurrency.
- AI orchestration: LangGraph and LangChain.
- Job search: JobSpy, Dice scraping, and Adzuna.
- Browser automation: Playwright and browser-use.
- UI: Django templates with Tailwind CDN and vanilla JavaScript `fetch()`.

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
- `agents.py` - LangGraph resume optimizer.
- `llm_gateway.py` and `llm_factory.py` - provider selection, invocation, limits, and usage tracking.
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

## Auth And Tenancy

`LoginRequiredMiddleware` protects the app by default. Public prefixes are limited to accounts, admin, static, and media paths. API routes return JSON `401` when unauthenticated; HTML routes redirect to login.

Most product models include an `owner` FK to `auth.User`. Code should fetch owned data through `objects.for_user(user)` or `get_owned_or_404()`. Shared global entities, especially `JobListing` and `JobDescription`, must not carry user-specific state directly.

During django-hijack impersonation, `request.user` is the target user. This intentionally makes normal owner-scoped queries show the impersonated user's data.

## External Integrations

- LLM providers: OpenAI, Anthropic, Groq, Google Gemini, Ollama local/cloud, and OpenRouter.
- Job sources: JobSpy, Dice, and Adzuna.
- Apply automation: Playwright deterministic adapters plus browser-use generic fallback.
- Redis: Huey queue plus provider-level LLM rate limiting.
- Media files: resume uploads, generated documents, and apply-agent screenshots.

## Architecture Notes For Agents

- Prefer existing Django view/API/task boundaries over new parallel mechanisms.
- Keep long-running work in Huey and persist progress in models.
- Preserve owner scoping when touching user data.
- Treat `JobListing` as a shared catalog; put user-specific state in related owned models.
- Do not add new LLM invocation paths that bypass `llm_gateway.py`.
- Do not add browser automation that bypasses the apply-agent orchestration state machine.
