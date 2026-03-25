# Job Search Page — Functionality & Filtering Logic

This document describes the Job Search tab: what it does, how results are fetched, and **how jobs are filtered** so you can review the pipeline.

---

## 1. Page overview

- **URL:** `/jobs/search/`
- **View:** `resume_app.views.job_search_view`
- **Template:** `resume_app/templates/resume_app/jobs_search.html`

The page has three main modes (tabs):

| Tab        | Source                    | Content |
|-----------|---------------------------|--------|
| **Results**   | External job API + DB     | Jobs for the current search query, after all filters and ranking. |
| **Favourites**| `SavedJobListing`         | Jobs the user has saved. |
| **Excluded**  | `DislikedJobListing`      | Jobs the user has disliked (hidden from Results). |

Additional UI:

- **My matches** (sidebar): `JobMatchResult` rows — jobs that have been run through a fit-check (e.g. from Keyword search), with score and status.
- **Disqualifiers**: User-defined phrases; jobs whose description contains any of these are excluded from Results (see below).
- **Disqualifier prompt**: After disliking a job, the user can add a phrase from that job as a disqualifier.

---

## 2. When Results are loaded

- **Results tab** is shown when `view=results` (default) and a **search term** `q` is present.
- The view calls `api_jobs_search(request, payload)` with:
  - `search_term` = `q`
  - `location` = location input
  - `results_wanted` = 50
  - `resume_id` = selected resume (or None for “Latest”).
  - `sort` = `focus` (aspirational) or `resume` (safe bets; by resume match %).

If the user clicks **Refresh results** (adds `?refresh=1`), the session cache is cleared and the next load triggers a **new fetch** from the external job service. Otherwise, the same search params may be served from **session cache** (see below).

---

## 3. Filtering logic (order of operations)

Filtering happens inside `jobs_api.jobs_search`. Jobs are processed in a fixed order; **each job is dropped as soon as it fails any step**. The order below is the actual pipeline.

### 3.1 Data used for filtering (computed once per request)

- **`disliked_listing_ids`**  
  All `job_listing_id` in `DislikedJobListing` (global list of disliked jobs).

- **`disqualifier_phrases`**  
  All phrases from `UserDisqualifier` (user’s “avoid these phrases” list). Stored normalized (lowercase, collapsed spaces).

### 3.2 Per-job filters (hard exclusions)

For each candidate job, the following checks are applied **in this order**. If any is true, the job is **excluded** and does not appear in Results.

1. **Disliked**  
   - **Condition:** `job.id in disliked_listing_ids`.  
   - **Effect:** Any job the user has ever disliked is excluded from Results (and shown under the Excluded tab instead).  
   - **Rationale:** “Don’t show this job again.”

2. **Disqualifier phrase match (whole-word)**  
   - **Condition:** `_job_matches_disqualifiers(job, disqualifier_pattern)` is true.  
   - **Implementation:** All user phrases are combined into a **single compiled regex** `\b(?:p1|p2|...)\b` (case-insensitive) once per request. Each job is then tested with one `pattern.search(description)` so the regex engine does a single pass per job (O(1) per job), not one search per phrase.  
   - **Rationale:** Substring matching would over-block (e.g. “sales” in “Salesforce”). Whole-word matching avoids that; a combined pattern avoids 1,000+ regex calls when you have many phrases and many jobs.  
   - **Effect:** User-defined words/phrases filter out jobs that contain them as distinct words.

Jobs that already have a `JobMatchResult` for the selected resume are **not** hidden; they stay in the list so the user can see both Focus % and Resume % and sort by “Safe bets” if desired.

Only jobs that pass **both** checks above are kept and then **ranked** (see below). There is no “soft” filter; these are hard exclusions.

---

## 4. Session cache (no re-fetch until refresh or new search)

- **Cache key (external fetch only):** `(search_term.strip(), location.strip())`.  
  **`resume_id` is not in the key.** The same (query, location) reuses cached refs when the user switches resume; only the local pipeline (filters + resume scoring + sort) is re-run.

- **On first search (or after `?refresh=1`):**  
  - Jobs are **fetched from the external service** (e.g. JobSpy) with a **fetch buffer** (`JOB_SEARCH_FETCH_BUFFER`, default 150) so that after hard exclusions (disliked, disqualifiers) we still have enough to fill the page.  
  - Each raw job is upserted into `JobListing` (by `source` + `external_id`).  
  - The **list of refs** `[{ "source", "external_id" }, ...]` is stored in **session** as `job_search_cache = { "params": cache_key, "refs": refs_for_cache }`.  
  - Then the two filters above are applied to build the result list. After ranking/sort, only the top **display limit** (`JOB_SEARCH_DISPLAY_LIMIT`, default 50) jobs are returned, so the UI gets a full page even for strict filters.

- **On subsequent loads (same search term and location, no refresh):**  
  - **No external fetch**, even if the user changed `resume_id` or `sort`.  
  - `job_search_cache["refs"]` is used to load `JobListing` rows from the DB.  
  - The **same two filters** are re-applied (disliked, disqualifiers). Resume-based scoring and sort are applied with the current `resume_id` and `sort`.

- **New search:**  
  Different `q` or `location` ⇒ different cache key ⇒ cache is not used ⇒ new external fetch and new cache.

- **Refresh:**  
  `?refresh=1` clears the cache so the next load triggers a new fetch.

---

## 5. After filtering: ranking and sort

Jobs that pass all filters get two scores:

- **Focus (aspirational):** Liked jobs’ embeddings as preference. **Title similarity** uses **job title only** (no company in the title vector), so the semantic space reflects role, not named entities. Role similarity is sentence-level vs. preferred “role” sentences from liked jobs. Combined: `alpha * title_sim + (1 - alpha) * role_sim` (default `alpha = 0.55`), with optional BM25 keyword boost. Each job gets **focus_percent** (0–100).
**Sort:** List is ordered by focus score (with optional penalty for similarity to disliked jobs).  

Filtering is entirely done by the two steps in §3.2; ranking/sort does not remove jobs.

---

## 6. Summary table (filtering only)

| Step | What is checked | Excluded when |
|------|------------------|----------------|
| 1    | Disliked list | Job id is in `DislikedJobListing` |
| 2    | Disqualifier phrases (whole-word) | Job description contains any `UserDisqualifier.phrase` as a **whole word** (word-boundary regex, case-insensitive) |

Only jobs that pass both are shown in Results. They are then scored (focus + resume match when resume selected) and sorted by the user’s chosen sort (Focus or Safe bets).

---

## 7. Where in the code

- **View (when to call search, refresh, tabs):** `resume_app/views.py` → `job_search_view`
- **Search + filtering + cache + ranking:** `resume_app/jobs_api.py` → `jobs_search`, `_get_disqualifier_phrases`, `_job_matches_disqualifiers`
- **Disqualifier model:** `resume_app/models.py` → `UserDisqualifier`
- **Disliked / saved / match lists:** `resume_app/models.py` → `DislikedJobListing`, `SavedJobListing`, `JobMatchResult`

This should be enough to review and adjust the filtering behaviour (e.g. add/remove steps or change order).
