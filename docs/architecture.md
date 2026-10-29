# Architecture

HireEdge is a multi-tenant Django 5.2 application featuring server-rendered HTML views, Django Ninja REST APIs, Huey background workers backed by Redis, and multi-provider LLM integrations.

---

## 1. Runtime Stack

- **Web Framework:** Django 5.2.
- **REST API Framework:** Django Ninja mounted at `/api/` with sub-routers for `/resume/` and `/jobs/`.
- **Authentication & Tenancy:** Session authentication with `LoginRequiredMiddleware` enforcing auth globally. Multi-tenancy uses tenant-scoped models (`owner = ForeignKey(User)`) via `resume_app.tenancy`.
- **Background Worker & Task Queue:** Huey backed by Redis (using `huey.RedisExpireHuey` with automatic result key expiration).
- **Relational Database:** SQLite for local/test defaults; MySQL/MariaDB for production via PyMySQL.
- **AI & Workflow Orchestration:** LangGraph and LangChain for structured resume optimization workflows; unified **LiteLLM** abstraction layer supporting OpenAI, Anthropic, Groq, Google Gemini, and local/cloud Ollama; **Langfuse Cloud** telemetry integration with selective PII redaction (`LANGFUSE_MASK_PII`), workflow trace grouping, and step-level generation naming.
- **Job Sourcing Engine:** Hexagonal architecture supporting JobSpy (Indeed, LinkedIn), Greenhouse direct boards, BuiltIn, Levels.fyi (encrypted API), and Dice (REST API + HTML fallback).
- **UI Architecture:** Server-rendered Django templates styled with Tailwind CSS, Chart.js for data visualization, and vanilla JavaScript `fetch()` for progressive telemetry and status polling. Brand theme tokens live in `resume_app/templates/resume_app/_tailwind_theme.html`.

---

## 2. Project Layout

```text
JobApp-Main/
├── django_project/
│   ├── manage.py                     # Django management entry point
│   ├── core/
│   │   ├── settings.py               # Configuration, apps, middleware, Huey, LLM settings
│   │   └── urls.py                   # Central routing and Ninja API registration
│   └── resume_app/
│       ├── models.py                 # 37 domain models (tenancy, resumes, jobs, pipeline)
│       ├── views.py                  # HTML views (cockpit, optimizer, search, settings, staff)
│       ├── pipeline_board.py         # Kanban pipeline board views and stage transitions
│       ├── api.py                    # Ninja REST API for resume optimizer and LLM endpoints
│       ├── jobs_api.py               # Ninja REST API for jobs, pipeline, and feedback actions
│       ├── tasks.py                  # Huey background workers, cron schedulers, locks
│       ├── sourcing/                 # Hexagonal job scraping architecture (ports, adapters, clients)
│       ├── agents.py                 # LangGraph resume optimizer graph (Writer, ATS, Recruiter)
│       ├── optimizer_budget.py       # Context budget trimming and semantic JD excerpting
│       ├── llm/                      # Central LLM gateway, provider factories, rate limiting, and policies
│       ├── dashboard_stats.py        # Performance dashboard telemetry aggregation
│       ├── tenancy.py                # Tenant isolation helpers (OwnedManager, get_owned_or_404)
│       └── templates/resume_app/     # Server-rendered HTML templates
├── docs/                             # Developer and agent knowledge base
└── ops/                              # Infrastructure provisioning (MariaDB, Redis)
```

---

## 3. Main Modules & Subsystems

- `models.py` — Domain models partitioned into global shared entities (`JobListing`, `JobDescription`, `AtsJudgeProfile`) and tenant-owned entities (`UserResume`, `CareerProfileAnalysis`, `OptimizedResume`, `PipelineEntry`, `JobSearchTask`, `SearchProfile`, `EmployerInterviewEvent`).
- `views.py` — Core server-rendered UI: Career Cockpit (`career_cockpit_view`), Resume Optimizer, Find Jobs, Settings, Performance Dashboard, Fit Inspector, and Staff tools.
- `pipeline_board.py` — Multi-stage Kanban pipeline management (Pipeline, Vetting, Applying, Done) with inline feedback actions.
- `api.py` & `jobs_api.py` — Django Ninja REST endpoints for interactive UI actions, background job polling, and data export.
- `tasks.py` — Huey background tasks: scheduled job scraping, vetting matching, metric recalculation, and cleanup.
- `sourcing/` — Hexagonal job ingestion subsystem with dedicated adapters for JobSpy, Greenhouse, BuiltIn, Levels.fyi, and Dice.
- `agents.py` & `optimizer_budget.py` — Multi-agent LangGraph workflow orchestrating resume re-writing, keyword matching, and recruiter readability scoring.
- `llm/` (`llm/gateway.py`, `llm/policy.py`, `llm/factory.py`) — Enterprise LLM subsystem managing provider selection, fallback chains, per-user daily token budgets, and concurrency controls.
- `tenancy.py` & `middleware.py` — Multi-tenant query scoping and global authentication enforcement.

---

## 4. Request Flow & Background Processing

```mermaid
sequenceDiagram
    autonumber
    actor User as User / Browser
    participant Web as Django Web Process
    participant Redis as Redis (Broker & Cache)
    participant Worker as Huey Worker Daemon
    participant DB as MySQL / MariaDB

    User->>Web: Action (e.g. "Run Search Now")
    Web->>Redis: Set job_task_pending:{task_id} (180s TTL)
    Web->>Redis: Set job_task_manual_cooldown:{task_id} (180s TTL)
    Web->>Redis: Enqueue task in Huey queue
    Web-->>User: HTTP 200 / Redirect to Cockpit

    loop Poll Status (every 2 seconds)
        User->>Web: GET /jobs/tasks/<id>/status/
        Web->>Redis: Check pending flag & user lock
        Web->>DB: Check latest JobSearchTaskRun
        Web-->>User: JSON {is_pending, is_running, status}
    end

    Worker->>Redis: Dequeue task
    Worker->>Redis: Acquire job_search_task_running:u{user_id}
    Worker->>DB: Create JobSearchTaskRun (status='running')
    Worker->>Redis: Delete job_task_pending:{task_id}
    Worker->>Worker: Scrape external boards & score listings
    Worker->>DB: Write JobListing, PipelineEntry, update Run
    Worker->>Redis: Release job_search_task_running:u{user_id}
    
    Note over User,Worker: Status endpoint now returns is_running: false with completed stats
```

---

## 5. Background Task Concurrency & Redis Architecture

To guarantee multi-tenant scalability, prevent duplicate scrapes, and safeguard users against lockouts:

1. **Per-Tenant Concurrency Lock (`job_search_task_running:u{user_id}`):**
   - Implemented via `get_job_search_task_lock_key(user_id)`.
   - Ensures that concurrent searches for different users run in parallel, while multiple searches for the same user are safely queued or skipped.
   - Protected by a Python `try ... finally: cache.delete(lock_key)` block and a 3,600s Redis safety TTL.
2. **Pending Handshake (`job_task_pending:{task_id}`):**
   - Set atomically on task dispatch with a 180s TTL.
   - Prevents the client polling loop from encountering race conditions where a task is enqueued in Redis but not yet written to the database by the worker.
3. **Manual Trigger Cooldown (`job_task_manual_cooldown:{task_id}`):**
   - 3-minute TTL (180s) preventing accidental or abusive repeated scraping.
   - **Automatic Failure Recovery:** If a task fails due to a network error, exception, or timeout, the worker and the status views immediately delete this key, allowing the user to retry without waiting for cooldown expiry.
4. **Vetting Matching Concurrency Lock (`vetting_matching_task_running:u{user_id}`):**
   - 1,800s TTL. Enforces sequential execution of deep analysis per user so local Ollama reuses KV cache prefixes across consecutive jobs.
5. **Distributed Periodic Leader Election (`periodic_leader_lock:{task_name}`):**
   - Acquired via atomic `cache.add()` with bounded TTLs (45s–300s).
   - In multi-replica container deployments (e.g. Docker Compose or Kubernetes with multiple worker instances), guarantees that exactly one worker replica acts as execution leader per periodic tick, preventing duplicate scheduled runs of `pipeline_manager`, `cleanup_manager`, and search queues.
6. **Dead-Letter Queue (DLQ) Quarantine (`FailedTaskQuarantine`):**
   - Automatically connected to Huey's `SIGNAL_ERROR` lifecycle hook.
   - When a task exhausts retries and crashes, its task name, execution arguments, error class, error message, and full stack traceback are quarantined in the database.
   - Operators can inspect, replay, or purge quarantined tasks using `python manage.py task_dlq`.

---

## 6. Review-Stage Deep Analysis & Two-Pass Batch Architecture

To eliminate runaway compute costs while providing deep semantic insights, LLM diagnostics run **only for opportunities in Review (`vetting`) and Applying (`applying`) stages** via the optimized Candidate Fit & Skill Radar architecture:

```mermaid
flowchart TD
    subgraph MatchRadar ["Resume Match Radar (Candidate Context)"]
        P_Jobs["Review-Stage PipelineEntries"] --> P_Cleanse["Heuristic Cleanser<br>(0ms regex, no LLM)"]
        P_Cleanse --> P_Ollama["SkillRadarService.analyze<br>(Candidate Resume Prefix Cached)"]
        P_Ollama --> P_DB["Tenant Write: PipelineEntry<br>(prob, reasoning, stretch_skills)"]
        P_Ollama --> P_Redis["Redis: cockpit_ollama_skill_radar_... (7d)"]
    end
```

1. **Resume Match Radar (`SkillRadarService.analyze`):**
   - **Candidate Resume KV Alignment:** Ordering Candidate Resume ahead of the Job Description allows Ollama to reuse the candidate KV prefix, cutting latency from ~6.95s to ~2.06s (~70% speedup).
   - **Heuristic JD Cleansing:** Zero-latency regex section-stripping (`use_llm=False`) cleans boilerplate without eviction.
   - **Dual Persistence:** Instant modal loading via Redis cache with fallback persistence in `PipelineEntry`.

---

## 7. Resume Optimizer Graph

Default compiled graph: **Writer → ATS Judge → Recruiter Judge** (single pass, `agents.create_workflow`).

- **Context Assembly:** `optimizer_budget.build_optimizer_context_state()` produces a role-focused JD slice (extracting role requirements while omitting generic employer boilerplate), deduplicates prompts, and caps judge context.
- **Hybrid Local Routing:** Writer is cloud-only (`allow_local=False`) for maximum synthesis quality. ATS and Recruiter judges can be routed to local Ollama (`OPTIMIZER_JUDGES_PREFER_LOCAL=True`) to minimize paid API token costs.
- **Reference:** [`django_project/resume_app/docs/OPTIMIZER_PAGE.md`](../django_project/resume_app/docs/OPTIMIZER_PAGE.md).

---

## 8. Authentication, Tenancy & Security

- **Authentication:** Django session authentication. `LoginRequiredMiddleware` blocks unauthenticated access across all app paths (exceptions: `/`, `/accounts/*`, `/admin/`, `/static/`).
- **Owner Scoping:** All user-specific models inherit or use `owner = ForeignKey(User)`. Queries must use `OwnedManager.for_user(user)` or `get_owned_or_404()`.
- **Media Protection:** Media files under `/media/` are served via `serve_media_view`, which checks authentication and ownership authorization.
- **Impersonation:** Staff users with `can_impersonate_users` can impersonate users via `django-hijack`. The session logs audit records in `ImpersonationAuditLog`.
- **Quotas & Throttling:** Governed by `Plan`, `Subscription`, and `UsageCounter`. Rate limiting for APIs and authentication paths is enforced by `AbuseThrottleMiddleware`.

---

## 9. Architectural Rules for Engineers and Agents

1. **Preserve Tenant Scoping:** Never perform un-scoped queries on tenant-owned models. Always use `for_user(user)`.
2. **Global vs Tenant Catalog:** Treat `JobListing` as a shared global catalog; store user reactions, pipeline states, and notes in `PipelineEntry` and `JobListingAction`.
3. **Route LLMs via Gateway:** Never bypass `resume_app/llm/gateway.py` with direct SDK calls. All calls must respect token budgets, user stop switches, and audit logging.
4. **Reliable Background Execution:** Keep long-running I/O (scraping, AI generation, embedding calculation) in Huey tasks with durable database state.
5. **Lock Lifecycle:** Always wrap Redis lock releases in `try ... finally` blocks and set explicit TTL expiration.
