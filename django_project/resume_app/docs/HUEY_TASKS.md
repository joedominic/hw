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
- `resume_id`: `OptimizedResume.id`
- `job_description_id`: `JobDescription.id`
- `provider`: LLM provider name
- `api_key`: decrypted API key for the provider
- `model`: provider model (optional)
- `prompts`: optional prompt overrides (`writer`, `ats_judge`, `recruiter_judge`)
- `debug`, `rate_limit_delay`, `max_iterations`, `score_threshold`
- `workflow_steps`, `loop_to`

**When it runs:**
- Queued when the user clicks **Optimize** / **Re-optimize** on the **Applying** board.
- Also queued by the `enqueue_applying_resume_optimization_task` helper.

**What it does:**
- Loads the `OptimizedResume` by `resume_id`.
- Parses the resume PDF text via `parse_pdf(...)`.
- Runs LLM calls through **`resume_app.llm_gateway.invoke_llm_messages`**: preference order, job pinning (`job_cache_key=str(optimized_resume.id)`), rate-limit cooldowns, and **`AppAutomationSettings.stop_llm_requests`** (kill switch).
- Builds a LangGraph workflow:
  - default is `writer` → `ats_judge` → `recruiter_judge`
  - or uses `workflow_steps` if provided (requires support in `agents.py`)
- Streams node updates and writes progress to the DB:
  - updates `optimized_resume.status` to `STATUS_RUNNING`
  - updates `optimized_resume.status_display` as scores/drafting progress arrives
  - creates `AgentLog` rows for each completed logical node
- On completion:
  - writes `optimized_resume.optimized_content`
  - writes `ats_score` and `recruiter_score`
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
- This is the primary “full optimization” LLM workload. It can run multiple iterations depending on workflow graph settings.

---

## 2. `enqueue_applying_resume_optimization_task(pipeline_entry_ids, force_new=False)`

**Defined:** `resume_app/tasks.py` — `@db_task()`

**Signature:**
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
- Determines effective workflow settings from `AppAutomationSettings` (`applying_optimizer_workflow`)
- Creates:
  - `JobDescription`
  - `OptimizedResume` (status `STATUS_QUEUED`)
- Enqueues `optimize_resume_task(...)` with `debug=True`

**Returns:**
- `{"status": "success", "results": [ ...per-entry dicts... ]}`
- or `{"status": "skipped", ...}` when no ids are provided.

---

## 3. `run_job_search_task(task_id)`

**Defined:** `resume_app/tasks.py` — `@db_task()`

**Signature:**
- `task_id`: `JobSearchTask.id`

**When it runs:**
- Enqueued by the periodic scheduler `enqueue_due_job_search_tasks()`.
- Not called directly by users (the UI uses `JobSearchTask` + “Run now” which ends up enqueuing this).

**What it does:**
- Uses a global lock (`JOB_SEARCH_TASK_LOCK_KEY`) so only one job-search run executes at a time.
- Creates a `JobSearchTaskRun` row in `STATUS_RUNNING`.
- Calls `run_job_search_core(...)` to:
  - fetch external jobs
  - apply search-time filters/ranking logic
- For each returned job payload:
  - upserts pipeline membership by creating `PipelineEntry` when needed
  - if an entry was soft-deleted (`removed_at != null`), it does not re-add it
- **Post-search step:** runs de-duplication for the task track only:
  - calls `dedupe_pipeline_entries(track_slug=task.track, stage="pipeline", include_done=False)`

**Returns:**
- On success: `{"status": "success", "task_id": task_id}`
- On errors: `{"status": "error", ...}`
- On skip: `{"status": "skipped", ...}` when locked or task inactive.

---

## 4. `evaluate_vetting_matching_task(pipeline_entry_ids, llm_provider=None, llm_model=None, matching_prompt=None)`

**Defined:** `resume_app/tasks.py` — `@db_task()`

**Signature:**
- `pipeline_entry_ids`: list of `PipelineEntry.id`
- optional LLM override: `llm_provider`, `llm_model`
- optional prompt override: `matching_prompt`

**When it runs:**
- Backfills vetting probability values via `enqueue_due_vetting_matching_tasks()`.
- Runs immediately when pipeline rows are auto-promoted from Pipeline → Vetting (via `apply_pipeline_auto_promotions()`).

**What it does:**
- Uses a global lock (`VETTING_MATCHING_LOCK_KEY`) so only one vetting matching task executes at a time.
- Loads only existing, active pipeline entries:
  - `stage = PipelineEntry.Stage.VETTING`
  - `removed_at__isnull=True`
  - ids limited to `pipeline_entry_ids`
- Requires at least one row in `LLMProviderPreference` with a connected API key (same pool as the central LLM gateway).
- Calls `run_matching(..., llm=None, job_cache_key="vetting:<entry_id>")` so selection, pinning, and cooldowns go through **`resume_app.llm_gateway`**.
- Parses resumes:
  - for each `track` present, loads the latest `UserResume` for that track (fallback to latest overall)
  - uses only a snippet (capped by `RESUME_MATCHING_SNIPPET_CHARS`)
- For each entry:
  - skips if it was already evaluated for the same resume id (`vetting_interview_resume_id`)
  - skips if job description is empty
  - calls `run_matching(...)` (up to 3 attempts):
    - extracts `interview_probability` and `reasoning`
  - writes:
    - `vetting_interview_probability` (clamped to 0..100)
    - `vetting_interview_reasoning` (truncated to 2000 chars)
    - `vetting_interview_resume_id`
    - `vetting_interview_scored_at`
  - calls `apply_vetting_to_applying_promotions([entry.id])`

**Returns:**
- `{"status": "success", "updated": <int>, "skipped": <int>, "errors": <list>}`
- `{"status": "skipped", ...}` if no ids or no matching entries.
- `{"status": "error", ...}` if no LLM is configured.

**LLM cost driver:**
- This is the primary source of “vetting” LLM usage (resume vs job matching). If you see periodic LLM token spikes, this is usually the function being called.

---

## 5. `enqueue_due_vetting_matching_tasks()`

**Defined:** `resume_app/tasks.py` — `@db_periodic_task(crontab(minute="*/20"))`

**Signature:**
- none

**When it runs:**
- Every 20 minutes.

**Purpose:**
- **Vetting Manager** (Huey dashboard name): backfills vetting interview probability for entries missing it or evaluated against an outdated “latest resume” per track. Rows with short job descriptions are skipped inside `evaluate_vetting_matching_task`.

**What it does:**
- Limits work per tick:
  - considers the newest 200 candidate vetting entries
  - enqueues up to `max_to_enqueue = 20` entries per run
- Determines the latest resume id per track (fallback to latest overall)
- Builds `to_enqueue` list where:
  - `vetting_interview_probability is None` OR
  - `vetting_interview_resume_id != latest.id`
- Calls `evaluate_vetting_matching_task(to_enqueue, ...)` when non-empty.
- Finally calls `apply_vetting_to_applying_promotions()` (global promotion pass).

**Returns:**
- `None` (periodic tasks don’t rely on a return value).

---

## 6. `enqueue_due_job_search_tasks()`

**Defined:** `resume_app/tasks.py` — `@db_periodic_task(crontab(minute="*"))`

**Signature:**
- none

**When it runs:**
- Every minute.

**Purpose:**
- Finds due `JobSearchTask` rows (`next_run_at <= now`) and enqueues exactly one job-search task per tick to prevent overlap.

**What it does:**
- Queries:
  - `JobSearchTask.is_active=True`
  - `next_run_at` set and `<= now`
  - orders by `next_run_at`, then `start_time`
  - limits to 1 due task
- For the selected task:
  - computes `next_run_at` using `get_next_run_at(task.frequency, from_time=now)`
  - updates `task.next_run_at`
  - enqueues `run_job_search_task(task.id)`

**Returns:**
- `None`

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
- Maintain **Pipeline-stage** (`""` or `pipeline`) entries only: preference/focus metrics refresh, purge weak fits, then optional auto-promote to Vetting.

**Per-stage age cleanup** (Pipeline, Vetting, Applying, Done by `PipelineEntry.added_at`) is handled by **`cleanup_manager()`** using **Settings → App automation → Cleanup Manager retention days** (`0` = skip that stage).

**Configuration (module constants in `tasks.py`):**
- `PIPELINE_MANAGER_STATS_MAX_AGE_DAYS` (default `2`) — rescale when metrics missing or `last_scored_at` older than this.
- `PIPELINE_MANAGER_PURGE_MARGIN_MAX` (default `-2`) — remove pipeline rows whose `preference_margin` for that track is **strictly less** than this (requires a metrics row with a non-null margin; NULL margins are not purged by this rule).
- `PIPELINE_MANAGER_BATCH_SIZE` (default `100`).

**What it does (per track, errors isolated per track):**
1. **Metrics:** gathers `job_listing_id` from active Pipeline-stage rows only (not saved-only listings), selects jobs needing scores, batches `recompute_preferences_for_jobs`, `JobListingTrackMetrics.update_or_create(...)`.
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

## Operational notes (re: LLM usage)

- The biggest periodic LLM load is typically:
  - `evaluate_vetting_matching_task`
  - (because it calls `run_matching` once per queued entry)
- Manual “resume tailoring” load is:
  - `optimize_resume_task`
- Job search ingestion is non-LLM (it fetches/filter/ranks jobs), but it can trigger vetting and metrics refresh later depending on enabled automations.

### Redis-backed RPM / TPM limits

- Implementation: `resume_app/llm_rate_limit.py`, enforced in `resume_app/agents._llm_invoke_with_retry` (all paths that use it, including Job Search Insights).
- **Configuration (env / `core/settings.py`):**
  - `LLM_RATE_LIMIT_ENABLED` (default: `True`)
  - `LLM_RATE_LIMIT_FAIL_OPEN` (default: `True` — if Redis is down or the wait budget is exceeded, the call is still allowed; watch logs)
  - `LLM_RATE_LIMIT_MAX_WAIT_SECONDS` (default: `120`)
  - `LLM_RATE_LIMIT_REDIS_URL` (optional; defaults to Huey Redis host/port/db)
  - `LLM_RATE_LIMIT_REDIS_DB` (default: same as `HUEY_REDIS_DB`)
  - Per-provider window limits: `LLM_RATE_LIMIT_BY_PROVIDER` — default includes **Groq** via `LLM_RATE_LIMIT_GROQ_RPM` (default `30`) and `LLM_RATE_LIMIT_GROQ_TPM` (default `6000`). Add other providers by extending the dict in settings.
  - **Integrations UI:** On Settings → Integrations, each preference row can set optional **Rate limit RPM** and **Rate limit TPM** (set **both** or leave **both** blank). Limits apply to that provider + **Preferred model** when they match the live LLM call; if no row matches the model, a row with an **empty** Preferred model is used as a provider-wide fallback, then env defaults.
- Token usage for limiting is estimated before the call (chars/4) and reconciled from provider usage metadata when available.

### Prompt caching (Groq and similar)

- Prompts are split into **system** vs **user** templates where possible (see Prompt library / `prompts.py`) so static instructions stay in a stable prefix.
- For Groq, caching behavior is described in [Groq Prompt Caching](https://console.groq.com/docs/prompt-caching). Check logs for `llm_usage` lines reporting `cached_tokens` and approximate cache hit percentage when the provider returns usage details.
- **Troubleshooting:** If `cached_tokens` stays zero, verify the system block is identical across calls, avoid putting timestamps or unique IDs in the system prompt, and keep tool/schema ordering stable when using tools.

