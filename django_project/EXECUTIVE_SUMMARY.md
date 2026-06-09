# Executive Summary — ResumeElite (JobApp-Jules)

## Purpose

AI-powered job-application platform that tailors resumes to job descriptions using a multi-agent LLM loop (Writer → ATS Judge → Recruiter Judge), ranks and filters job listings from external boards, and manages a Kanban-style application pipeline (Pipeline → Vetting → Applying → Done). Users upload PDFs, configure LLM providers, search jobs, vet matches, and run background resume optimizations.

## Architecture

**Pattern:** Django monolith with a single domain app (`resume_app`), dual HTTP surfaces, and async workers — not Clean/Hexagonal; boundaries are module-level conventions rather than strict ports/adapters.

| Surface | Role |
|---------|------|
| Server-rendered UI | `views.py`, `pipeline_board.py` + Django templates |
| JSON API | Django Ninja in `api.py` + `jobs_api.py` |
| Background work | Huey tasks in `tasks.py` (Redis-backed) |
| AI orchestration | LangGraph state machine in `agents.py` |

Long-running optimize and automation flows enqueue Huey jobs; the browser polls Ninja endpoints for status. Job search integrates **python-jobspy**; ranking uses local embeddings (sentence-transformers), BM25, and preference vectors derived from likes/dislikes.

## Tech Stack

| Layer | Choices |
|-------|---------|
| Runtime / framework | Python 3.12+, Django 5.2.11, Django Ninja 1.5.3 |
| Data / storage | SQLite (WAL + busy timeout for dev concurrency); `FileField` media uploads |
| Background queue | Huey 2.x + Redis (optional `HUEY_IMMEDIATE=1` for in-process dev) |
| AI | LangGraph, LangChain (OpenAI, Anthropic, Groq, Google GenAI, Ollama local/cloud) |
| Embeddings / NLP | sentence-transformers, rank-bm25, pdfplumber, python-docx |
| Job ingestion | python-jobspy, Playwright (transitive) |
| UI | Django templates + Tailwind CDN + vanilla `fetch()` (no HTMX) |
| Key libraries | django-environ, cryptography (Fernet API keys), redis, pandas/numpy |

## Folder Structure

```
JobApp-Jules/                 # Git root — requirements.txt, .env, docs
└── django_project/           # Django project (manage.py)
    ├── core/                 # settings, urls, WSGI/ASGI
    ├── resume_app/           # Sole custom app (~60 modules)
    │   ├── models.py         # 22 ORM models
    │   ├── views.py          # HTML pages
    │   ├── api.py            # Ninja: optimizer, LLM, workflows
    │   ├── jobs_api.py       # Ninja: search, pipeline, preferences
    │   ├── tasks.py          # Huey background tasks
    │   ├── agents.py         # LangGraph optimizer graph
    │   ├── job_*.py          # Search, rank, dedupe, sources
    │   ├── llm_*.py          # Factory, gateway, rate limits, session
    │   ├── templates/        # Server UI
    │   ├── management/       # CLI commands (dedupe, huey status, …)
    │   └── docs/             # ARCHITECTURE_UI, HUEY_TASKS, SIMPLIFICATION, …
    ├── scripts/              # Ad-hoc utilities
    └── media/                # Resumes + pipeline LLM artifacts (gitignored)
```

## DI & Configuration

- **Config:** `django-environ` reads `JobApp-Jules/.env` (parent of `django_project`); extensive tunables in `core/settings.py` (optimizer budgets, job-focus weights, Huey/Redis, LLM rate limits, pipeline LLM batching).
- **DI:** No formal DI container. Dependencies are resolved via Django settings, module-level factories (`llm_factory.get_llm`), and direct imports. LLM provider choice comes from `LLMProviderPreference` rows + per-request overrides.
- **Secrets:** Optional env API keys (`OPENAI_API_KEY`, etc.); user keys stored encrypted in DB via `crypto.py` (Fernet derived from `SECRET_KEY`). Optional `API_ACCESS_TOKEN` for Ninja auth.
- **Caching:** LocMem for preference vectors; Redis for Huey and optional LLM rate limiting.

## Testing

| Aspect | Detail |
|--------|--------|
| Framework | Django `TestCase` + `Client` in `resume_app/tests.py` |
| Scope | ~46 test methods across ~15 classes: models, PDF parse, API normalization, job-source mapping, keyword mining, pipeline promotions, mocked LLM/Huey paths |
| Gaps | No pytest/coverage config; no CI manifest in repo; LangGraph/LLM integration largely mocked; duplicate ranking logic documented as untested parity risk (`docs/SIMPLIFICATION.md`) |

Tests require full `requirements.txt` install (LangChain stack); not runnable in a bare Python env.

## Docker & Dev Workflow

- **No Dockerfile** or compose stack in repo.
- **Local dev:** `pip install -r requirements.txt` → `migrate` → `runserver` + `run_huey` (or `HUEY_IMMEDIATE=1` without Redis).
- **Defaults:** SQLite DB, Redis at `192.168.2.174:6379` for Huey (LAN IP — environment-specific).
- **Prod readiness:** README still references Streamlit at `ui/app.py` (missing); live UI is Django-only. No pinned multi-stage deploy, health checks, or production DB migration path documented.

## Strengths

- Clear dual-layer UI/API split with internal docs (`ARCHITECTURE_UI.md`, `ONBOARDING.md`, feature-specific guides).
- Rich domain model (tracks, pipeline stages, RAG resume chunks, LLM usage accounting, encrypted provider configs).
- Thoughtful LLM ops: rate limiting, token callbacks, context budgets, local hybrid retrieval for the writer.
- Substantial in-app test suite for core flows; management commands for ops (dedupe, queue status).
- Honest technical-debt catalog in `SIMPLIFICATION.md` (duplicate ranking, side effects in rankers).

## Red Flags

- **SQLite + Huey + runserver** — workable for solo dev with WAL pragmas, not production-scale; contention and backup story weak.
- **Hardcoded / LAN Redis default** — fragile for new contributors; no `.env.example` in repo.
- **Insecure dev defaults** — `DEBUG=True`, empty `ALLOWED_HOSTS`, fallback `SECRET_KEY` in settings if env unset.
- **Fully pinned `requirements.txt`** (~120 packages) without lockfile separation; includes heavy ML deps (torch, sentence-transformers) for all installs.
- **README drift** — Streamlit UI documented but absent; architecture docs are more accurate.
- **Duplicate / side-effecting ranking code** — correctness and maintenance risk called out in `SIMPLIFICATION.md`.
- **No Docker/CI** — deployment and automated test gates not defined in-repo.
- **Generated media in tree** — `media/pipeline_llm_extract/` artifacts may be committed accidentally.

---

*Generated from codebase scan — May 2026.*
