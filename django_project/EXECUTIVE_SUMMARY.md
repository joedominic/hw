# Executive Summary — HireEdge (JobApp-Main)

## Purpose

HireEdge is an AI-powered job application acceleration and optimization platform. It combines:
1. **Multi-Agent Resume Optimization (LangGraph):** Tailors uploaded resumes to target job descriptions through a multi-agent loop (Writer → ATS Judge → Recruiter Judge) with dense/lexical local RAG over resume chunks.
2. **Multi-Source Job Discovery & Ranking:** Aggregates listings from Indeed, LinkedIn (JobSpy), Dice, Levels.fyi, and BuiltIn, ranking results via sentence-transformer embeddings (`all-MiniLM-L6-v2`), BM25 keyword matching, preference centroids (likes/dislikes), and Ollama seniority checks.
3. **Four-Stage Kanban Pipeline:** Coordinates the job search lifecycle across **Discovered → Applying → Interview → Offer** with automated rule- and LLM-based promotions.
4. **Interview Prep & Cover Letter Engine:** AI-powered interview question coaching and role-specific cover letter generation tailored to the target job description.
5. **SaaS Monetization & Control:** Commercial billing via Stripe (Free, Pro, Unlimited), daily request/token quotas, hashed customer API keys, and staff impersonation (`django-hijack`).

---

## Architecture & Tech Stack

**Pattern:** Django monolith with a modular domain core (`resume_app`), dual HTTP surfaces (Django Templates + Django Ninja OpenAPI), async task consumers (Huey), and headless browser automation workers.

| Layer | Technologies |
|---|---|
| **Runtime & Framework** | Python 3.12+, Django 5.2.11, Django Ninja 1.5.3 |
| **Database & Storage** | SQLite (Dev with WAL) or MySQL / MariaDB (Production with TLS verification); local filesystem or AWS S3 (`django-storages` + `boto3`) |
| **Task Queue & Scheduler** | Huey 2.6+ backed by Redis (supports in-process `HUEY_IMMEDIATE=1` for dev) |
| **AI Orchestration** | LangGraph 1.0+, LangChain (OpenAI, Anthropic, Groq, Google GenAI, Ollama) |
| **NLP & Vectors** | `sentence-transformers` (`all-MiniLM-L6-v2`), `rank-bm25`, `pdfplumber`, `python-docx` |
| **Job Aggregation** | `python-jobspy`, custom JSON/REST clients (`dice_client`, `levels_client`, `builtin_client`) |
| **Browser Automation** | Playwright 1.49+, `browser-use` 0.11+ |
| **Frontend Surface** | Server-rendered Django Templates + Tailwind CSS CDN + vanilla JavaScript `fetch()` |
| **Security & Auth** | Django session auth, `LoginRequiredMiddleware`, `CustomerApiKey` auth, Fernet encryption (`crypto.py`), `django-hijack` |
| **Billing** | Stripe Subscriptions, Checkout, Billing Portal, Webhook idempotency |

---

## Data Model & Schema Overview

- **38 ORM Models** in `resume_app/models.py`.
- **31 Database Migrations** (`0001_initial` through `0031_optimizerworkflow_step_llm_config`).
- **Data Scoping:** User-level tenancy via `OwnedManager` (`.for_user(user)`).
- **Core Entities:**
  - **Account & Identity:** `User`, `UserExperienceSettings`, `ApplicantProfile`, `SiteCredential`, `ImpersonationAuditLog`, `CustomerApiKey`.
  - **Billing & Quotas:** `Plan`, `Subscription`, `UsageCounter`, `StripeWebhookEvent`.
  - **Resume & AI Optimization:** `UserResume`, `ResumeChunk`, `JobDescription`, `OptimizedResume`, `AgentLog`, `OptimizerWorkflow`, `AtsJudgeProfile`, `SystemPromptProfile`.
  - **LLM Provider Management:** `LLMProviderConfig`, `LLMProviderPreference`, `LLMAppUsageTotals`, `LLMUsageByModel`, `LLMUsageByQuery`.
  - **Job Search & Pipeline:** `JobListing`, `JobListingAction`, `JobListingEmbedding`, `JobListingTrackMetrics`, `UserDisqualifier`, `JobMatchResult`, `SearchProfile`, `JobSearchTask`, `JobSearchTaskRun`, `PipelineEntry`, `Track`.
  - **Apply Automation:** `ApplicationAttempt`, `ApplicationAttemptStep`, `AtsAutoSubmitStats`, `AppAutomationSettings`.

---

## Background Processing (Huey Tasks)

1. **Scheduled Maintenance:**
   - `enqueue_due_job_search_tasks` (1 min): Triggers scheduled searches based on cron expressions.
   - `apply_agent_heartbeat` (1 min): Drives asynchronous browser application attempts.
   - `mark_stale_job_search_runs_failed` (15 min): Cleans up hung worker tasks.
   - `enqueue_due_vetting_matching_tasks` (20 min): Batch evaluates job fit & interview probability.
   - `pipeline_manager` (30 min): Re-scores listings, enforces margin purges, auto-promotes stages.
   - `purge_generated_resumes_periodic` (6 hours): Removes ephemeral generated resumes.
   - `cleanup_manager` (daily at 01:30): Cross-stage deduplication, retention purge, inactive URL sweep.
2. **Async Task Execution:**
   - `optimize_resume_task`: Executes the multi-agent LangGraph resume optimizer.
   - `run_job_search_task`: Fetches, filters, ranks, and ingests jobs from multiple boards.
   - `run_apply_agent_step`: Advances a single state-machine step in a Playwright browser context.
   - `pipeline_resume_llm_extract_task`: Batched LLM extraction of skills and requirements.

---

## Strengths

- **Comprehensive End-to-End Workflow:** Unifies job discovery, AI fit vetting, resume tailoring, and autonomous form submission in a single platform.
- **Robust LLM Governance:** Centralized `llm_gateway` and `llm_policy` enforce token budgets, user concurrency caps, timeouts, rate-limiting cooldowns, and a global kill switch.
- **Hybrid Local/Remote Strategy:** Uses local Ollama for low-cost tasks (JD cleansing, seniority check, vetting match) while reserving frontier models for high-stakes resume writing.
- **Resilient Automation:** Apply agent state is persisted to database records on every step, enabling worker crash recovery without keeping persistent browser sessions in memory.
- **Enterprise Ops & Observability:** Detailed audit logs for support impersonation (`ImpersonationAuditLog`), apply steps (`ApplicationAttemptStep`), and token accounting by query kind/model.

---

## Architectural Vulnerabilities & Multi-Tenant Limitations

- **User-Only Tenancy (No Organization / Team Entity):** The tenancy boundary is strictly 1 user = 1 tenant. There is no concept of Organizations, Workspaces, Teams, shared pipelines, or multi-user access control.
- **Shared Task Queue & Worker Starvation:** A single Huey queue processes all workloads. High-volume scraping or bulk optimizations by one tenant can starve interactive jobs for other tenants.
- **Global Background Locks:** Certain tasks (e.g. `run_job_search_task`) use global Redis locks (`JOB_SEARCH_TASK_LOCK_KEY`) rather than tenant-scoped locks, causing concurrent user tasks to skip.
- **Shared IP / Scraping Block Risk:** Outbound JobSpy requests and Playwright browser apply steps originate from the host IP without tenant-isolated proxy rotation, creating shared IP block risks across all users.
- **Soft Application-Level Isolation:** Data isolation relies entirely on developers remembering to call `.for_user(user)` or `get_owned_or_404()`, lacking database-level Row Level Security (RLS) or schema separation.
- **Global Tables:** `JobListing` and `SystemPromptProfile` are global, preventing tenants from maintaining private job boards or organization-specific prompt templates.
