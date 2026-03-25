# Resume Optimizer Page — Current Functionality

This document describes the Resume Optimizer page: layout, actions, and backend behaviour.

---

## 1. Page overview

- **URLs:** `/`, `/resume/optimizer/`
- **View:** `resume_app.views.optimizer_view`
- **Template:** `resume_app/templates/resume_app/optimizer.html`

**Layout:** Two columns — left: LLM config + Prompts; right: main workflow form, run-mode toggle, step-by-step card, and optimization status card.

---

## 2. Left column

### LLM configuration

- **Provider** dropdown (GET submit) and **Model** select (from the main form). Key status message: “Using connected key for X” or “No API key configured” with a **Go to Settings** link. API keys are managed on the **Settings** (Integrations) page, not on the optimizer.

### Prompts

- Writer, ATS judge, and recruiter judge prompt templates in a **tabbed** interface (one prompt at a time). “Edit in Prompt Library” links to the dedicated **Prompt Library** page for full-screen editing.
- Save prompts and Reset to server defaults; prompts are stored in session and used when running the optimizer or a step.

---

## 3. Right column

### 1. Upload resume & job description

- **Single form** (`action=run_optimizer`) with: resume file (or prefill from Match), job description textarea, model select, writer/ATS/recruiter prompt textareas, debug checkbox, rate limit delay, max iterations (1–5).
- **Run mode** toggle: “Full run” | “Step by step”. Full run shows “Run optimizer”; step by step shows the step-by-step card.
- **Run optimizer** (Full run): Submits the form. If a file is present, the form is submitted via AJAX to `/api/resume/optimize`; the page stays in place and the status panel shows progress and polls until completed/failed. If no file (e.g. prefill resume only), the form submits normally and redirects to the same page with `?resume_id=<id>`.
- **Optimization status** (card “3. Optimization status”): Always visible. When a run is active (from URL `resume_id` or from AJAX start), the body shows status, ATS/recruiter scores, token usage, optimized content (when completed), download Word/PDF, error message (when failed), and “Agent thoughts” accordion from `AgentLog`. Status is polled every 2s until completed or failed (no full-page reload when started via AJAX).

### 2. Step by step (when Run mode = Step by step)

- Step progress: 1. Writer → 2. ATS Judge → 3. Recruiter Judge.
- **Step** dropdown: Writer | ATS Judge | Recruiter Judge.
- For ATS/Recruiter: “Current resume draft” textarea (input for the judge). For Writer, the draft area is hidden.
- **Run step**: Runs the selected step via POST to `/api/resume/run-step`. Choose the next step from the dropdown and run again. Result area shows **Input** (collapsible prompt), **Output** (step result), and **Tokens** (input/output counts). When you re-run Writer after running the judges, their feedback is passed so the Writer can incorporate it.
- Errors show which step failed and the server/LLM error message when available.

---

## 4. Backend

### Full run

- **Task:** `resume_app.tasks.run_optimize_resume_task` (Huey) enqueues work that runs `optimize_resume_task`: writer → ATS judge → recruiter judge → conditional loop back to writer up to `max_iterations` or until average score ≥ 85.
- **API:** `POST /api/resume/optimize` (Form + file) creates `OptimizedResume`, enqueues the Huey task (Redis), returns `{ "task_id": "<id>", "resume_id": <id> }`.
- **Huey consumer:** Start a worker so tasks run: `python manage.py run_huey` (from `django_project/`). Redis must be reachable (default `192.168.2.174:6379`; override with `HUEY_REDIS_HOST`, `HUEY_REDIS_PORT`, `HUEY_REDIS_DB`).
- **Status:** `GET /resume/status/<resume_id>/` (Django view) or `GET /api/resume/status/<resume_id>` returns JSON: status, status_display, ats_score, recruiter_score, optimized_content, error_message, total_input_tokens, total_output_tokens, logs.

### Single step

- **API:** `POST /api/resume/run-step` invokes `writer_node`, `ats_judge_node`, or `recruiter_judge_node` from `resume_app.agents`.
- **Response:** `RunStepResponse`: `step`, `output` (step-specific fields plus `debug_prompt`, `input_tokens`, `output_tokens` when available), and `error` on failure. Frontend uses `output.debug_prompt` for the Input section and displays token counts from `output.input_tokens` / `output.output_tokens`.

---

## 5. Related files

| Area   | File |
|--------|------|
| View   | `resume_app/views.py` — `optimizer_view`, `optimizer_status_view` |
| API    | `resume_app/api.py` — optimize, run_step, get_status |
| Task   | `resume_app/tasks.py` — `run_optimize_resume_task` (Huey), `optimize_resume_task` (sync) |
| Agents | `resume_app/agents.py` — writer_node, ats_judge_node, recruiter_judge_node |

---

## 6. Settings and Prompt Library

- **Settings** (`/settings/`, `settings_view`): Manage LLM provider API keys in one place. Each provider has an API key input and “Connect & save key”. Keys are validated and stored encrypted; used by the optimizer and other tools.
- **Prompt Library** (`/resume/prompts/`, `prompt_library_view`): Dedicated page to edit Writer, ATS Judge, and Recruiter prompt templates (tabbed). Save updates session `optimizer_prompts`; Reset loads server defaults. The optimizer links to it via “Edit in Prompt Library”.
