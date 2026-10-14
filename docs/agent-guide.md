# Agent Guide

This guide establishes conventions, high-value entry points, and architectural rules for AI coding assistants working in the HireEdge repository.

---

## 1. Quick Orientation

Before modifying features, routes, or models, review the relevant knowledge base files:

1. `docs/README.md` — High-level layout, spec hierarchy, and entry points.
2. Behavioral specs (authoritative):
   - `docs/site-functionality.md` — Canonical user workflows and UI behavior.
   - `docs/career-cockpit.md` — Task scheduling, on-demand execution, cooldowns, and keyboard hotkeys.
   - `docs/performance-and-analytics.md` — Career momentum dashboard and interview tracking.
   - `docs/data-model.md` — Model schemas, stage transitions, and multi-tenant scoping.
   - `docs/api-reference.md` — URL routing and Django Ninja endpoints.
3. System and operations guides:
   - `docs/architecture.md` — Framework boundaries and background execution.
   - `docs/job-sources-scraping.md` — Multi-board scraping architecture and adapters.
   - `docs/operations.md` — Environment, testing, and worker management.

---

## 2. High-Value Entry Points

- **Routing:** `django_project/core/urls.py`
- **Settings:** `django_project/core/settings.py`
- **Domain Models:** `django_project/resume_app/models.py`
- **Tenancy Scoping:** `django_project/resume_app/tenancy.py`
- **Career Cockpit & Views:** `django_project/resume_app/views.py`
- **Kanban Pipeline Board:** `django_project/resume_app/pipeline_board.py`
- **Resume & LLM REST API:** `django_project/resume_app/api.py`
- **Jobs & Pipeline REST API:** `django_project/resume_app/jobs_api.py`
- **Background Tasks & Workers:** `django_project/resume_app/tasks.py`
- **Job Sourcing Engine:** `django_project/resume_app/sourcing/` and `job_sources.py`
- **Resume Optimizer Graph:** `django_project/resume_app/agents.py`
- **Optimizer Context & Token Budgets:** `django_project/resume_app/optimizer_budget.py`
- **LLM Subsystem & Gateway:** `django_project/resume_app/llm/` (`gateway.py`, `factory.py`, `policy.py`)
- **Performance Telemetry:** `django_project/resume_app/dashboard_stats.py`

---

## 3. Core Coding Rules

1. **Virtual Environment Discipline:**
   Always execute Python commands with the repository virtualenv:
   ```powershell
   # From django_project/:
   ..\env\Scripts\python.exe manage.py <command>

   # Or from repository root:
   .\env\Scripts\python.exe django_project\manage.py <command>
   ```
   *(On Linux/macOS: `../env/bin/python manage.py <command>` or `./env/bin/python django_project/manage.py <command>`)*
2. **Tenant Scoping Enforcement:**
   Always scope queries on user models using `.for_user(user)` or `get_owned_or_404()`. Never write global queries that could leak data across tenants.
3. **Shared Job Catalog vs Tenant State:**
   `JobListing` is shared globally across all users. Never add user-specific state to `JobListing`. Record user interactions in `PipelineEntry`, `JobListingAction`, and `EmployerInterviewEvent`.
4. **Background Task Isolation:**
   All network I/O, scraping, and LLM generation must execute asynchronously in Huey tasks with durable database progress stored in models (`JobSearchTaskRun`, `OptimizedResume`).
5. **Redis Lock Safety:**
   Always acquire Redis locks with safety timeouts and release them in `try ... finally` blocks.
6. **LLM Gateway Routing:**
   Never bypass `resume_app/llm/gateway.py` with raw vendor SDK calls. All AI invocations must respect token budgets, daily caps, and user stop controls.

---

## 4. Documentation Maintenance

When modifying code, you **must update the corresponding documentation in `docs/` in the same change**:

- **Adding or modifying a route/URL:** Update `docs/api-reference.md`.
- **Adding or modifying a database model:** Update `docs/data-model.md`.
- **Workflows, UI buttons & hotkeys:** Update `docs/site-functionality.md` and `docs/career-cockpit.md`.
- **Telemetry or interview stages:** Update `docs/performance-and-analytics.md`.
- **Scraping adapters or sources:** Update `docs/job-sources-scraping.md`.
- **Operations, environment, tests:** Update `docs/operations.md`.
