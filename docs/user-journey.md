# End-to-End User Journey Trace

This document traces a single job opportunity through its complete lifecycle in HireEdge: from initial career profiling and search ingestion to pipeline triage, in-funnel resume tailoring, and final interview outcome tracking.

---

## 1. Journey Architecture Overview

```mermaid
sequenceDiagram
    autonumber
    actor User as Candidate / Tenant
    participant Browser as Browser UI
    participant Web as Django Web Layer
    participant Worker as Huey Worker Daemon
    participant Redis as Redis Cache & Locks
    participant DB as MariaDB / MySQL

    Note over User,DB: Step 1: Profile & Target (Tab 1)
    User->>Browser: Upload resume at /jobs/profiler/
    Browser->>Web: POST /jobs/profiler/api/analyze/
    Web->>DB: Write UserResume, CareerProfileAnalysis
    Web-->>Browser: JSON {competencies, target_titles, salary_bands}
    User->>Browser: Provision profile "Python Lead"
    Browser->>Web: POST /jobs/profiler/api/provision/
    Web->>DB: Write SearchProfile(slug="py-lead", search_term="Senior Python")

    Note over User,DB: Step 2: Discovery & Ingestion (Tab 1 / Worker)
    alt Ad-hoc Discovery in Find jobs
        User->>Browser: Search "Python Lead" at /jobs/search/
        User->>Browser: Click "Save to Cockpit" on Acme Corp
        Browser->>Web: POST /api/resume/jobs/{id}/save?profile=py-lead
        Web->>DB: Write JobListingAction(action='saved'); new row stage='vetting'
    else Automated Background Sourcing
        Worker->>Worker: run_job_search_task(task_id)
        Worker->>DB: Write JobListing, PipelineEntry(stage='pipeline')
    end

    Note over User,DB: Step 3: Cockpit Triage & Deep Review (Tab 2)
    User->>Browser: Open Cockpit at /jobs/cockpit/?profile=py-lead
    User->>Browser: Focus card with J/K, press E (Move to Review)
    Browser->>Web: POST /jobs/cockpit/ (action="save")
    Web->>DB: move_to_vetting()
    Web->>Worker: Enqueue evaluate_vetting_matching_task
    Worker->>Redis: cockpit_llm_skill_radar cache
    Worker->>DB: Update PipelineEntry vetting_* columns
    User->>Browser: Press E to advance from Review to Applying
    Browser->>Web: POST /jobs/cockpit/ (action="save")
    Web->>DB: move_to_applying()

    Note over User,DB: Step 4: In-Funnel Resume Tailoring Studio
    User->>Browser: Press T on card
    Browser->>Web: GET /resume/optimizer/?job_id={id}&profile=py-lead&resume_id={r_id}
    User->>Browser: Click "Tailor Resume" (Full Run)
    Browser->>Web: POST /api/resume/optimize
    Web->>Worker: Enqueue optimize_resume_task
    Worker->>DB: Write OptimizedResume, AgentLog
    User->>Browser: Review and refine draft, press Ctrl+S ("Save Draft")
    Browser->>Web: POST /resume/optimizer/ (action="save_draft")
    Web->>DB: Update OptimizedResume.optimized_content [keeps stage='applying']
    User->>Browser: Click external "Employer Post", submit on company portal
    User->>Browser: Press Ctrl+Enter ("I Submitted This")
    Browser->>Web: POST /resume/optimizer/ (action="approve_and_mark_applied")
    Web->>DB: mark_done() sets stage='done', applied_at, applied resume snapshot
    Web->>DB: Write JobListingAction(action='liked')
    Browser-->>User: Esc returns to Cockpit (stage=done)

    Note over User,DB: Step 5: Tracker & Interview Outcomes (Tab 3)
    User->>Browser: Open Interview Prep Kit
    Browser->>Web: POST /api/resume/jobs/pipeline-entry/{id}/generate-interview-prep
    Web->>DB: Update PipelineEntry.interview_prep
    User->>Browser: Add recruiter screening interview
    Browser->>Web: Form submit (add interview event)
    Web->>DB: Write EmployerInterviewEvent(round_type='recruiter_call', status='scheduled')
    User->>Browser: View Momentum & Tracker at /performance/
    Browser->>Web: GET /performance/api/metrics/
    Web-->>Browser: JSON {active_streak: 6, weekly_goal_progress: "4/5"}
```

---

## 2. Detailed Step-by-Step Lifecycle

### Step 1: Target Definition (`/jobs/profiler/`)
- **User Action:** Candidate uploads their master resume PDF in the AI Resume Profiler.
- **View / Handler:** `resume_views.career_profiler_view` / `career_profiler_analyze_api`.
- **Endpoint:** `POST /jobs/profiler/api/analyze/`.
- **Database Rows Written:**
  - `UserResume`: Source resume text, uploaded file path, candidate name.
  - `CareerProfileAnalysis`: Extracted core competencies, executive summary, target job titles, and salary ranges stored in `analysis_data`.
- **Provisioning Profile:**
  - Candidate selects "Senior Python Lead" and provisions the search profile.
  - **Endpoint:** `POST /jobs/profiler/api/provision/`.
  - **Database Rows Written:**
    - `SearchProfile`: `name="Python Lead"`, `slug="py-lead"`, `search_term="Senior Python Engineer"`, `location="Remote"`, `resume=UserResume`.

---

### Step 2: Discovery & Ingestion
- **Pathway A (Find jobs):**
  - Candidate searches live listings at `GET /jobs/search/` (`jobs_search_view`).
  - Candidate clicks **Save to Cockpit** on Acme Corp "Senior Python Architect".
  - **Endpoint:** `POST /api/resume/jobs/{job_id}/save` (`jobs_save` in `jobs_api.py`), with `track` and/or `profile`.
  - **Database Rows Written:**
    - `JobListingAction`: `action="saved"`, `owner=request.user`.
    - `PipelineEntry`: new rows use `stage="vetting"`. An active row keeps its stage. There is no `is_saved` column.
- **Pathway B (Automated Huey Background Task):**
  - Huey worker executes `run_job_search_task(task_id)`.
  - Scrapes JobSpy, Greenhouse, BuiltIn, Levels.fyi, or Dice.
  - Deduplicates on `(source, external_id)`.
  - **Database Rows Written:**
    - `JobListing`: title, company, location, description, `url`. Salary is not a column.
    - `PipelineEntry`: `stage="pipeline"` (New). Scores land on `JobListingTrackMetrics`.
    - `JobSearchTaskRun`: Run metrics (`jobs_fetched`, `jobs_added_to_pipeline`).

---

### Step 3: Cockpit Triage & Deep Review (`/jobs/cockpit/`)
- **User Action:** Candidate loads the Career Cockpit: `GET /jobs/cockpit/?profile=py-lead` (`career_cockpit_view`). The backend also accepts `?track=`.
- **Advancement from New:**
  - Candidate navigates with `J`/`K` and presses `E` (Move to Review).
  - Form action submits `action="save"`.
  - **Database Rows Updated:** `move_to_vetting()` sets `stage='vetting'`. There is no `shortlisted_at`.
- **Review Stage Evaluation:**
  - **Candidate Match & Skill Radar:** Huey task `evaluate_vetting_matching_task` runs for Review cards. Structures Candidate Resume before Job Description to leverage Ollama KV prefix cache (~2.06s inference). Extracts match score, stretch skills, and fit summary.
  - **Cache & Persistence:**
    - Redis Cache: `cockpit_llm_skill_radar_{user_id}_{resume_id|none}_{job_id}` (7d TTL).
    - Database Fallback: `PipelineEntry.vetting_interview_probability = 85`, `PipelineEntry.vetting_interview_reasoning = "Strong Python and Kubernetes fit..."`.
- **Advancement to Applying:**
  - Candidate presses `E` (or `T`) on the Review card.
  - Form action submits `action="save"` again. `_apply_save_action` calls `move_to_applying()`.
  - **Database Rows Updated:** `PipelineEntry.stage = 'applying'`.

---

### Step 4: In-Funnel Resume Tailoring Studio (`/resume/optimizer/`)
- **User Action:** Candidate presses `T` (Tailor Resume) on the applying card.
- **View:** Navigates directly to:
  ```text
  GET /resume/optimizer/?job_id={job_id}&profile=py-lead&resume_id={resume_id}
  ```
- **Optimization Execution:**
  - Candidate triggers optimization: `POST /api/resume/optimize`.
  - Background Huey task `optimize_resume_task` executes 3-node LangGraph:
    1. *Writer Node:* Rewrites bullets matching Acme Corp's architectural requirements.
    2. *ATS Judge Node:* Evaluates keyword overlap (local Ollama or cloud).
    3. *Recruiter Judge Node:* Evaluates brevity and impact.
  - **Database Rows Written:**
    - `OptimizedResume`: body in `optimized_content`, plus `ats_score`, `recruiter_score`, cover letter, and `optimizer_context_snapshot`.
    - `AgentLog`: Node execution steps and raw judge feedback.
- **Draft Saving vs. Submission:**
  - **Save Draft:** Candidate refines draft and presses `Ctrl+S` (`POST /resume/optimizer/` with `action="save_draft"`). The card stays in `Applying` while exports (PDF/DOCX) are generated.
  - **Employer Submission:** Candidate clicks 1-click "Employer Post" external link, applies on the employer portal, and presses `Ctrl+Enter` ("I Submitted This").
  - **Endpoint:** `POST /resume/optimizer/` with `action="approve_and_mark_applied"`, which calls `mark_done()`.
  - **Database Rows Updated:**
    - `PipelineEntry`: `stage = 'done'`, `applied_at`, `applied_resume_markdown`, `applied_optimized_resume`.
    - `JobListingAction`: `action = "liked"` (auto-like). There is no `applied` action.
- **Escape Return:**
  - Pressing `Esc` in the optimizer activates the top action link (`id="back-to-cockpit-link"`), returning directly to `/jobs/cockpit/?track=py-lead&stage=done` (or `stage=applying` if saved as draft), dynamically scoped to the active profile slug.

---

### Step 5: Tracker & Interview Progression (`/performance/`)
- **Interview Preparation:**
  - Candidate clicks "Prep Kit" on the applied card in Cockpit.
  - **Endpoint:** `POST /api/resume/jobs/pipeline-entry/{id}/generate-interview-prep`.
  - **Database Rows Updated:** `PipelineEntry.interview_prep`.
- **Employer Interaction Logging:**
  - Recruiter reaches out; candidate logs phone screen:
  - **Database Rows Written:**
    - `EmployerInterviewEvent`:
      - `pipeline_entry = acme_entry`
      - `round_type = 'recruiter_call'`
      - `status = 'scheduled'`
      - `scheduled_at`, `duration_minutes`, `interviewer_names`, `location_or_link`, `notes`
- **Subsequent Interview Stages:**
  - `recruiter_call` → `hiring_manager` → `panel_interview` → `offer`, or `archived_rejected` / `withdrawn`.
  - **Database Rows Updated:** a new or updated `EmployerInterviewEvent` with `round_type` and `status` (`scheduled`, `completed`, or `cancelled`). There is no `completed_at` column.
- **Momentum & Outcome Tracking:**
  - Candidate visits `/performance/` (`performance_dashboard_view`).
  - Renders cached shell from Redis `perf_stats_{user.id}`.
  - Client hydrates `/performance/api/metrics/`.
  - Visualizes active streak counter, emerald weekly target progress bar (e.g. 5/5 target reached), and funnel velocity with zero punitive deficit shaming.
