# Agent Guide

This guide establishes conventions, high-value entry points, and architectural rules for AI coding assistants working in the HireEdge repository.

---

## 1. Quick Orientation

Before modifying features, routes, or models, review the relevant knowledge base files:

1. `docs/README.md` — High-level layout and entry points.
2. Domain-specific documents:
   - `docs/career-cockpit.md` — Task scheduling, on-demand execution, cooldowns, and inspection.
   - `docs/job-sources-scraping.md` — Multi-board scraping architecture and adapters.
   - `docs/performance-and-analytics.md` — Performance dashboard and interview tracking.
   - `docs/site-functionality.md` — User workflows and UI behavior.
   - `docs/architecture.md` — Framework boundaries and background execution.
   - `docs/data-model.md` — Model schemas and multi-tenant scoping.
   - `docs/api-reference.md` — URL routing and Django Ninja endpoints.
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
- **LLM Gateway & Rate Limiting:** `django_project/resume_app/llm_gateway.py` and `llm_policy.py`
- **Performance Telemetry:** `django_project/resume_app/dashboard_stats.py`

---

## 3. Core Coding Rules

1. **Virtual Environment Discipline:**
   Always execute Python commands with the repository virtualenv at `D:\Workshop\JobApp-Main\env\Scripts\python.exe`.
2. **Tenant Scoping Enforcement:**
   Always scope queries on user models using `.for_user(user)` or `get_owned_or_404()`. Never write global queries that could leak data across tenants.
3. **Shared Job Catalog vs Tenant State:**
   `JobListing` is shared globally across all users. Never add user-specific state to `JobListing`. Record user interactions in `PipelineEntry`, `JobListingAction`, and `EmployerInterviewEvent`.
4. **Background Task Isolation:**
   All network I/O, scraping, and LLM generation must execute asynchronously in Huey tasks with durable database progress stored in models (`JobSearchTaskRun`, `OptimizedResume`).
5. **Redis Lock Safety:**
   Always acquire Redis locks with safety timeouts and release them in `try ... finally` blocks.
6. **LLM Gateway Routing:**
   Never bypass `llm_gateway.py` with raw vendor SDK calls. All AI invocations must respect token budgets, daily caps, and user stop controls.

---

## 4. Documentation Maintenance

When modifying behavior, update the corresponding documentation in `docs/` in the same commit:

- **Workflows & Features:** `docs/site-functionality.md` or `docs/career-cockpit.md`.
- **Routes & REST APIs:** `docs/api-reference.md`.
- **Database Models & Relationships:** `docs/data-model.md`.
- **Scraping & Sourcing:** `docs/job-sources-scraping.md`.
- **Operations & Tests:** `docs/operations.md`.
