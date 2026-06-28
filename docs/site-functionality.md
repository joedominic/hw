# Site Functionality

ResumeElite is a multi-user job-search workspace. It combines resume optimization, job discovery, pipeline management, LLM-assisted preparation, and semi-automated application submission.

## Primary User Areas

### Resume Optimizer

Entry points: `/` and `/resume/optimizer/`

Users upload a resume PDF and paste a job description. The app creates an `OptimizedResume`, enqueues a Huey task, and runs a LangGraph loop:

1. Writer revises the resume for the target job.
2. ATS judge scores keyword and applicant-tracking fit.
3. Recruiter judge scores human readability and role fit.
4. The loop repeats until thresholds are met or iteration limits are reached.

The UI polls status, shows agent logs, and supports editing the generated draft. Completed resumes can be exported as PDF or DOCX. Users can also generate and save a cover letter for a completed optimization.

### Prompt Library And Workflows

Entry points: `/resume/prompts/` and `/workspace/workflows/`

Users can customize prompts for writer, judge, matching, insights, cover letter, and interview prep flows. ATS judge profiles capture reusable ATS scoring instructions. Optimizer workflows define the sequence of optimization steps and scoring thresholds.

### Job Search

Entry point: `/jobs/search/`

Users search external job boards, rank results, and take feedback actions. Search can use JobSpy, Dice, and Adzuna depending on settings and credentials. The app stores shared job listings globally while recording likes, dislikes, saves, matches, embeddings, and track metrics per user.

Important behaviors:

- Search results can be liked, disliked, saved, or marked applied.
- Disqualifier phrases filter jobs that should not enter the pipeline.
- Focus and preference scoring use embeddings and user feedback.
- AI match and insight flows compare jobs against selected resumes.

### Pipeline Boards

Entry points: `/jobs/pipeline/`, `/jobs/vetting/`, `/jobs/applying/`, `/jobs/done/`

The pipeline is a Kanban-style workflow backed by `PipelineEntry`.

- `pipeline`: new or saved jobs.
- `vetting`: jobs awaiting deeper resume/JD matching.
- `applying`: jobs ready for application work.
- `done`: applied or completed jobs.

Users can move, delete, bulk update, and enrich jobs. Background managers can evaluate fit, promote strong candidates, clean weak candidates, and generate interview preparation.

### Tracks And Resume Library

Entry point: `/jobs/tracks/`

Tracks separate job-search contexts such as IC, management, or custom searches. Each track can have library resumes and scoring context. New users are seeded with default tracks.

### Scheduled Job Automation

Entry point: `/jobs/automation/`

Users create `JobSearchTask` records with search terms, location, sites, schedule, and track. Huey periodically finds due tasks, fetches jobs, filters and ranks them, creates pipeline entries, and records each `JobSearchTaskRun`.

### Apply Agent

Entry points: `/jobs/apply-agent/`, `/jobs/apply-agent/<attempt_id>/`, `/jobs/apply-agent/profile/`

The apply agent automates parts of applying to jobs in the Applying stage. It stores applicant profile data, optional site credentials, and resumable `ApplicationAttempt` state.

Flow:

1. Start attempt from an Applying pipeline entry.
2. Generate or select an optimized resume.
3. Resolve the application URL and detect the ATS.
4. Fill a dry-run form using deterministic ATS adapters or generic browser-use automation.
5. Wait for user approval when required.
6. Submit or mark failed/rejected.

Known ATS adapters include Greenhouse, Lever, Ashby, iCIMS, and related flows. Unknown ATS flows require review and do not auto-submit.

### Settings

Entry point: `/settings/`

Settings cover LLM provider credentials, provider/model preferences, default models, usage totals, pipeline automation thresholds, apply-agent defaults, and LLM stop controls. API keys are stored encrypted per user.

### Staff Impersonation

Entry point: `/staff/users/`

Users with `resume_app.can_impersonate_users` can search active users and start django-hijack impersonation with a reason. The app records audit logs and shows a visible impersonation banner with release controls.

## Background Work

Huey powers long-running and scheduled work:

- Resume optimization.
- Job search task runs.
- Vetting match evaluation.
- Pipeline metric refresh and promotion.
- Generated-resume cleanup.
- Apply-agent heartbeats and browser steps.

Full functionality requires both the Django web process and a Huey worker unless `HUEY_IMMEDIATE=1` is used for local single-process testing.
