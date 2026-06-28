# ResumeElite Knowledge Base

This directory is the agent-facing knowledge base for the site. Start here before changing product behavior, routes, data models, background jobs, or automation flows.

## What The Site Does

ResumeElite is a Django application for managing a job search end to end:

- Optimize resumes against job descriptions with a LangGraph writer/judge loop.
- Search and rank jobs from external sources, then manage them through a pipeline.
- Generate job prep artifacts such as cover letters and interview preparation.
- Run scheduled search, vetting, cleanup, and apply-agent work through Huey.
- Automate parts of job applications with a human-in-the-loop browser apply agent.
- Support multiple users through owner-scoped data, session auth, and staff impersonation.

## Documents

- [Site Functionality](site-functionality.md) - user-facing features and workflows.
- [Architecture](architecture.md) - framework, modules, routing, background work, and integrations.
- [Data Model](data-model.md) - domain entities, ownership, and relationships.
- [API Reference](api-reference.md) - HTML routes and JSON API surfaces.
- [Operations](operations.md) - local setup, environment, workers, tests, and gotchas.
- [Agent Guide](agent-guide.md) - conventions agents should follow when working in this repo.
- [Docker Deployment](DOCKER.md) - container-specific operations.

## Fast Orientation

The Django project lives in `django_project/`; the main app is `django_project/resume_app/`. Core routing is in `django_project/core/urls.py`. Server-rendered pages are mostly in `resume_app/views.py`, `pipeline_board.py`, and `apply_views.py`. JSON endpoints are in `api.py`, `jobs_api.py`, and `apply_api.py`. Long-running work is in `tasks.py`.

Use the repository virtual environment for Python commands. From the repo root on Windows, invoke `.\.venv\Scripts\python.exe`; from `django_project/`, invoke `..\.venv\Scripts\python.exe`.
