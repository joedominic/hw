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
4. **Ollama Guard (New)**:
   - For the top 10 ranked results, the system runs a fast seniority and fit check via Local Ollama (Nemotron 4B).
   - Jobs that don't match the target seniority (e.g., Junior roles for a Principal track) are penalized.

### Ingestion
Jobs that pass filters are added to `PipelineEntry` with `stage="pipeline"` (or blank).

---

## 2. Vetting Stage (Fit Evaluation)

### Transition Triggers
- **Manual**: User clicks "Save" (favourite) on a job in the Pipeline board.
- **Automated**: The `pipeline_manager` task promotes jobs from Pipeline to Vetting if their `preference_margin` meets the threshold.

### Evaluation
Once in Vetting, the `evaluate_vetting_matching_task` is triggered:
1. **LLM-Based Cleansing**: The JD is sent to Local Ollama to extract core responsibilities and requirements, stripping all boilerplate.
2. **Matching**: It uses an LLM to run a **Matching Prompt** comparing the cleansed JD against the user's resume.
3. The LLM returns an **Interview Probability** (0-100) and **Reasoning**.

---

## 3. Applying Stage (Tailoring)

### Transition Triggers
- **Manual**: User clicks "Save" on a job in the Vetting board.
- **Automated (Fast-Track)**: High-confidence matches (Preference Margin > 50) are automatically promoted from Pipeline directly to Applying, skipping the Vetting stage.
- **Automated (Normal)**: Entries with an `interview_probability` >= threshold are promoted from Vetting to Applying.

### Optimization
In the Applying stage, the user can trigger the **Resume Optimizer**. The JD is again cleansed via LLM to ensure the Optimizer agents focus only on relevant information.

---

## 4. Done Stage (Completion)

### Transition Triggers
- **Manual**: User clicks "Save" on a job in the Applying board (indicating they have applied).
- **Automated**: None currently.
