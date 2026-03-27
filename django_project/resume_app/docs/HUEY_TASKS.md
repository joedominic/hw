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
- Instantiates an LLM (`get_llm(...)`).
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
- Resolves an LLM provider:
  - uses args if provided
  - else picks the top runtime provider candidate (`get_runtime_provider_candidates()`)
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
- Backfills vetting interview probability only for vetting entries missing it or evaluated against an outdated “latest resume” per track.

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

## 8. `refresh_pipeline_preferences_delta()`

**Defined:** `resume_app/tasks.py` — `@db_periodic_task(crontab(minute="*/30"))`

**Signature:**
- none

**When it runs:**
- Every 30 minutes.

**Purpose:**
- Computes preference/focus metrics only for jobs that do not yet have `JobListingTrackMetrics` for a given track.

**What it does:**
- For each `Track.slug`:
  - gathers job ids across:
    - pipeline entries in that track
    - saved jobs (saved `JobListingAction`)
  - filters to those missing metrics rows for that track
  - batches by 100
  - runs `recompute_preferences_for_jobs(jobs, track=track)`
  - creates `JobListingTrackMetrics` rows with:
    - `focus_percent`
    - `focus_after_penalty`
    - `preference_margin`
    - `last_scored_at`
  - (uses `bulk_create(..., ignore_conflicts=True)` so it doesn’t duplicate rows)
- Calls `apply_pipeline_auto_promotions()` at the end:
  - if enabled, promotes Pipeline → Vetting based on the refreshed preference margin

**Returns:**
- `None`

---

## 9. `refresh_pipeline_preferences_full()`

**Defined:** `resume_app/tasks.py` — `@db_periodic_task(crontab(minute=0, hour="0"))`

**Signature:**
- none

**When it runs:**
- Once per day at 00:00 (server time; Huey cron interpretation).

**Purpose:**
- Full recompute of metrics for all track jobs (pipeline + saved + anything that already has metrics),
  plus a purge pass for strongly negative jobs.

**What it does:**
- For each track:
  - gathers job ids via `_iter_track_job_ids(track, include_metrics=True)`
  - batches by 100
  - recomputes:
    - `focus_percent`
    - `focus_after_penalty`
    - `preference_margin`
  - upserts using `JobListingTrackMetrics.update_or_create(...)`
  - purges job listings whose `preference_margin < -2`:
    - finds active PipelineEntry rows in Pipeline (blank stage or Pipeline stage)
    - calls `entry.mark_deleted(save=True)`
- Calls `apply_pipeline_auto_promotions()` at the end.

**Returns:**
- `None`

---

## 10. `dedupe_pipeline_jobs_task(track="*", stage="all", include_done=False)`

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

## 11. `cleanup_inactive_pipeline_entries_daily()`

**Defined:** `resume_app/tasks.py` — `@db_periodic_task(crontab(minute="30", hour="1"))`

**When it runs:**
- Once per day at 01:30 (server time).

**Purpose:**
- Remove clearly inactive/closed jobs from active board stages, then clean duplicates.

**What it does:**
- Calls `purge_inactive_pipeline_entries(...)` from `resume_app.job_activity`:
  - checks active entries in Pipeline/Vetting/Applying
  - visits each job URL (best effort)
  - soft-deletes rows that have clear “closed” signals
    - e.g. HTTP 404/410/451
    - or page text like “no longer accepting applications”
  - keeps rows when status is unknown (network errors/timeouts)
- Then calls `dedupe_pipeline_entries(track_slug="*", stage="all", include_done=False)`.

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

