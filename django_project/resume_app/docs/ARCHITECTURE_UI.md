# UI Architecture: Server Views vs Django Ninja APIs

HireEdge utilizes two complementary presentation and API layers:

## 1. Server-Rendered HTML Views

- **Implementation:** Traditional Django class-based and function-based views returning HTML templates styled with Tailwind CSS (CDN).
- **Core View Modules:**
  - `views.py`: Main dashboard, settings, prompt library, LLM test bench, workflow management, track & search profile settings.
  - `pipeline_board.py`: Kanban board controllers for **Pipeline**, **Vetting**, **Applying**, and **Done** stages.
  - `apply_views.py`: Autonomous Apply Agent dashboard, profile management, and attempt review interface.
  - `auth_views.py`: Authentication flows (login, signup, logout), landing page, and legal terms/privacy.
  - `account_views.py`: Password reset flows, email verification, account data export (GDPR JSON), and account deletion.
  - `billing_views.py`: Subscription management, Stripe Checkout session redirects, and customer billing portal.
  - `staff_views.py`: Internal user administration and support tooling (`@user_passes_test(is_staff)`).
  - `onboarding_views.py`: First-time user setup wizard and checklist.
- **State Mutations:** Form submissions use standard **POST + redirect** patterns with Django messages framework (`django.contrib.messages`).

---

## 2. Asynchronous JSON APIs (Django Ninja)

Mounted under `/api/` using `NinjaAPI(auth=[django_auth, SessionOrApiKeyAuth()])`, supporting both active browser sessions and customer API keys (`CustomerApiKey`):

- **`api.py` (`/api/resume`):**
  - Multi-agent resume optimization triggers, polling (`/status/<id>`), single-step execution (`/run-step`), draft saving, and PDF/DOCX downloads.
  - LLM provider management, key validation (`/llm/connect`), model listings, and central LLM completions (`/llm/complete`).
  - Saved workflow CRUD endpoints.
- **`jobs_api.py` (`/api/resume/jobs`):**
  - Job search queries across aggregated boards, preference metrics calculations, and embeddings.
  - User feedback recording (like, dislike, save, hide) and whole-word phrase disqualifier CRUD.
  - Match debug analysis, AI fit checks, and batch pipeline resume summary triggers.
- **`apply_api.py` (`/api/resume/apply`):**
  - Application attempt management, dry-run approvals, manual URL overrides, and credential testing.

---

## UI / API Interaction Pattern

| Scenario | Architectural Approach |
|---|---|
| Full-page navigation, initial board loads, account settings, authentication | Django Views (`views.py`, etc.) + Server-rendered HTML |
| Asynchronous progress polling (Resume optimizer, apply agent steps) | Vanilla JS `fetch()` to Django Ninja endpoints |
| In-place board actions (Like, Dislike, Save, Stage Moves) | JavaScript `fetch()` calls to `/api/resume/jobs/*` |
| File downloads (Exported PDF/Word resumes, GDPR JSON export) | Authenticated `FileResponse` / `HttpResponse` handlers |
