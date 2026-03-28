## Codebase Simplification Document

This document catalogs redundancies and simplification opportunities in the job search and pipeline stack, and suggests concrete refactors. It is organized by impact and risk.

---

## High Impact — Logic Bugs / Correctness

### 1. Duplicate ranking logic across two files

- `jobs_api.py` `jobs_search` (cache path) has ~100 lines of inline ranking (BM25, cosine, margin, auto-dislike, penalty sort).
- `job_search_core.py` `_rank_jobs_with_meta` has the same pipeline for the non-cache path.
- **Constraint:** The preference scoring derived from likes/dislikes (cosine similarity against liked/disliked centroids → `preference_margin_percent`) is the primary sort signal and **must be preserved exactly**. BM25 is additive/secondary. The disliked-similarity penalty is also retained. The goal is to eliminate duplicate code paths only — do not alter the scoring math or weights.
- **Refactor:** Consolidate all ranking into a single function in `job_search_core.py` (e.g. `_rank_jobs_with_meta`), and have the cache path in `jobs_api.py` delegate to it instead of reimplementing the logic.

### 2. DB mutation inside a ranking function

- `job_search_core.py` `_rank_jobs_with_meta` creates `JobListingAction.DISLIKED` records and calls `invalidate_preference_cache()` mid-loop.
- A function named `_rank_*` should not write to the database; side-effects make ranking harder to reason about and test.
- **Refactor:** Extract the auto-dislike and cache invalidation behaviour into the caller (e.g. a separate step that runs over the ranked jobs) so the ranking function is pure (read-only).

### 3. `get_preference_vectors()` called twice in `jobs_search` cache path

- In `jobs_api.py` `jobs_search`, the cache path calls `get_preference_vectors()` twice (once early, once just before usage); the first result is discarded.
- **Refactor:** Call `get_preference_vectors()` once, bind the result to variables, and reuse them.

### 4. Hardcoded track slugs in cache invalidation

- `preference.py` `invalidate_disliked_embeddings_cache` explicitly deletes only `"ic"` and `"mgmt"` cache keys.
- This silently leaves cache entries stale for any new tracks created by the user.
- **Refactor:** Query all track slugs via `Track.objects.values_list("slug", flat=True)` and delete cache entries for each. Consider a single wildcard or versioned cache key scheme if supported.

### 5. Stale variable name: `role_sent_vecs_per_job` holds full-description vectors

- `job_search_core.py` unpacks `rank_jobs_by_preference` as `scores, title_vecs, role_sent_vecs_per_job`, but the third return value is actually full-description vectors.
- `job_ranking.py`’s docstrings also still refer to role-sentence vectors.
- **Refactor:** Rename the variable and documentation to `full_desc_vecs` (or similar) in both files to avoid confusion and subtle misuse.

---

## Medium Impact — Redundant Code

### 6. Legacy `_ic`/`_mgmt` score columns on `JobListing`

- `JobListing` still has six hardcoded fields: `pipeline_focus_percent_ic`, `pipeline_focus_after_penalty_ic`, `pipeline_margin_percent_ic` and the same for `_mgmt`.
- These are superseded by `JobListingTrackMetrics` (per `(job_listing, track)` row) introduced by migration `0021`.
- **Refactor:** Add a migration to drop the six legacy columns from `JobListing` and remove any remaining code that references them.

### 7. Duplicate periodic task bodies in `tasks.py` (resolved)

- Replaced by a single `pipeline_manager` periodic task (`resume_app/tasks.py`) scoped to Pipeline-stage rows only; see `docs/HUEY_TASKS.md`.

### 8. Duplicate `AgentLog` creation block in `optimize_resume_task`

- In `tasks.py`, the `AgentLog.objects.create(...)` call (with `step_display` name resolution) appears twice: inside the streaming loop and again after the loop completes.
- **Refactor:** Extract to a helper such as `_create_agent_log(resume, step, content, token_usage=None)` and call it from both sites.

### 9. Five near-identical `list_models_*` functions in `llm_services.py`

- `llm_services.py` defines:
  - `list_models_openai`
  - `list_models_anthropic`
  - `list_models_groq`
  - `list_models_google`
  - `list_models_ollama_cloud`
- Each function:
  - Imports the provider SDK.
  - Calls the provider’s “list models” endpoint.
  - Extracts model IDs.
  - Returns a sorted, truncated list.
  - Logs and re-raises on failure.
- **Refactor:** Introduce a generic helper, e.g. `_list_models_generic(fetch_fn, extract_fn)`, and implement provider-specific functions as small wrappers that pass SDK-specific lambdas into it.

### 10. Backwards-compat shims in `preference.py`

- `get_preference_vector(track)` returns only the liked centroid and is a thin wrapper around `get_preference_vectors(track)[0]`.
- `get_liked_jobs_with_embeddings(track)` is a thin alias around `get_liked_jobs_for_focus_reason(track)`.
- **Refactor:** If no external callers rely on these legacy names, remove them and have everything call `get_preference_vectors` / `get_liked_jobs_for_focus_reason` directly. Otherwise, mark them clearly as deprecated.

### 11. Track resolution pattern repeated in `jobs_api.py`

- The expression `(track or session_track or "").strip().lower() or Track.get_default_slug()` appears in `jobs_like`, `jobs_dislike`, and `jobs_save`.
- **Refactor:** Extract this logic to a helper in `jobs_api.py` (e.g. `_resolve_track(track, session_track)`) and reuse it.

### 12. `JobMatchPayload.thoughts` is always identical to `.reasoning`

- In `schemas.py`, `JobMatchPayload` has both `reasoning` and `thoughts`.
- All callers populate `thoughts` with `match_result.reasoning` (or `""`), making it redundant.
- **Refactor:** Remove the `thoughts` field from `JobMatchPayload` and simplify all serializers/callers to use `reasoning` only, updating any frontend references.

### 13. Pointless `run_optimize_resume_task` wrapper in `tasks.py`

- `run_optimize_resume_task` is a thin wrapper that only calls `optimize_resume_task(*args, **kwargs)`; its sole purpose is to carry the `@db_task()` decorator.
- **Refactor:** Move the `@db_task()` decorator directly onto `optimize_resume_task` and drop the wrapper, updating any references to call the decorated function.

---

## Low Impact — Templates / UI

### 14. `job_tasks.html` and `job_automation.html` duplicate the entire task table

- Both templates contain an identical `<table>` with a `{% for t in tasks %}` loop, including recent-runs sub-rows.
- **Refactor:** Extract the table markup into a reusable partial, e.g. `resume_app/templates/resume_app/_task_table.html`, and include it from both templates with `{% include "resume_app/_task_table.html" %}`.

### 15. Four `<form>` tags per job row in `pipeline.html`

- Each job row in `pipeline.html` uses four separate `<form method="post">` blocks for Save / Like / Dislike / Delete actions.
- This increases HTML size and complexity.
- **Refactor:** Consolidate actions using either:
  - A single-button AJAX pattern (as already used in `jobs_search.html`), or
  - A single form with a hidden `action` field and different submit buttons.

### 16. `job_task_create_view` / `job_task_edit_view` parse identical form fields

- Both views parse and validate `frequency`, `start_time`, `site_name`, and `jobs_to_fetch` in the same way.
- **Refactor:** Extract shared parsing and validation into a helper (e.g. `_parse_task_form(request)`) that returns cleaned data or raises `ValidationError`, and call it from both views.

---

## Minor — Naming / Dead Code

### 17. `pref_title == pref_full` — dead variable split in `job_ranking.py`

- `job_ranking.py` sets `pref_title, pref_full = prefs[0], prefs[0]`, so both variables refer to the same centroid.
- If separate title vs full centroids are not planned, this split is misleading.
- **Refactor:** Collapse to a single variable (e.g. `pref = prefs[0]`) and update call sites accordingly.

### 18. Unused `TokenUsageCallback` re-export in `llm_factory.py`

- `llm_factory.py` imports `TokenUsageCallback` from `callbacks.py` “for parity with existing imports”, but does not use it.
- **Refactor:** Verify callers; if nothing imports `TokenUsageCallback` via `llm_factory`, drop the re-export/import to reduce surface area.

### 19. `DEFAULT_MATCHING_PROMPT` dual import path

- `DEFAULT_MATCHING_PROMPT` is defined in `prompts.py`, re-exported from `agents.py`, and imported from `agents.py` in `jobs_api.py`.
- **Refactor:** Make `prompts.py` the single source of truth and import the constant directly from there in `jobs_api.py` (and elsewhere), removing the unnecessary re-export.

### 20. Stale comment in `tasks.py`

- `optimize_resume_task` has a comment stating “Runs in a plain thread (not Celery)” while this project uses Huey, not Celery.
- **Refactor:** Update or remove the comment so it reflects current architecture.

### 21. `logger.warning` for routine cache miss in `preference.py`

- A routine cold-start cache miss logs at `WARNING` level.
- **Refactor:** Downgrade to `INFO` or `DEBUG` to avoid noisy logs in normal operation.

### 22. `_get_llm_from_request` likely duplicated across `api.py` and `jobs_api.py`

- Both modules appear to implement similar logic to construct an LLM from the request and session.
- **Refactor:** Confirm duplication and, if present, consolidate into a shared helper (either in a shared module or a single API module) to keep behaviour consistent.

---

## Suggested Implementation Order

1. Items 3, 4, 5 — low-risk, high-correctness adjustments.
2. Items 7, 8, 9 — pure deduplication with no behaviour change.
3. Items 11, 13, 16 — small helper extractions.
4. Item 1 — ranking consolidation (highest risk; add or extend tests around job ordering).
5. Item 2 — extraction of DB mutation from the ranking function (ideally done with item 1).
6. Item 6 — database migration to drop legacy `JobListing` columns (irreversible; do last, after behaviour has been verified).
7. Items 10, 12, 14, 15, 17–22 — cosmetic and minor cleanups; can be done opportunistically.

