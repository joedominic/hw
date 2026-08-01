# Resume Optimizer Page — Current Functionality

This document describes the Resume Optimizer page: layout, actions, and backend behaviour.

---

## 1. Page overview

- **URLs:** `/`, `/resume/optimizer/`
- **View:** `resume_app.views.optimizer_view`
- **Template:** `resume_app/templates/resume_app/optimizer.html`

**Layout:** Guided wizard — Setup (power users) → Input & upload → Optimization. The Input step mirrors the CareerFlow-inspired card layout: upload + job description side-by-side, collapsible supporting context, and a configuration card with workflow / ATS / run mode / primary CTA.

---

## 2. Left column

### LLM configuration

- **Provider** dropdown (GET submit) and **Model** select (from the main form). Key status message: “Using connected key for X” or “No API key configured” with a **Go to Settings** link. API keys are managed on the **Settings** (Integrations) page, not on the optimizer.
- **Cloud-only routing (optimizer graph):** Writer, ATS, Recruiter, and the in-graph `jd_cleanse` step require a remote/cloud LLM (`allow_local=False`). **Ollama Local is never used inside resume optimization.**
- **Local for everything else:** Pipeline JD cleanse (`JDCleanserService`) and vetting match use **Ollama Local**; other non-optimizer product LLM calls prefer local.

### Prompts

- Writer and recruiter judge prompts are system-wide (read-only preview in Advanced setup). **ATS judge** uses a **profile dropdown** (admin-managed global profiles); preview text updates when you change the selection.
- “Edit in Prompt Library” links to the dedicated **Prompt Library** page. ATS profiles are managed there (list + system/user templates); writer/recruiter/matching/etc. remain on the main library tabs.
- Prompts are not editable on the Optimizer page; staff edit them in the Prompt Library (`SystemPromptProfile`). Last-selected ATS profile id is stored in session (`optimizer_ats_judge_profile_id`).

---

## 3. Right column

### 1. Upload resume & job description

- **Single form** (`action=run_optimizer`) with: resume file (or prefill from Match), job description textarea, model select, **ATS profile** select, workflow preset, debug checkbox, rate limit delay, max iterations (1–5; see §4 — loop is not active in the default graph).
- **Run mode** toggle: “Full run” | “Step by step”. Full run shows “Run optimizer”; step by step shows the step-by-step card.
- **Run optimizer** (Full run): Submits the form. If a file is present, the form is submitted via AJAX to `/api/resume/optimize`; the page stays in place and the status panel shows progress and polls until completed/failed. If no file (e.g. prefill resume only), the form submits normally and redirects to the same page with `?resume_id=<id>`.
- **Optimization status** (card “3. Optimization status”): Always visible. When a run is active (from URL `resume_id` or from AJAX start), the body shows status, ATS/recruiter scores, token usage, and an **editable draft textarea** when the run completes. Use **Save draft** to persist edits; **Export PDF/Word** auto-saves the current editor text to the active `resume_id` first (so a stale prior-run save URL cannot apply), then downloads with cache-busting. While the run is still in progress, the draft is read-only until completion. Status is polled every 2s until completed or failed (no full-page reload when started via AJAX).

### 2. Step by step (when Run mode = Step by step)

- Step progress: 1. Writer → 2. ATS Judge → 3. Recruiter Judge.
- **Step** dropdown: Writer | ATS Judge | Recruiter Judge.
- For ATS/Recruiter: “Current resume draft” textarea (input for the judge). For Writer, the draft area is hidden.
- **Run step**: Runs the selected step via POST to `/api/resume/run-step`. Choose the next step from the dropdown and run again. Result area shows **Input** (collapsible prompt), **Output** (step result), and **Tokens** (input/output counts). When you re-run Writer after running the judges, their feedback is passed so the Writer can incorporate it.
- Errors show which step failed and the server/LLM error message when available.

---

## 4. Backend

### Default workflow (single pass)

The compiled graph runs **once**: **Writer → ATS judge → Recruiter judge → END**. There is no score-threshold loop back to Writer in the current graph (`create_workflow` / `create_workflow_from_steps` in `resume_app.agents`). Custom workflow step lists still run each listed step in order only (repeated step ids get separate nodes, but no automatic re-loop). Optional **`jd_cleanse`** (Prompt Library JD Cleanse templates) shrinks the posting and overwrites `job_description`, `writer_job_description`, and `judge_job_description` so later Writer/judge steps see the cleansed text.

**Important:** In the default order, ATS/Recruiter run *after* Writer, so their feedback is **not** an input to the first Writer call. Judge feedback only reaches Writer when:

1. **Step-by-step:** you re-run Writer and the UI/API passes prior judge feedback in `feedback`, or
2. **Custom workflow:** a step list places a judge *before* a later Writer (e.g. `recruiter_first`: Recruiter → Writer → ATS → Recruiter). `feedback` is accumulated with LangGraph `operator.add`.

**LLM calls per full run:** 3 cloud calls (Writer + ATS + Recruiter). Adding in-graph `jd_cleanse` adds one more **cloud** call. Pipeline enqueue / vetting may also run a separate **local** JD cleanse via `JDCleanserService`.

### Writer node inputs

`writer_node` builds prompt format kwargs from graph state (then fills the Writer system/user templates):

| Template field | Source |
|----------------|--------|
| `{resume_text}` | Document to edit this step: non-empty `optimized_resume` (prior draft) else `resume_text` |
| `{source_resume_text}` | Original PDF text (omitted when duplicate of body) |
| `{job_description}` | Role-focused JD slice (`writer_job_description`) |
| `{full_job_description}` | Full posting (omitted when already covered by role slice) |
| `{feedback}` | Joined `state.feedback` list (empty on first Writer in default graph) |
| `{optimized_resume}` | Prior draft string (may be empty) |
| `{optimization_notes}`, `{pipeline_skills_json}`, `{job_highlights}`, `{retrieval_context}` | Supporting context |

### Agent thoughts / LLM call debug (staff only)

Full prompts are product IP (“secret sauce”). The **Agent thoughts** panel is visible only when the *real* signed-in user is staff/superuser — including while django-hijack impersonating another account (`can_view_optimizer_llm_debug`). Regular users never receive prompt payloads in status JSON.

Each node’s `AgentLog.thought` stores (via a process-local side-channel merged when the Huey task writes the log — LangGraph stream updates often drop these keys):

- `debug_messages` — exact system/user messages sent to the LLM
- `debug_prompt` — flat text fallback of those messages
- `raw_llm_response` / `raw_llm_response_retry` — model output before score parsing
- `input_tokens` / `output_tokens` — per-step counts (headers like `N in · M out`; estimated when the provider omits usage)

UI renders three sections per step: **System Prompt**, **User Prompt**, **Raw Output** (plus token line).

### Token budgets

Context is built in `resume_app.optimizer_budget`:

| Field | Purpose |
|-------|---------|
| `writer_job_description` | Role-focused JD excerpt (semantic cleanse via `extract_role_description`, not prefix truncate) |
| `judge_job_description` | Same role slice fed to ATS/Recruiter (then capped again at invoke) |
| `resume_text` / `source_resume_text` | Writer edit body vs original PDF anchor |

**Writer dedupe:** When the source PDF text matches the document being edited, `{source_resume_text}` is omitted (placeholder note). When the full posting is already covered by the role slice, `{full_job_description}` is omitted.

**Judge caps:** Draft and JD are truncated with `OPTIMIZER_JUDGE_RESUME_MAX_CHARS` (default 12000) and `OPTIMIZER_JUDGE_JD_MAX_CHARS` (default 8000).

**Recruiter judge:** Uses the same unstructured single-call path as ATS (no structured-output retry doubling).

**Writer caps (existing):** `OPTIMIZER_WRITER_RESUME_MAX_CHARS`, `OPTIMIZER_SOURCE_RESUME_MAX_CHARS`, `OPTIMIZER_WRITER_JD_ROLE_MAX_CHARS`, context note/skill/highlight limits in `core/settings.py`.

### Full run

- **Task:** `resume_app.tasks.run_optimize_resume_task` (Huey) enqueues work that runs `optimize_resume_task`: writer → ATS judge → recruiter judge (single pass).
- **API:** `POST /api/resume/optimize` (Form + file) creates `OptimizedResume`, enqueues the Huey task (Redis), returns `{ "task_id": "<id>", "resume_id": <id> }`.
- **Huey consumer:** Start a worker so tasks run: `python manage.py run_huey` (from `django_project/`). Redis must be reachable (default `192.168.2.174:6379`; override with `HUEY_REDIS_HOST`, `HUEY_REDIS_PORT`, `HUEY_REDIS_DB`).
- **Status:** `GET /resume/status/<resume_id>/` (Django view) or `GET /api/resume/status/<resume_id>` returns JSON: status, status_display, ats_score, recruiter_score, optimized_content, error_message, total_input_tokens, total_output_tokens, logs.

### Single step

- **API:** `POST /api/resume/run-step` invokes `writer_node`, `ats_judge_node`, or `recruiter_judge_node` from `resume_app.agents`.
- **Response:** `RunStepResponse`: `step`, `output` (step-specific fields plus `debug_prompt`, `input_tokens`, `output_tokens` when available), and `error` on failure. Frontend uses `output.debug_prompt` for the Input section and displays token counts from `output.input_tokens` / `output.output_tokens`.

### Settings (env / `core/settings.py`)

| Setting | Default | Notes |
|---------|---------|-------|
| `OPTIMIZER_JUDGES_PREFER_LOCAL` | `False` | When True, ATS + Recruiter prefer local Ollama if an `is_local` provider is configured |
| `OPTIMIZER_JUDGE_RESUME_MAX_CHARS` | `12000` | Judge draft cap |
| `OPTIMIZER_JUDGE_JD_MAX_CHARS` | `8000` | Judge JD cap |
| `OPTIMIZER_USE_ROLE_SLICE_FOR_WRITER_JD` | `True` | Semantic role extract instead of prefix truncate |
| `OPTIMIZER_WRITER_JD_ROLE_MAX_CHARS` | `8000` | Max chars for role-focused JD |

Mark at least one Ollama integration row as **local** in Settings for hybrid judge routing to take effect.

**See also:** root knowledge base — `docs/site-functionality.md`, `docs/architecture.md` (Resume Optimizer section), `docs/operations.md` (optimizer env vars).

---

## 5. Related files

| Area   | File |
|--------|------|
| View   | `resume_app/views.py` — `optimizer_view`, `optimizer_status_view` |
| API    | `resume_app/api.py` — optimize, run_step, get_status |
| Task   | `resume_app/tasks.py` — `run_optimize_resume_task` (Huey), `optimize_resume_task` (sync) |
| Agents | `resume_app/agents.py` — writer_node, ats_judge_node, recruiter_judge_node, jd_cleanse_node |
| Budget | `resume_app/optimizer_budget.py` — role JD slice, dedupe helpers, judge caps |

---

## 6. Settings and Prompt Library

- **Settings** (`/settings/`, `settings_view`): Manage LLM provider API keys in one place. Each provider has an API key input and “Connect & save key”. Keys are validated and stored encrypted; used by the optimizer and other tools. Configure Ollama endpoints and set **local** for on-box judge routing.
- **Prompt Library** (`/resume/prompts/`, staff-only `prompt_library_view`): system-wide Writer, Recruiter, Matching, Insights, JD cleanse (tabbed) stored in `SystemPromptProfile`. **ATS judge profiles** are global (`owner=null`): create/rename/delete and edit system/user/combined templates. Saved workflows can set a default ATS profile for pipeline runs. Runtime uses **System + User** messages when splits are set (or code defaults). All users share the same prompts.
