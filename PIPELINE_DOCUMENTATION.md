# AI Resume Optimizer: Job Pipeline Documentation

This document describes how job listings move through the various pipeline stages in the AI Resume Optimizer platform.

## Pipeline Overview
The system uses a four-stage pipeline to manage the lifecycle of a job application:
1. **Pipeline**: Initial landing area for fetched jobs.
2. **Vetting**: Jobs undergoing detailed AI-based fit evaluation.
3. **Applying**: Shortlisted jobs for which the user intends to tailor a resume and apply.
4. **Done**: Jobs where the application process is complete.

---

## 1. Pipeline Stage (Initial Ingest)

### Discovery
Jobs enter the system via **Job Search Tasks** (`JobSearchTask`). These tasks run on a cron schedule or are triggered manually ("Run now").

### Filtering & Ranking
During the fetch process (`run_job_search_core`):
1. **Deduplication**: Jobs are deduplicated based on source and external ID.
2. **Disqualifiers**: Descriptions are checked against user-defined disqualifier phrases.
3. **Vector Ranking**:
   - The system uses **SentenceTransformers** (`all-MiniLM-L6-v2`) to generate embeddings for the job title and description.
   - Jobs are compared against the user's "Liked" and "Disliked" job embeddings.
   - A **Preference Margin** is calculated (Like Similarity - Dislike Similarity).
   - Jobs with a margin < -5 are automatically marked as **Disliked** and excluded.

### Ingestion
Jobs that pass filters are added to `PipelineEntry` with `stage="pipeline"` (or blank).

---

## 2. Vetting Stage (Fit Evaluation)

### Transition Triggers
- **Manual**: User clicks "Save" (favourite) on a job in the Pipeline board.
- **Automated**: The `pipeline_manager` task promotes jobs from Pipeline to Vetting if their `preference_margin` meets the threshold defined in `AppAutomationSettings.pipeline_preference_margin_min` (requires `pipeline_to_vetting_enabled` to be true).

### Evaluation
Once in Vetting, the `evaluate_vetting_matching_task` is triggered:
1. It uses an LLM (configured in Settings or via local Ollama) to run a **Matching Prompt**.
2. It compares the job description against a snippet of the user's resume (truncated to ~8000 chars).
3. The LLM returns an **Interview Probability** (0-100) and **Reasoning**.
4. These results are stored directly on the `PipelineEntry`.

---

## 3. Applying Stage (Tailoring)

### Transition Triggers
- **Manual**: User clicks "Save" on a job in the Vetting board.
- **Automated**: If `vetting_to_applying_enabled` is true, entries with an `interview_probability` >= `vetting_interview_probability_min` are automatically promoted by the `apply_vetting_to_applying_promotions` task.

### Optimization
In the Applying stage, the user can trigger the **Resume Optimizer** (`enqueue_applying_resume_optimization_task`):
1. This starts a multi-agent LangGraph workflow (Writer -> ATS Judge -> Recruiter Judge).
2. It iteratively tailors the resume to the specific job description.
3. Progress and results (Optimized Resume) are linked back to the `PipelineEntry`.

---

## 4. Done Stage (Completion)

### Transition Triggers
- **Manual**: User clicks "Save" on a job in the Applying board (indicating they have applied).
- **Automated**: None currently.

---

## Maintenance & Cleanup

### `pipeline_manager` (Every 30 mins)
- Recomputes preference metrics for Pipeline-stage jobs if they are missing or stale (> 2 days).
- Purges jobs with a `preference_margin` < -2.
- Handles automated promotion to Vetting.

### `cleanup_manager` (Daily)
- **Deduplication**: Cross-stage deduplication to remove similar jobs across the pipeline.
- **Retention**: Hard-deletes (or marks as deleted) jobs that have been in a stage longer than the configured retention days (e.g., Pipeline: 2 days, Vetting: 6 days, Applying: 10 days).
- **Activity Check**: Best-effort check if the job URL is still active.
