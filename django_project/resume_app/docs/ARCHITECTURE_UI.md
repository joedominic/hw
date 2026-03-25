# UI architecture: forms vs APIs

This app uses two complementary layers:

## Server-rendered pages (`views.py`)

- **What:** Django views return HTML templates. Most mutations use **POST + redirect** with flash messages (`django.contrib.messages`).
- **Where:** URLs wired in `core/urls.py` as named routes (e.g. `track_list`, `job_automation`, `resume_optimizer`).
- **Use for:** Full-page flows, track/resume management, automation task CRUD, settings, and anything that should work without JavaScript.

## JSON APIs (Django Ninja)

- **`api.py`** — Resume optimizer pipeline, LLM provider connect/list/default, workflow CRUD, prompt fetch/save, and related endpoints. Mounted under the app’s API prefix (see `core/urls.py`).
- **`jobs_api.py`** — Job search, embeddings/matching, likes/dislikes/saves, pipeline actions, focus breakdown, etc. Nested under `/jobs` relative to the main API router.

HTMX, `fetch`, or small scripts on templates call these endpoints; they return JSON (or file downloads where noted in route handlers).

## Rule of thumb

| Need | Use |
|------|-----|
| User submits a form and expects a full page refresh / next step | `views.py` + template |
| Partial update, async job status, list actions without navigation | `api.py` / `jobs_api.py` |

Effective LLM prompts can be stored per profile (see `prompt_store`); defaults live in `prompts.py` / agent constants.
