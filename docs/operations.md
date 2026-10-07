# Operations

This document covers operational procedures, local setup, environment configuration, worker management, test suites, and operational gotchas for HireEdge.

---

## 1. Local Setup

Always use the project's virtual environment (`env/`). Do not run Django management commands with a global or system Python interpreter.

From the repository root (`D:\Workshop\JobApp-Main`):

```powershell
# Verify Python virtualenv
& "D:\Workshop\JobApp-Main\env\Scripts\python.exe" -c "import sys; print(sys.prefix)"

# Install dependencies if necessary
& "D:\Workshop\JobApp-Main\env\Scripts\pip.exe" install -r requirements.txt
& "D:\Workshop\JobApp-Main\env\Scripts\pip.exe" install --no-deps -r requirements-jobspy.txt
```

### Running the Services

Full functionality requires two concurrent processes: the Django web server and the Huey task worker.

**Process 1: Web Server**
```powershell
cd D:\Workshop\JobApp-Main\django_project
& "D:\Workshop\JobApp-Main\env\Scripts\python.exe" manage.py migrate
& "D:\Workshop\JobApp-Main\env\Scripts\python.exe" manage.py runserver
```

**Process 2: Huey Task Worker**
```powershell
cd D:\Workshop\JobApp-Main\django_project
& "D:\Workshop\JobApp-Main\env\Scripts\python.exe" manage.py run_huey
```

> [!NOTE]
> Setting `HUEY_IMMEDIATE=1` executes tasks synchronously in the web process for single-process testing, but disables periodic scheduling and background task isolation.

---

## 2. Common Service URLs

- `http://127.0.0.1:8000/` — Landing page (redirects to pipeline/cockpit).
- `http://127.0.0.1:8000/jobs/cockpit/` — Career Cockpit (search automation hub).
- `http://127.0.0.1:8000/jobs/search/` — Multi-board job discovery.
- `http://127.0.0.1:8000/resume/optimizer/` — Multi-agent resume optimizer.
- `http://127.0.0.1:8000/performance/` — Performance momentum dashboard.
- `http://127.0.0.1:8000/system/fit-inspector/` — System fit and scoring diagnostics.
- `http://127.0.0.1:8000/jobs/huey/` — Staff Huey queue monitor.
- `http://127.0.0.1:8000/api/docs` — OpenAPI interactive documentation.

---

## 3. Environment Configuration

HireEdge reads environment variables from the `.env` file in the repository root.

### 3.1 Core & Database
- `SECRET_KEY`: Django cryptographic key.
- `DEBUG`: Set to `True` for local development; `False` in production.
- `ALLOWED_HOSTS`: Permitted hostnames.
- `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_HOST`, `MYSQL_PORT`: Configures MySQL/MariaDB connection (PyMySQL). If `MYSQL_DATABASE` is empty, the app defaults to SQLite at `django_project/db.sqlite3`.
- `MYSQL_CONN_HEALTH_CHECKS`: Enables persistent connection validation.

### 3.2 Redis & Huey Queue
- `HUEY_REDIS_HOST`: Redis host IP (e.g. `127.0.0.1` or LAN host `192.168.2.174`).
- `HUEY_REDIS_PORT`: Redis port (default instance uses `6380`).
- `HUEY_REDIS_DB`: Redis database index (default `0`).
- `REDIS_PASSWORD`: Shared authentication password for Redis.
- `REDIS_USE_TLS`: Enable SSL/TLS encryption for Redis transport.
- `HUEY_NAME`: Queue namespace (defaults to `jobapp-main` to avoid task collision across checkouts).
- `HUEY_RESULT_EXPIRE_SECONDS`: TTL for task results in Redis (default `86400`s).

### 3.3 LLM Gateway & Provider Limits
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `GOOGLE_API_KEY`: Global provider fallback keys.
- `LLM_USER_DAILY_REQUEST_LIMIT`: Daily request cap per user.
- `LLM_DAILY_TOKEN_LIMIT_FREE` / `_PRO` / `_UNLIMITED`: Daily token allowances by plan.
- `LLM_PLATFORM_DAILY_TOKEN_LIMIT`: Daily spending ceiling for environment-backed platform keys.
- `LLM_USER_MAX_CONCURRENT`: Maximum in-flight LLM requests per user (default `2`).
- `OPTIMIZER_JUDGES_PREFER_LOCAL`: Route ATS and Recruiter judges to local Ollama (default `True`).

### 3.4 Job Scraping Settings
- `LEVELS_FYI_STANDARD_LEVELS`: Seniority level filters for Levels.fyi.
- `LEVELS_FYI_OFFSET_STEP`, `LEVELS_FYI_PAGE_DELAY`, `LEVELS_FYI_MAX_SCAN_PAGES`: Traversal tuning for Levels.fyi.

---

## 4. Background Workers & Periodic Schedulers

The Huey worker process (`manage.py run_huey`) consumes background tasks and executes periodic schedules.

### Periodic Task Schedule:
| Task Function | Schedule | Description |
|---------------|----------|-------------|
| `enqueue_due_job_search_tasks` | Every 1 minute | Dispatches due search tasks to scraping workers |
| `mark_stale_job_search_runs_failed` | Every 15 minutes | Fails tasks stalled longer than 30 minutes; purges locks & cooldowns |
| `enqueue_due_vetting_matching_tasks` | Every 20 minutes | Dispatches vetting match evaluation jobs |
| `pipeline_manager` | Every 30 minutes | Refreshes preference centroids and prunes weak fits |
| `purge_generated_resumes_periodic` | Every 6 hours | Deletes stale temporary optimization drafts |
| `cleanup_manager` | Daily at 01:30 UTC | Archives inactive pipeline rows and logs |

---

## 5. Automated Test Suites

Run Django unit and integration tests using the virtualenv from `django_project/`:

```powershell
# Run the entire test suite
& "D:\Workshop\JobApp-Main\env\Scripts\python.exe" manage.py test resume_app

# Run specific functional suites
& "D:\Workshop\JobApp-Main\env\Scripts\python.exe" manage.py test resume_app.test_job_automation_cockpit
& "D:\Workshop\JobApp-Main\env\Scripts\python.exe" manage.py test resume_app.test_employer_responses
& "D:\Workshop\JobApp-Main\env\Scripts\python.exe" manage.py test resume_app.test_multi_tenant
& "D:\Workshop\JobApp-Main\env\Scripts\python.exe" manage.py test resume_app.test_saved_searches
& "D:\Workshop\JobApp-Main\env\Scripts\python.exe" manage.py test resume_app.test_llm_gateway_routing
```

---

## 6. Operational Gotchas & Troubleshooting

1. **Job Stuck on "Queued in Worker...":**
   - Verify that `manage.py run_huey` is active.
   - If tasks are enqueued but not executing, check the Redis connection (`manage.py huey_queue_status`) and confirm `HUEY_NAME` matches across web and worker configs.
2. **Premature Cooldown Timer:**
   - The status endpoint `/jobs/tasks/<id>/status/` relies on `job_task_pending:{task_id}` to bridge task dispatch and worker pickup. If the worker is dead, the task remains pending for 180 seconds, alerting the UI.
3. **Automatic Cooldown Reset on Failure:**
   - If a search fails, `tasks.py` and `views.py` automatically clear `job_task_manual_cooldown:{task_id}`, resetting remaining cooldown to 0. Users can click "Run Search Now" immediately without waiting 60 minutes.
4. **Redis Port & Password:**
   - The default Redis instance runs on port `6380` with password protection. Using default unauthenticated `6379` will fail or connect to unrelated services.
5. **Staff Access & Impersonation:**
   - Use `python manage.py set_staff_user <username>` to grant staff status and add the user to the `Support` group for access to `/staff/users/`.
