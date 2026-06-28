# Agent Guide

This guide tells coding agents how to use the knowledge base and where to look before changing the site.

## Start Here

Before editing behavior, read:

1. `docs/README.md`
2. The most relevant domain document:
   - `docs/site-functionality.md`
   - `docs/architecture.md`
   - `docs/data-model.md`
   - `docs/api-reference.md`
   - `docs/operations.md`
3. The source files named by the relevant document.

## High-Value Entry Points

- Routing: `django_project/core/urls.py`
- Settings: `django_project/core/settings.py`
- Models: `django_project/resume_app/models.py`
- Tenancy helpers: `django_project/resume_app/tenancy.py`
- HTML views: `django_project/resume_app/views.py`
- Pipeline views: `django_project/resume_app/pipeline_board.py`
- Apply-agent views: `django_project/resume_app/apply_views.py`
- Resume API: `django_project/resume_app/api.py`
- Jobs API: `django_project/resume_app/jobs_api.py`
- Apply API: `django_project/resume_app/apply_api.py`
- Background tasks: `django_project/resume_app/tasks.py`
- Resume optimizer graph: `django_project/resume_app/agents.py`
- LLM gateway: `django_project/resume_app/llm_gateway.py`
- Apply-agent orchestrator: `django_project/resume_app/apply_agent/orchestrator.py`

## Coding Rules To Preserve

- Use the repo virtual environment for Python commands.
- Keep user data owner-scoped with `for_user()` and `get_owned_or_404()`.
- Treat `JobListing` as shared and put user state in owned related models.
- Keep long-running work in Huey tasks with durable DB state.
- Route LLM calls through `llm_gateway.py`.
- Route apply automation through the apply-agent state machine.
- Match existing Django templates, forms, views, Ninja routers, and model patterns before adding new abstractions.
- Add or update tests when changing auth, tenancy, background work, LLM behavior, pipeline transitions, or apply-agent state.

## Documentation Maintenance

When behavior changes, update the relevant file under `docs/` in the same change:

- Product workflow change: `site-functionality.md`.
- Route or API change: `api-reference.md`.
- Model, ownership, or migration change: `data-model.md`.
- Setup, env, worker, deployment, or test change: `operations.md`.
- Cross-cutting architectural change: `architecture.md`.

Keep docs concise and practical. Prefer naming the route, model, task, and source file over broad prose.
