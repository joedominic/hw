# HireEdge Knowledge Base

This directory is the developer- and agent-facing knowledge base for HireEdge.

> [!IMPORTANT]
> **Documentation Authority & Spec Hierarchy:**
> - **Behavioral Specifications (Authoritative):** [`site-functionality.md`](site-functionality.md), [`career-cockpit.md`](career-cockpit.md), [`performance-and-analytics.md`](performance-and-analytics.md), [`data-model.md`](data-model.md), and [`api-reference.md`](api-reference.md) constitute the authoritative specification for how HireEdge works. Agents and engineers must follow these specs when verifying or changing behavior.
> - **Dated Audits & Market Summaries (Informational Only):** [`product_feature_brief.md`](product_feature_brief.md), [`saas_readiness_analysis.md`](saas_readiness_analysis.md), and [`HIREEDGE_UX_ANTI_SLOP_AUDIT.md`](HIREEDGE_UX_ANTI_SLOP_AUDIT.md) are point-in-time design audits and product briefs. They do not override the behavioral spec files.

---

## What The Site Does

HireEdge is an AI-powered career operating system that integrates executive career profiling, unbiased multi-channel job discovery, high-velocity Kanban triage, multi-agent AI resume tailoring, and interview outcome tracking into a single unified workspace.

The complete user workflow is structured around the **Stitch 3-Tab Model**:
1. **Tab 1: Target & Discover (`/jobs/profiler/` & `/jobs/search/`)** — AI Resume Profiler and Search Sandbox (Unbiased) with 1-click `+ Add to Review` bridge.
2. **Tab 2: Career Cockpit (`/jobs/cockpit/`)** — Central tri-pane triage workstation, slide-over Automation Drawer (`openAutomationDrawer()`), deep LLM fit evaluation, and in-funnel resume tailoring return loop.
3. **Tab 3: Tracker (`/performance/`)** — Career Momentum & Outcomes, active streaks, weekly goals, and employer interview tracking.

For the complete, canonical user journey specification across the 3 tabs, see **[`docs/site-functionality.md`](site-functionality.md)**.

---

## Core Documentation

### Behavioral Specs & Reference
- [Site Functionality](site-functionality.md) — Canonical user journeys, page mechanics, and 3-tab interaction patterns.
- [Career Cockpit](career-cockpit.md) — Tri-pane layout, slide-over automation drawer, on-demand execution, cooldown lifecycle, and keyboard triage hotkeys.
- [Performance & Analytics](performance-and-analytics.md) — Momentum dashboard, conversion metrics, and employer interview tracking.
- [Data Model](data-model.md) — Domain entities, tenant ownership model, relationships, and pipeline stage transitions.
- [API Reference](api-reference.md) — HTML routes and Django Ninja REST API surface.
- [Job Sourcing & Scraping](job-sources-scraping.md) — Multi-channel scraping architecture, adapters, deduplication, and ranking.

### Architecture & Operations
- [Architecture](architecture.md) — Runtime stack, component boundaries, background processing, and Redis locks.
- [Operations](operations.md) — Environment configuration, Redis setup, background workers, tests, and operational practices.
- [Agent Guide](agent-guide.md) — Engineering conventions, entry points, and maintenance map for AI agents.
- [User Journey Trace](user-journey.md) — End-to-end lifecycle trace of a single job listing from ingestion to offer.
- [Docker Deployment](DOCKER.md) — Containerized multi-service deployment.
- [Search Profile Consolidation](search-profile-consolidation.md) — Status: Phase 1 done, Phase 2 partial, Phases 3–5 open.

### Dated Audits & Point-in-Time Reviews (Informational)
- [Product Feature Brief](product_feature_brief.md) — High-level capabilities and market positioning overview.
- [SaaS Readiness Analysis](saas_readiness_analysis.md) — Architectural audit of multi-tenancy, billing, and operational readiness.
- [Anti-Slop UX Audit](HIREEDGE_UX_ANTI_SLOP_AUDIT.md) — Design audit and UI simplification findings.

### Subsystem Detail Notes
- Optimizer detail: [`django_project/resume_app/docs/OPTIMIZER_PAGE.md`](../django_project/resume_app/docs/OPTIMIZER_PAGE.md) — Token budgets, single-pass graph, and hybrid local Ollama judge routing.
- LLM gateway detail: [`django_project/resume_app/docs/LLM_GATEWAY.md`](../django_project/resume_app/docs/LLM_GATEWAY.md) — Invocation pipeline, token budgets, rate limits, and fallback policies.
- Huey background tasks: [`django_project/resume_app/docs/HUEY_TASKS.md`](../django_project/resume_app/docs/HUEY_TASKS.md) — Comprehensive task signatures, scheduling, and side effects.
- UI architecture: [`django_project/resume_app/docs/ARCHITECTURE_UI.md`](../django_project/resume_app/docs/ARCHITECTURE_UI.md) — UI component design, layout structure, and styling standards.
- Job search page: [`django_project/resume_app/docs/JOB_SEARCH_PAGE.md`](../django_project/resume_app/docs/JOB_SEARCH_PAGE.md) — Search query execution, pagination, and feedback handling.
- Simplification roadmap: [`django_project/resume_app/docs/SIMPLIFICATION.md`](../django_project/resume_app/docs/SIMPLIFICATION.md) — Historical cleanup decisions and removed legacy surfaces.

---

## Fast Orientation

The Django project root is `django_project/`; the main product app is `django_project/resume_app/`. Central routing is defined in `django_project/core/urls.py`. Server-rendered views reside primarily in `resume_app/views.py` and `pipeline_board.py`. REST API routers reside in `api.py` and `jobs_api.py`. Huey background workers and scheduled managers reside in `tasks.py`. LLM router and gateway services reside in `resume_app/llm/`.

Always run commands using the repository virtual environment:
```powershell
# From django_project/:
..\env\Scripts\python.exe manage.py <command>

# Or from repository root:
.\env\Scripts\python.exe django_project\manage.py <command>
```
*(On Linux/macOS: `../env/bin/python manage.py <command>` or `./env/bin/python django_project/manage.py <command>`)*
