# Operations

This document covers local setup, environment variables, background workers, tests, and known operational gotchas.

## Local Setup

Use the repository virtual environment. Do not run Django commands with a system Python interpreter.

From the repo root:

```powershell
.\.venv\Scripts\python.exe -c "import sys; print(sys.prefix)"
.\.venv\Scripts\pip.exe install -r requirements.txt
```

From `django_project/`:

```powershell
..\.venv\Scripts\python.exe manage.py migrate
..\.venv\Scripts\python.exe manage.py runserver
```

Full functionality requires a second process:

```powershell
cd D:\Workshop\JobApp-Main\django_project
..\.venv\Scripts\python.exe manage.py run_huey
```

For single-process local testing, set `HUEY_IMMEDIATE=1`. This is useful for development but does not run the periodic scheduler like a real Huey worker.

## Common URLs

- `http://127.0.0.1:8000/` - optimizer.
- `http://127.0.0.1:8000/accounts/login/` - login.
- `http://127.0.0.1:8000/api/docs` - OpenAPI docs in debug mode.
- `http://127.0.0.1:8000/jobs/search/` - job search.
- `http://127.0.0.1:8000/jobs/huey/` - Huey dashboard.
- `http://127.0.0.1:8000/jobs/apply-agent/` - apply-agent dashboard.

## Environment

The app reads `.env` from the repo root by default. Use `.env.example` as the template.

Core variables:

- `SECRET_KEY` - Django signing and encryption key base. Rotating it can invalidate encrypted LLM keys.
- `DEBUG` - local debug mode.
- `ALLOWED_HOSTS` - host allowlist.
- `SIGNUP_ENABLED` - enables or disables public signup.
- `HUEY_REDIS_HOST`, `HUEY_REDIS_PORT`, `HUEY_REDIS_DB` - Redis connection for Huey.
- `HUEY_IMMEDIATE` - run tasks in-process for local testing.
- `LLM_USER_DAILY_REQUEST_LIMIT` - per-user LLM request cap; `0` means unlimited.
- `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `ADZUNA_COUNTRY` - Adzuna job source.
- `APPLY_USE_MOCK_RESOLVER` - use mock apply URL resolution in dev.
- `APPLY_BROWSER_HEADLESS` - visible or headless browser automation.
- `LLM_RATE_LIMIT_*` - Redis-backed provider RPM/TPM limits.

LLM provider keys can be stored per user through Settings. Environment fallbacks include `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, and `GOOGLE_API_KEY`.

## Dependencies

Important packages include:

- Django, django-ninja, django-environ, django-hijack.
- Huey, Redis, croniter.
- LangGraph, LangChain, provider SDKs, tiktoken.
- sentence-transformers, numpy, rank-bm25.
- python-jobspy and requests.
- pdfplumber, reportlab, python-docx, Pillow.
- browser-use, Playwright, psutil.

Apply-agent browser automation requires Playwright browsers:

```powershell
.\.venv\Scripts\playwright.exe install chromium
```

Embedding flows may require a compatible CPU `torch` install if not already present in the environment.

## Background Workers

Run one web process and one Huey worker for normal development.

Periodic tasks include:

- `enqueue_due_job_search_tasks` - every minute.
- `apply_agent_heartbeat` - every minute.
- `mark_stale_job_search_runs_failed` - every 15 minutes.
- `enqueue_due_vetting_matching_tasks` - every 20 minutes.
- `pipeline_manager` - every 30 minutes.
- `cleanup_manager` - daily.
- `purge_generated_resumes_periodic` - every six hours.

Useful management commands:

```powershell
..\.venv\Scripts\python.exe manage.py huey_queue_status
..\.venv\Scripts\python.exe manage.py dedupe_pipeline_jobs
..\.venv\Scripts\python.exe manage.py clear_applying_optimizations
..\.venv\Scripts\python.exe manage.py restore_llm_config
```

## Tests

Run Django tests from `django_project/`:

```powershell
..\.venv\Scripts\python.exe manage.py test resume_app
```

Important test files:

- `resume_app/tests.py` - general models, optimizer, jobs, and pipeline coverage.
- `resume_app/test_apply_agent.py` - apply-agent state and adapter behavior.
- `resume_app/test_multi_tenant.py` - owner isolation, signup seeding, and staff impersonation.
- `resume_app/test_job_prep.py` - cover letter and interview prep behavior.
- `resume_app/test_utils.py` - test helpers.

## Docker

See `docs/DOCKER.md` for container setup. The Docker architecture uses separate `web` and `huey` services sharing the same image and requires external Redis.

## Gotchas

- Redis defaults may point to `192.168.2.174`; override for local machines.
- Jobs stuck in queued state usually mean Huey is not running or Redis is unreachable.
- SQLite is acceptable for local development with one Huey worker; PostgreSQL is a better production target.
- `SECRET_KEY` protects encrypted provider keys, so key rotation requires a migration strategy.
- `HUEY_IMMEDIATE=1` is not equivalent to production because periodic tasks do not schedule normally.
- Playwright Chromium must be installed in the same environment that runs Huey.
- Job scraping can be slow or blocked by upstream sources.
- `README.md` may be stale; prefer this `docs/` directory for current behavior.
