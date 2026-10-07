# HireEdge Knowledge Base

This directory is the developer- and agent-facing knowledge base for HireEdge. Start here before changing product behavior, routes, data models, background jobs, or automation flows.

## What The Site Does

HireEdge is a multi-user Django application for managing an end-to-end job search workflow:

- **AI Resume Optimization:** Rewrite and tailor resumes against target job descriptions using a LangGraph pipeline (Writer → ATS Judge → Recruiter Judge).
- **Multi-Source Job Aggregation:** Search and rank jobs across multiple job boards (Indeed, LinkedIn via JobSpy, direct Greenhouse boards, BuiltIn, Levels.fyi, and Dice).
- **Career Cockpit & Automation:** Schedule and monitor recurring job searches with on-demand manual triggers, cooldown protection, and detailed ingestion analytics.
- **Kanban Pipeline Board:** Manage opportunities across Pipeline, Vetting, Applying, and Done stages with semantic fit and user preference scoring.
- **Interview & Prep Intelligence:** Generate tailored cover letters, interview preparation questions, and track employer interview rounds (`EmployerInterviewEvent`).
- **Multi-Tenancy & Access Control:** Tenant-scoped data isolation, session authentication, Stripe subscriptions, and staff impersonation.

## Core Documentation

- [Site Functionality](site-functionality.md) - End-to-end user workflows, navigation, and page features.
- [Architecture](architecture.md) - Runtime stack, component boundaries, background processing, and Redis locks.
- [Data Model](data-model.md) - Domain entities, tenant ownership model, and relationships.
- [API Reference](api-reference.md) - HTML routes and Django Ninja JSON API surface.
- [Career Cockpit](career-cockpit.md) - Automated job task schedules, on-demand execution, cooldown lifecycle, and run inspection.
- [Job Sourcing & Scraping](job-sources-scraping.md) - Multi-channel scraping architecture, adapters, deduplication, and ranking.
- [Performance & Analytics](performance-and-analytics.md) - Momentum dashboard, conversion metrics, and employer interview tracking.
- [Operations](operations.md) - Environment configuration, Redis setup, background workers, tests, and operational practices.
- [Agent Guide](agent-guide.md) - Engineering conventions and rules for AI agents modifying this codebase.
- [Docker Deployment](DOCKER.md) - Containerized multi-service deployment.
- [Search Profile Consolidation](search-profile-consolidation.md) - Transition roadmap from legacy Tracks to Search Profiles.
- [SaaS Readiness Analysis](saas_readiness_analysis.md) - Architectural audit of multi-tenancy, billing, and operational readiness.

### Subsystem Detail Notes
- Optimizer detail: [`django_project/resume_app/docs/OPTIMIZER_PAGE.md`](../django_project/resume_app/docs/OPTIMIZER_PAGE.md) - Token budgets, single-pass graph, and hybrid local Ollama judge routing.
- LLM gateway detail: [`django_project/resume_app/docs/LLM_GATEWAY.md`](../django_project/resume_app/docs/LLM_GATEWAY.md) - Invocation pipeline, token budgets, rate limits, and fallback policies.
- Huey background tasks: [`django_project/resume_app/docs/HUEY_TASKS.md`](../django_project/resume_app/docs/HUEY_TASKS.md) - Comprehensive task signatures, scheduling, and side effects.

## Fast Orientation

The Django project root is `django_project/`; the main product app is `django_project/resume_app/`. Central routing is defined in `django_project/core/urls.py`. Server-rendered views reside primarily in `resume_app/views.py` and `pipeline_board.py`. REST API routers reside in `api.py` and `jobs_api.py`. Huey background workers and scheduled managers reside in `tasks.py`.

Always use the project's virtual environment for running commands. From the repository root on Windows:
```powershell
& "D:\Workshop\JobApp-Main\env\Scripts\python.exe" manage.py <command>
```
