# Operations

This document covers local setup, environment variables, background workers, tests, and known operational gotchas.

## Local Setup

Use the repository virtual environment. Do not run Django commands with a system Python interpreter.

From the repo root:

```powershell
.\.venv\Scripts\python.exe -c "import sys; print(sys.prefix)"
.\.venv\Scripts\pip.exe install -r requirements.txt
.\.venv\Scripts\pip.exe install --no-deps -r requirements-jobspy.txt
```

JobSpy is a second step: upstream pins `markdownify<0.14`, which conflicts with `browser-use` (`markdownify>=1.2`). Runtime deps for JobSpy are already listed in `requirements.txt`.

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
- `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_HOST`, `MYSQL_PORT` - when `MYSQL_DATABASE` is set, Django uses MySQL/MariaDB instead of `django_project/db.sqlite3`. Tests still use SQLite unless `MYSQL_USE_FOR_TESTS=1`. Use a dedicated least-privilege user scoped to this one DB (see `ops/mariadb/provision.sql`).
- `MYSQL_SSL_CA`, `MYSQL_SSL_VERIFY_IDENTITY`, `MYSQL_SSL_DISABLED` - TLS to the DB. The server negotiates encryption by default; set `MYSQL_SSL_CA` for verified TLS, or `MYSQL_SSL_DISABLED=1` for plaintext (local socket).
- `MYSQL_TEST_DATABASE`, `MYSQL_CONN_HEALTH_CHECKS`, `MYSQL_DUMP_BIN` - test DB name (default `test_<db>`), persistent-connection health checks, and the native dump binary for `dbbackup`.
- `SIGNUP_ENABLED` - enables or disables public signup.
- `REQUIRE_EMAIL_VERIFICATION` - when true, unverified users are limited to account settings until they verify.
- `EMAIL_BACKEND`, `DEFAULT_FROM_EMAIL`, `ACCOUNT_EMAIL_BASE_URL`, `EMAIL_HOST*` - transactional email for verification and password reset (console backend by default).
- `SAAS_ENFORCE_QUOTAS`, `SAAS_DEFAULT_PLAN_SLUG`, `SAAS_STAFF_BYPASS_QUOTAS` - plan quota enforcement (staff bypass off by default).
- `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET` - optional Stripe billing (webhook signature required when `DEBUG=False`).
- `DJANGO_CACHE_USE_REDIS`, `DJANGO_CACHE_URL`, `DJANGO_CACHE_REDIS_DB` - shared Redis cache for abuse throttle (auto-on when `DEBUG=False` and Huey is not immediate).
- `FERNET_KEYS` - optional multi-key encryption rotation (else derived from `SECRET_KEY`).
- `ABUSE_*` - login/API throttling.
- `SECURE_*` / `CSRF_TRUSTED_ORIGINS` - HTTPS cookies and HSTS (default on when `DEBUG=0`).
- `AWS_STORAGE_BUCKET_NAME` (+ related AWS_*) - optional S3 media via django-storages.
- `HUEY_REDIS_HOST`, `HUEY_REDIS_PORT`, `HUEY_REDIS_DB` - Redis connection for Huey (default host `127.0.0.1`; keep the broker on a private network).
- `REDIS_PASSWORD`, `REDIS_USE_TLS` - shared Redis AUTH password and TLS toggle. Applied consistently to Huey, the Django cache (abuse throttle), and the LLM rate limiter. Set a password on any shared/LAN Redis: task results are **pickled**, so write access to Redis is effectively remote code execution in the worker.
- `HUEY_RESULT_EXPIRE_SECONDS` - TTL for stored task results (default 86400). The app uses `huey.RedisExpireHuey`, which writes each result to its own expiring key. Plain `RedisHuey` keeps results in a hash that is only pruned when a caller reads the result, so it grows without bound.
- `HUEY_IMMEDIATE` - run tasks in-process for local testing.
- `LLM_USER_DAILY_REQUEST_LIMIT` - per-user LLM request cap; `0` means unlimited.
- `LLM_DAILY_TOKEN_LIMIT_FREE` / `_PRO` / `_UNLIMITED` - daily token budgets by plan (defaults 200k / 2M / unlimited).
- `LLM_USER_DAILY_TOKEN_LIMIT` - optional hard ceiling on tokens for all plans.
- `LLM_PLATFORM_DAILY_TOKEN_LIMIT` - shared daily burn cap for platform/env API keys (default 5M).
- `LLM_INVOKE_TIMEOUT_SECONDS` - wall-clock timeout per LLM invoke (default 180).
- `LLM_USER_MAX_CONCURRENT` - max in-flight LLM calls per user (default 2).
- `LLM_COMPLETE_MAX_INPUT_CHARS` - max system+user chars for `POST /llm/complete` (default 16000).
- `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `ADZUNA_COUNTRY` - Adzuna job source.
- `LEVELS_FYI_STANDARD_LEVELS`, `LEVELS_FYI_OFFSET_STEP`, `LEVELS_FYI_PAGE_DELAY`, `LEVELS_FYI_MAX_SCAN_PAGES` - Levels.fyi job source (undocumented encrypted API; no keys required).
- `APPLY_USE_MOCK_RESOLVER` - use mock apply URL resolution in dev.
- `APPLY_BROWSER_HEADLESS` - visible or headless browser automation.
- `LLM_RATE_LIMIT_*` - Redis-backed provider RPM/TPM limits. `LLM_RATE_LIMIT_FAIL_OPEN` defaults to `DEBUG` (fail-closed in production). OpenAI and Groq are wired by default. Full gateway docs: [`resume_app/docs/LLM_GATEWAY.md`](../django_project/resume_app/docs/LLM_GATEWAY.md).

**Optimizer token budgets and hybrid judge routing** (see `core/settings.py` and `resume_app/docs/OPTIMIZER_PAGE.md`):

- `OPTIMIZER_JUDGES_PREFER_LOCAL` - default `True`; ATS/Recruiter prefer an Ollama row marked local in Settings.
- `OPTIMIZER_JUDGE_RESUME_MAX_CHARS` - default `12000`; cap resume draft sent to judges.
- `OPTIMIZER_JUDGE_JD_MAX_CHARS` - default `8000`; cap JD sent to judges.
- `OPTIMIZER_USE_ROLE_SLICE_FOR_WRITER_JD` - default `True`; semantic role extract instead of prefix truncate.
- `OPTIMIZER_WRITER_JD_ROLE_MAX_CHARS` - default `8000`; max chars for role-focused JD excerpt.
- `OPTIMIZER_WRITER_RESUME_MAX_CHARS`, `OPTIMIZER_SOURCE_RESUME_MAX_CHARS` - Writer body vs PDF anchor caps.
- `OPTIMIZER_CONTEXT_*_MAX_CHARS`, `OPTIMIZER_RETRIEVAL_*` - supporting context and RAG limits.

LLM provider keys can be stored per user through Settings. Mark at least one Ollama integration as **local** for hybrid judge routing. Environment fallbacks include `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, and `GOOGLE_API_KEY`.

## Dependencies

Important packages include:

- Django, django-ninja, django-environ, django-hijack.
- Huey, Redis, croniter.
- LangGraph, LangChain, provider SDKs, tiktoken.
- sentence-transformers, numpy, rank-bm25.
- python-jobspy and requests.
- pdfplumber, reportlab, python-docx (Pillow via those).
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
..\.venv\Scripts\python.exe manage.py set_staff_user alice bob
..\.venv\Scripts\python.exe manage.py set_staff_user alice --superuser
..\.venv\Scripts\python.exe manage.py set_staff_user alice --remove
..\.venv\Scripts\python.exe manage.py dedupe_user_emails --dry-run
```

`set_staff_user` sets `is_staff` and adds the **Support** group (for `/staff/users/`). Use `--no-support` for Django admin only, `--superuser` for full admin, `--remove` to revoke.

Migration `0025` clears duplicate emails (keeps the strongest account per address) and adds a unique DB index on non-blank `auth_user.email`. Preview with `dedupe_user_emails --dry-run` before migrating if needed.

## Database Backups (MySQL/MariaDB)

```powershell
# Create a gzipped logical backup in django_project/backups/.
# Uses native mysqldump/mariadb-dump when available, else a pure-Python dumper.
..\.venv\Scripts\python.exe manage.py dbbackup

# Verify a backup by restoring it into the test database and counting rows.
..\.venv\Scripts\python.exe manage.py dbrestore backups\resxjob-<timestamp>.sql.gz --verify
```

- `dbrestore` targets `MYSQL_TEST_DATABASE` (default `test_<db>`) and refuses to overwrite the live DB without `--force`.
- Both the test suite (`MYSQL_USE_FOR_TESTS=1`) and `dbrestore --verify` need the app user to have rights on the test/scratch DB. An admin must run `ops/mariadb/provision.sql` once to create the least-privilege app user and grant `test_<db>`.
- **TODO(ops):** schedule `dbbackup` (cron/Task Scheduler), ship dumps off-host (S3/rsync), and add retention pruning + restore drills.

### Least-privilege DB user and TLS

- The application should connect as a user scoped to the single app schema (not a global admin). See `ops/mariadb/provision.sql`.
- The MariaDB server negotiates TLS automatically; set `MYSQL_SSL_CA` to enforce **verified** TLS in production.

### Redis security

- Prefer a **dedicated Redis instance** for JobApp-Main (this checkout uses port `6380` with AUTH) so other apps' cache data on `6379` (e.g. `optiondataID:*`) cannot collide with Huey or OOM the queue.
- Redis is shared by Huey, the Django cache (abuse throttle), and the LLM rate limiter. Set `REDIS_PASSWORD` (and `REDIS_USE_TLS` where the broker terminates TLS) and keep the broker on a private network / firewalled port. Defaults now point at `127.0.0.1` rather than a LAN IP.

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
- Set a unique `HUEY_NAME` per checkout when multiple JobApp clones share one Redis (default `jobapp-main`). Colliding names steal each other's tasks.
- Jobs stuck in queued state usually mean Huey is not running or Redis is unreachable. Apply Agent also needs `python manage.py run_huey` in this repo's venv.
- Apply Agent: enable the master switch on `/jobs/apply-agent/profile/`, fill name + email, and keep `APPLY_USE_MOCK_RESOLVER=0` (default) so Indeed/LinkedIn URLs resolve live. Mock mode is for CI only.
- SQLite is acceptable for local development with one Huey worker; set `MYSQL_*` for MySQL/MariaDB (recommended for multi-worker / Docker). One-time data move: `manage.py copy_sqlite_to_mysql` after `migrate` on an empty MySQL database.
- `SECRET_KEY` protects encrypted provider keys, so key rotation requires a migration strategy.
- `HUEY_IMMEDIATE=1` is not equivalent to production because periodic tasks do not schedule normally.
- Playwright Chromium must be installed in the same environment that runs Huey.
- Job scraping can be slow or blocked by upstream sources.
- `README.md` may be stale; prefer this `docs/` directory for current behavior.
