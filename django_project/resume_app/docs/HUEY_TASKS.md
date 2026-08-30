# Huey Tasks — Scheduling, Inputs, and Side Effects

This document describes the main Huey task functions in `resume_app/tasks.py` that power:
- job search ingestion into the pipeline
- vetting matching (resume vs job description)
- pipeline metrics refresh (preference/focus scoring)
- optional pipeline de-duplication
- manual resume optimization runs

All tasks are defined in `resume_app/tasks.py` and use Django models from `resume_app/models.py`.

---

## 1. `optimize_resume_task(...)`

**Defined:** `resume_app/tasks.py` — `@db_task()`

**Signature (key params):**
- `user_id`: owner `User.id` (tenancy context)
- `resume_id`: `OptimizedResume.id`
- `job_description_id`: `JobDescription.id`
- `provider`: LLM provider name
- `api_key`: decrypted API key for the provider
- `model`: provider model (optional)
- `prompts`: optional prompt overrides (`writer`, `ats_judge`, `recruiter_judge`)
- `ats_judge_profile_id`: optional explicit ATS profile pk (else workflow default or global default profile)
- `debug`, `rate_limit_delay`, `max_iterations`, `score_threshold` (iteration/threshold params are retained for API compatibility; the default compiled graph is a single pass)
- `workflow_steps`, `loop_to` (`loop_to` ignored; custom `workflow_steps` run listed steps once in order)

**When it runs:**
- Queued when the user clicks **Optimize** / **Re-optimize** on the **Applying** board.
- Also queued by the `enqueue_applying_resume_optimization_task` helper.

**What it does:**
- Loads the `OptimizedResume` by `resume_id`.
- Parses the resume PDF text via `parse_pdf(...)`.
- Builds optimizer context via `optimizer_budget.build_optimizer_context_state()` (role-focused JD excerpt, truncated resume fields, char budgets).
- Runs LLM calls through **`resume_app.llm.invoke_llm_messages`**: preference order, job pinning (`job_cache_key=str(optimized_resume.id)`), rate-limit cooldowns, and **`AppAutomationSettings.stop_llm_requests`** (kill switch). Writer and judges are remote-first by default (`prefer_local=False`); set `OPTIMIZER_JUDGES_PREFER_LOCAL=True` to prefer local Ollama for ATS/Recruiter only.
- Builds a LangGraph workflow:
  - default is `writer` → `ats_judge` → `recruiter_judge` (single pass, then END)
  - or uses `workflow_steps` if provided (each listed step runs once in order)
- Streams node updates and writes progress to the DB:
  - updates `optimized_resume.status` to `STATUS_RUNNING`
  - updates `optimized_resume.status_display` as scores/drafting progress arrives
  - creates `AgentLog` rows for each completed logical node
- On completion:
  - writes `optimized_resume.optimized_content`
  - writes `ats_score` and `recruiter_score`
  - writes `optimizer_context_snapshot` from `optimizer_context_budget` (char counts for writer/judge JD and resume fields)
  - writes token usage:
    - `total_input_tokens`
    - `total_output_tokens`
  - sets `optimized_resume.status = STATUS_COMPLETED`
- On error:
  - sets `optimized_resume.status = STATUS_FAILED`
  - writes `error_message`
  - if auth error is detected (`is_auth_error`), clears the provider API key in `LLMProviderConfig`

**Returns:**
- On success: `{"status": "success", "resume_id": resume_id}`
- On cancelled: `{"status": "cancelled", "resume_id": resume_id}`
- On not found / error: `{"status": "error", ...}`

**LLM cost drivers:**
- Default run is **three LLM calls** (Writer + ATS + Recruiter), one pass. Hybrid routing sends judges to local Ollama when configured, leaving Writer on the paid/strong model. Token input is reduced by Writer prompt dedupe, role-focused JD slicing, and judge input caps (`OPTIMIZER_*` settings). See `resume_app/docs/OPTIMIZER_PAGE.md`.

---

## 2. `enqueue_applying_resume_optimization_task(user_id, pipeline_entry_ids, force_new=False)`

**Defined:** `resume_app/tasks.py` — `@db_task()`

**Signature:**
- `user_id`: owner `User.id`
- `pipeline_entry_ids`: list of `PipelineEntry.id`
- `force_new`: when false, avoids enqueuing if queued/running already exists

**When it runs:**
- Called by UI actions on the pipeline boards:
  - **Optimize** / **Re-optimize** (per-job)
  - **Optimize selected** (bulk)

**What it does:**
- Validates/normalizes `pipeline_entry_ids` to integers.
- For each entry id, calls `_enqueue_single_pipeline_resume_optimization(...)`.

**`_enqueue_single_pipeline_resume_optimization(...)` key guards:**
- Entry must still exist and be `removed_at__isnull=True`
- Entry must be in `PipelineEntry.Stage.APPLYING`
- If `force_new=False` and an `OptimizedResume` exists with status `QUEUED` or `RUNNING`, it skips
- Builds a `JobDescription` for the pipeline row
- Picks an appropriate `UserResume` for the entry’s `track` (track-specific, else latest overall)
- Resolves the active LLM provider config and decrypts the API key
- Determines effective workflow settings from `AppAutomationSettings` (`applying_optimizer_workflow`), including that workflow’s optional default `ats_judge_profile`
- Creates:
  - `JobDescription`
  - `OptimizedResume` (status `STATUS_QUEUED`)
- Enqueues `optimize_resume_task(...)` with `debug=True`

**Returns:**
- `{"status": "success", "results": [ ...per-entry dicts... ]}`
- or `{"status": "skipped", ...}` when no ids are provided.

---

## 3. `run_job_search_task(user_id, task_id)`

**Defined:** `resume_app/tasks.py` — `@db_task()`

**Signature:**
- `user_id`: owner `User.id`
- `task_id`: `JobSearchTask.id`

**When it runs:**
- Enqueued by the periodic scheduler `enqueue_due_job_search_tasks()`.
- Triggered when users click "Run Now" on a scheduled search task.

**What it does:**
- Uses a **per-tenant lock** (`job_search_task_running:u{user_id}`) so concurrent searches for different users run in parallel without blocking each other.
- Creates a `JobSearchTaskRun` row in `STATUS_RUNNING`.
- Calls `run_job_search_core(...)` to fetch external jobs, filter, and rank.
- Adds non-duplicate jobs to `PipelineEntry`.
- Runs post-search deduplication and persists `JobListingTrackMetrics`.
- Calls `apply_pipeline_auto_promotions()`.

---

## 4. `evaluate_vetting_matching_task(user_id, pipeline_entry_ids, llm_provider=None, llm_model=None, matching_prompt=None)`

**Defined:** `resume_app/tasks.py` — `@db_task()`

**Signature:**
- `user_id`: owner `User.id`
- `pipeline_entry_ids`: list of `PipelineEntry.id`
- optional LLM override: `llm_provider`, `llm_model`
- optional prompt override: `matching_prompt`

**When it runs:**
- Backfills vetting probability values via `process_user_vetting_matching_task(user_id)`.
- Runs immediately when pipeline rows are auto-promoted from Pipeline → Vetting.

**What it does:**
- Uses a **per-tenant lock** (`vetting_matching_task_running:u{user_id}`) so multiple users can evaluate vetting matches concurrently.
- Loads existing, active pipeline entries in VETTING stage for that user.
- Calls `run_matching(...)` through `resume_app.llm` and records interview probabilities.

---

## 5. `enqueue_due_vetting_matching_tasks()`

**Defined:** `resume_app/tasks.py` — `@db_periodic_task(crontab(minute="*/20"))`

**When it runs:**
- Every 20 minutes.

**Purpose:**
- Periodic dispatcher that fans out `process_user_vetting_matching_task(user_id)` to Huey workers for each active user.

---

## 6. `enqueue_due_job_search_tasks()`

**Defined:** `resume_app/tasks.py` — `@db_periodic_task(crontab(minute="*"))`

**When it runs:**
- Every minute.

**Purpose:**
- Finds all due `JobSearchTask` rows across users (up to batch limit `MAX_DUE_JOB_SEARCH_TASKS_PER_TICK = 50`), advances `next_run_at`, and enqueues `run_job_search_task(task.owner_id, task.id)` concurrently.

---

## 7. `mark_stale_job_search_runs_failed()`

**Defined:** `resume_app/tasks.py` — `@db_periodic_task(crontab(minute="*/15"))`

**Signature:**
- none

**When it runs:**
- Every 15 minutes.

**Purpose:**
- Marks job-search runs that have been stuck in `RUNNING` too long as `FAILED`.

**What it does:**
- Uses:
  - `JOB_SEARCH_RUN_STALE_MINUTES = 60`
- Finds `JobSearchTaskRun` rows:
  - `status = STATUS_RUNNING`
  - `started_at < now - 60 minutes`
- Updates them to:
  - `status = STATUS_FAILED`
  - `finished_at = now`
  - `error_message = "Run timed out ..."`

**Returns:**
- `None`

---

## 8. `pipeline_manager()`

**Defined:** `resume_app/tasks.py` — `@db_periodic_task(crontab(minute="*/30"))`

**Signature:**
- none

**When it runs:**
- Every 30 minutes.

**Purpose:**
- Maintain **Pipeline-stage** (`""` or `pipeline`) entries only: refresh stale preference/focus metrics, purge weak Pref margins, then optional auto-promote to Vetting.

**Profile scope:** iterates `profile_slugs_for_pipeline(user)` — union of Track + SearchProfile slugs plus any bucket with active pipeline entries or scheduled search tasks (covers SearchProfile-only slugs that have no legacy Track row).

**Note:** Scheduled search (`run_job_search_task`) already persists Fit/Pref at ingest; this task refreshes metrics older than `PIPELINE_MANAGER_STATS_MAX_AGE_DAYS` and handles profiles that did not run through a recent search.

**Per-stage age cleanup** (Pipeline, Vetting, Applying, Done by `PipelineEntry.added_at`) is handled by **`cleanup_manager()`** using **Settings → App automation → Cleanup Manager retention days** (`0` = skip that stage).

**Configuration (module constants in `tasks.py`):**
- `PIPELINE_MANAGER_STATS_MAX_AGE_DAYS` (default `2`) — rescale when metrics missing or `last_scored_at` older than this.
- `PIPELINE_MANAGER_PURGE_MARGIN_MAX` (default `-2`) — remove pipeline rows whose `preference_margin` for that track is **strictly less** than this (requires a metrics row with a non-null margin; NULL margins are not purged by this rule).
- `PIPELINE_MANAGER_BATCH_SIZE` (default `100`).

**What it does (per track, errors isolated per track):**
1. **Metrics:** gathers `job_listing_id` from active Pipeline-stage rows only (not saved-only listings), selects jobs needing scores, batches `persist_preference_metrics_for_jobs` → `JobListingTrackMetrics.update_or_create(...)`.
2. **Margin purge:** same stage filter; for job ids with `preference_margin < PIPELINE_MANAGER_PURGE_MARGIN_MAX`, removes entries via hard-delete unless the job has liked/disliked actions (then `mark_deleted()`).
3. **Promotion:** `apply_pipeline_auto_promotions()` (may enqueue `evaluate_vetting_matching_task` for newly promoted ids).

**Note:** Saved jobs that never appear on the Pipeline board are **not** refreshed by this task.

**Returns:**
- `None`

---

## 9. `dedupe_pipeline_jobs_task(track="*", stage="all", include_done=False)`

**Defined:** `resume_app/tasks.py` — `@db_task()`

**Signature:**
- `track`:
  - `*` or `all` means “all tracks”
  - else a single track slug
- `stage`:
  - `all` means pipeline + vetting + applying
  - or one of `pipeline`, `vetting`, `applying`, `done`
- `include_done`:
  - when `stage="all"`, also include Done

**When it runs:**
- Manual invocation from:
  - the app UI (settings “Deduplicate pipeline”)
  - or CLI / scripts
- Also, `run_job_search_task` calls the underlying dedupe immediately after a search completes, scoped to:
  - `stage="pipeline"`, `include_done=False`

**What it does:**
- Calls `resume_app.job_dedupe.dedupe_pipeline_entries(...)`.

**Core behaviour (implemented in `job_dedupe.py`):**
- Builds a fingerprint based on normalized:
  - job title
  - company name
  - description prefix (excludes location and URL)
- Groups duplicate `PipelineEntry` rows within the same track and chosen stage scope.
- Keeps the “winner” based on track metrics preference signals when available (then stable tie-breakers).
- Calls `PipelineEntry.mark_deleted(save=True)` for the losers.

**Returns:**
- The result dict from `dedupe_pipeline_entries(...)` (or an `{"status": "error", ...}` for invalid params).

---

## 10. `cleanup_manager()`

**Defined:** `resume_app/tasks.py` — `@db_periodic_task(crontab(minute="30", hour="1"))`

**Also exported as:** `cleanup_inactive_pipeline_entries_daily` (alias to the same function) for older imports.

**When it runs:**
- Once per day at 01:30 (server time).

**Purpose:**
- Board hygiene: dedupe across active stages, age-based purge per stage (Settings), then best-effort inactive posting check for Applying.

**Configuration (`AppAutomationSettings`, Settings → App automation):**
- `cleanup_pipeline_retention_days`, `cleanup_vetting_retention_days`, `cleanup_applying_retention_days`, `cleanup_done_retention_days` — remove rows in that stage with `added_at` older than N days; **`0` skips that stage**. Defaults for the first three are **`2` / `6` / `10`**; Done defaults to **`0`** (off) until you set it.

**What it does (in order):**
1. `dedupe_pipeline_entries(track_slug="*", stage="all", include_done=False)` — duplicate detection across Pipeline, Vetting, and Applying.
2. `apply_cleanup_retention_purge(cfg)` — per track and per stage, removes entries past retention (same hard/soft rule as other cleanups when liked/disliked).
3. `purge_inactive_pipeline_entries(limit=400)` from `resume_app.job_activity`:
   - checks active entries in **Applying** only (Pipeline and Vetting excluded)
   - visits each job URL (best effort)
   - soft-deletes rows that have clear “closed” signals (e.g. 404/410/451 or closed-apply wording)
   - keeps rows when status is unknown

**Status cache:**
- Writes `CLEANUP_STATUS_CACHE_KEY` with `dedupe_removed`, `dedupe_groups`, `retention_removed`, inactive-check counters, and `errors` (Huey monitor “Last cleanup”).

**Returns:**
- `None` (periodic maintenance task).

---

## 11. `apply_agent_heartbeat()`

**Defined:** `resume_app/tasks.py` — `@db_periodic_task(crontab(minute="*"))`

**When it runs:**
- Every 60 seconds. This is the **primary driver** of the Autonomous Apply Agent state machine.

**Gate:**
- No-ops unless `AppAutomationSettings.apply_agent_enabled` is true (Apply Agent → Profile & settings).

**What it does:**
- Finds every `ApplicationAttempt` in a non-terminal status (`ApplicationAttempt.ACTIVE_STATUSES`) and enqueues `run_apply_agent_step` for each. Because all progress is DB-persisted, a missed optimizer callback or a dead worker never strands an attempt — the next tick resumes it.

**Returns:**
- `{"status": "ok", "enqueued": <n>}` (or `None` when disabled).

---

## 12. `run_apply_agent_step(user_id, attempt_id)`

**Defined:** `resume_app/tasks.py` — `@db_task()`

**When it runs:**
- Enqueued by `apply_agent_heartbeat`, as an immediate fast-path when an attempt is started / approved / given an override URL, when an optimizer run completes for a waiting attempt (`nudge_apply_attempts_for_pipeline_entry`), and by self-chaining after a successful state transition.

**What it does:**
- Acquires a per-attempt cache lock (so the same attempt is never processed twice concurrently), then calls `apply_agent.orchestrator.advance_attempt(attempt_id, user_id=…)` to perform exactly one state-machine step:
  - `queued → optimizing → waiting_optimizer` (waits for `OptimizedResume`)
  - `→ resolve_and_detect` (live Playwright for aggregators by default; mock URL map when `APPLY_USE_MOCK_RESOLVER=True`; known ATS hosts short-circuit)
  - `→ dry_run_fill` (headless fill via a deterministic ATS adapter, or the browser-use generic agent for unknown ATS; captures a semantic answer key)
  - `→ awaiting_approval` (semi-auto) or `→ submitting` (graduated full-auto)
  - `submitting` is **atomic**: re-validation fill on a fresh form + Submit + success assertion (DOM confirmation and submit XHR) in one transaction, then `succeeded` + `PipelineEntry.mark_done()`.
- If status advanced into another active state, enqueues itself again so progress does not wait solely on the 60s heartbeat.
- Browser work runs inside an isolated `browser.new_context()` with a 30s default page timeout and a small concurrency semaphore (`APPLY_BROWSER_CONCURRENCY`, default 2). A crash/timeout mid-submit is recorded as `submit_ambiguous` and never auto-retried (double-apply risk).

**Configuration (`core/settings.py` / env):**
- `HUEY_NAME` (default `jobapp-main`) — unique Redis queue name per checkout.
- `APPLY_USE_MOCK_RESOLVER` (default `False`) — mock vs live URL resolution.
- `APPLY_BROWSER_CONCURRENCY` (default `2`) — max concurrent browser steps.
- `AppAutomationSettings.apply_*` — mode, allowed ATS, upload format, min optimizer score, full-auto graduation threshold.

**Returns:**
- `{"status": "ok"|"noop"|"skipped"|"error", "state": <status>}`.

---

## 13. `purge_generated_resumes_periodic()`

**Defined:** `resume_app/tasks.py` — `@db_periodic_task(crontab(minute="15", hour="*/6"))`

**When it runs:**
- Every 6 hours.

**Purpose:**
- Removes ephemeral generated `UserResume` PDFs older than `cleanup_generated_resume_retention_days` (from `AppAutomationSettings`) across all active users.

---

## 14. `pipeline_resume_llm_extract_task(run_dir_str)`

**Defined:** `resume_app/tasks.py` — `@db_task()`

**Signature:**
- `run_dir_str`: Filesystem path to the run metadata directory.

**When it runs:**
- Enqueued via `POST /api/resume/jobs/pipeline-resume-summary/start` for batch skill extraction across pipeline jobs.

**What it does:**
- Loads run metadata (`run_meta.json`), verifies `owner_id`, resolves LLM provider credentials.
- Executes batch skill extraction across pipeline jobs with rate-limiting, JSON parse retries, and optional keyword consolidation.

---

## Operational notes (re: LLM usage)

- The biggest periodic LLM load is typically:
  - `evaluate_vetting_matching_task`
  - (because it calls `run_matching` once per queued entry)
- Manual “resume tailoring” load is:
  - `optimize_resume_task`
- Job search ingestion is non-LLM (it fetches/filter/ranks jobs), but it can trigger vetting and metrics refresh later depending on enabled automations.

### Redis-backed RPM / TPM limits

- Implementation: `resume_app/llm/rate_limit.py`, enforced via `resume_app.llm.invoke_llm_messages` (optimizer, matching, insights, pipeline extract, etc.). Full gateway reference: [`LLM_GATEWAY.md`](LLM_GATEWAY.md).
- **Configuration (env / `core/settings.py`):**
  - `LLM_RATE_LIMIT_ENABLED` (default: `True`)
  - `LLM_RATE_LIMIT_FAIL_OPEN` (default: follows `DEBUG` — fail-closed in production; when true, Redis down / wait exceeded still allows the call)
  - `LLM_RATE_LIMIT_MAX_WAIT_SECONDS` (default: `120`)
  - `LLM_RATE_LIMIT_REDIS_URL` (optional; defaults to Huey Redis host/port/db)
  - `LLM_RATE_LIMIT_REDIS_DB` (default: same as `HUEY_REDIS_DB`)
  - Per-provider window limits: `LLM_RATE_LIMIT_BY_PROVIDER` — default includes **Groq** (`LLM_RATE_LIMIT_GROQ_RPM` / `_TPM`) and **OpenAI** (`LLM_RATE_LIMIT_OPENAI_RPM` / `_TPM`). Add other providers by extending the dict in settings.
  - Also: daily token budgets, per-user concurrency, and invoke timeouts — see `LLM_GATEWAY.md` and `operations.md`.
  - **Integrations UI:** On Settings → Integrations, each preference row can set optional **Rate limit RPM** and **Rate limit TPM** (set **both** or leave **both** blank). Limits apply to that provider + **Preferred model** when they match the live LLM call; if no row matches the model, a row with an **empty** Preferred model is used as a provider-wide fallback, then env defaults.
- Token usage for limiting is estimated before the call (chars/4) and reconciled from provider usage metadata when available.

### Prompt caching (Groq and similar)

- Prompts are split into **system** vs **user** templates where possible (see Prompt library / `prompts.py`) so static instructions stay in a stable prefix.
- For Groq, caching behavior is described in [Groq Prompt Caching](https://console.groq.com/docs/prompt-caching). Check logs for `llm_usage` lines reporting `cached_tokens` and approximate cache hit percentage when the provider returns usage details.
- **Troubleshooting:** If `cached_tokens` stays zero, verify the system block is identical across calls, avoid putting timestamps or unique IDs in the system prompt, and keep tool/schema ordering stable when using tools.

