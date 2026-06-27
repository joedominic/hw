# Data Model

Most domain models live in `django_project/resume_app/models.py`. The app uses a shared job catalog with per-user ownership for resumes, preferences, pipeline entries, LLM settings, automation settings, and apply-agent state.

## Ownership Model

Use `resume_app.tenancy` helpers for tenant-safe access:

- `OwnedManager.for_user(user)` filters rows by `owner`.
- `get_owned_or_404(model, user, **lookup)` fetches owner-scoped rows safely.
- `api_user(request)` and `get_active_user(request)` resolve the tenant user.

Shared/global models:

- `JobListing` - deduplicated external job catalog.
- `JobDescription` - reusable job-description text for optimizer runs.

Per-user models include:

- `Track`
- `UserResume`
- `OptimizedResume`
- `PipelineEntry`
- `JobListingAction`
- `JobListingEmbedding`
- `JobListingTrackMetrics`
- `UserDisqualifier`
- `JobMatchResult`
- `JobSearchTask`
- `OptimizerWorkflow`
- `AtsJudgeProfile`
- `LLMProviderConfig`
- `LLMUsageByModel`
- `LLMUsageByQuery`
- `SiteCredential`
- `AtsAutoSubmitStats`

Per-user singleton-style models include:

- `UserPromptProfile`
- `AppAutomationSettings`
- `ApplicantProfile`
- `LLMAppUsageTotals`

Indirectly scoped models:

- `ResumeChunk` belongs to `UserResume`.
- `AgentLog` belongs to `OptimizedResume`.
- `JobSearchTaskRun` belongs to `JobSearchTask`.
- `ApplicationAttempt` belongs to `PipelineEntry`.
- `ApplicationAttemptStep` belongs to `ApplicationAttempt`.
- `LLMProviderPreference` belongs to `LLMProviderConfig`.

## Core Domains

### Identity And Staff

- `User` is Django's built-in auth user.
- `ImpersonationAuditLog` records staff hijack sessions, including hijacker, target, IP, reason, and end time.
- New users are seeded through `onboarding.py`.

### Tracks And Resumes

- `Track` separates job-search contexts.
- `UserResume` stores uploaded PDFs and may be tied to a track.
- `ResumeChunk` stores parsed resume text and embeddings for retrieval.
- `OptimizedResume` stores optimization status, generated content, scores, token usage, cover letter, and links to the source resume, job description, workflow, and optional pipeline entry.
- `AgentLog` records optimizer step output.

### Prompts And Workflows

- `UserPromptProfile` stores user-custom prompt text.
- `AtsJudgeProfile` stores reusable ATS judge prompt sets.
- `OptimizerWorkflow` stores configurable optimization steps and thresholds.

### LLM Configuration And Usage

- `LLMProviderConfig` stores encrypted provider keys and default provider/model settings.
- `LLMProviderPreference` stores ordered provider/model candidates and optional RPM/TPM limits.
- `LLMAppUsageTotals`, `LLMUsageByModel`, and `LLMUsageByQuery` track usage.

LLM calls should go through `llm_gateway.py` so usage, rate limits, provider preferences, and user stop controls are honored.

### Job Search And Pipeline

- `JobListing` stores external job metadata and is shared across users.
- `JobListingAction` stores per-user like, dislike, save, and applied actions.
- `JobListingEmbedding` and `JobListingTrackMetrics` support preference scoring.
- `UserDisqualifier` stores phrases that filter unwanted jobs.
- `PipelineEntry` stores a user's job pipeline state, scores, generated prep, and track.
- `JobMatchResult` stores resume-to-job fit checks.
- `JobSearchTask` stores scheduled search configuration.
- `JobSearchTaskRun` stores execution history and counts.

### Apply Agent

- `ApplicantProfile` stores form-fill profile data.
- `SiteCredential` stores encrypted login and session data for ATS domains.
- `ApplicationAttempt` is the resumable state machine for applying to a pipeline job.
- `ApplicationAttemptStep` records actions, screenshots, logs, and errors.
- `AtsAutoSubmitStats` tracks per-ATS safe-submit history.

## Pipeline Stages

`PipelineEntry.stage` uses these main stages:

- `pipeline`
- `vetting`
- `applying`
- `done`
- deleted/hidden states for cleanup and user actions

Stage transitions may be initiated by users or background managers. Applying-stage entries can start apply-agent attempts.

## Migration Context

Migration `0014_multi_tenant_owner.py` adds multi-tenant ownership to many existing models, backfills a bootstrap owner for old rows, creates impersonation audit logging, and seeds a Support group with the `can_impersonate_users` permission.

When adding models, decide explicitly whether the entity is shared globally, directly owner-scoped, or indirectly scoped through an owned parent.
