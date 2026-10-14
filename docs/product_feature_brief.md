> [!NOTE]
> This document is a high-level product brief and market positioning overview. For the authoritative behavioral specification of all screens, routes, and interactions, see [`site-functionality.md`](site-functionality.md).

---

## 1. The Problem

Job seekers spend 60–80% of their job hunt on low-leverage, fragmented tasks: manually checking multiple disconnected job boards, tailoring resumes in separate tools, losing track of application stages across spreadsheets, and guessing at hiring requirements.

Despite the proliferation of point-solution AI tools, the workflow remains disjointed: one tool rewrites a resume, another tracks submissions, and search aggregators bombard users with duplicate postings. Candidates experience severe cognitive fatigue from context-switching across browser tabs and punitive agile tracking dashboards.

---

## 2. What HireEdge Does

HireEdge is an **AI-powered job search and career acceleration operating system** that organizes the complete job search lifecycle around the **Stitch 3-Tab Model**:

- **Tab 1: Target & Discover (`/jobs/profiler/` & `/jobs/search/`):** AI Resume Profiler and Search Sandbox with 1-click `+ Add to Review` ingestion.
- **Tab 2: Career Cockpit (`/jobs/cockpit/`):** Central tri-pane triage workstation, slide-over Automation Drawer, deep LLM fit evaluation, and in-funnel resume tailoring return loop.
- **Tab 3: Tracker (`/performance/`):** Positive career momentum telemetry, weekly goals, active streaks, and employer interview tracking.

*(For detailed screen behaviors, keyboard shortcuts, and state transitions, see the authoritative specification in [`docs/site-functionality.md`](site-functionality.md)).*

---

## 3. Core Feature Set

### 🎯 Tab 1: AI Profiler & Multi-Source Search Sandbox
- **AI Resume Profiler:** Parses candidate resumes, evaluates core market strengths, and creates fine-tuned search profiles with targeted Boolean terms.
- **Multi-Source Sourcing:** Live listings across **Indeed, LinkedIn (via JobSpy), Greenhouse direct boards, BuiltIn, Levels.fyi, and Dice**.
- **Centroid Preference Learning:** User feedback (likes and dislikes) refines profile semantic embeddings over time.
- **Cross-Board Deduplication:** Duplicates across multiple boards collapse into a single normalized record.
- **One-Click Ingestion Bridge:** Any discovered role can be instantly sent to the Cockpit Review stage via `+ Add to Review`.

### 🕹️ Tab 2: Career Cockpit & Autonomous Search Automation
- **Unified Tri-Pane Kanban:** Manage jobs across `New`, `Review`, `Applying`, and `Applied`.
- **Slide-Over Automation Drawer:** Full automation controls (daily, weekdays, weekly, custom cron) kept cleanly out of the daily triage view.
- **On-Demand "Run Search Now":** Manual triggers with atomic Redis pending state and a 3-minute cooldown (180s limit).
- **Automatic Cooldown Reset on Failure:** Zero lingering lockout when an execution fails or times out.
- **Run Inspection Modal:** Granular ingestion disposition breakdowns (New, Reposted, Already in Pipeline, Expired, Eliminated) with CSV and JSON exports.
- **Deterministic Labor Compliance & Authenticity Auditor:** Evaluates listings in Review/Applying using 10 deterministic compliance checks, extracting posting dates and application lifespan deltas ($\le 4$ calendar days) to unmask fake listings, DOL ETA-9089 PERM testing notices, evergreen talent pools, and internal promotions.
- **Keyboard Triage Hotkeys:** Rapid navigation (`J/K`), stage advancement (`E/L`), and AI tailoring (`T`).

### ✍️ Multi-Agent AI Resume Optimization
- **3-Agent LangGraph Pipeline:**
  1. *Writer Node:* Tailors bullet points to highlight relevant impact for the target role.
  2. *ATS Judge Node:* Evaluates keyword coverage, formatting, and applicant tracking fit.
  3. *Recruiter Judge Node:* Evaluates executive readability, brevity, and business impact.
- **Hybrid Local LLM Routing:** Run ATS and Recruiter judges on local Ollama models (`OPTIMIZER_JUDGES_PREFER_LOCAL=True`) to achieve zero marginal cloud API cost on evaluation steps.
- **Context-Preserved Return Loop:** Escape back to Cockpit via `Esc` or approve draft and advance to Applied via `Ctrl+Enter`.
- **Artifact Export:** PDF and DOCX export with custom cover letter generator.

### 📈 Tab 3: Tracker & Real-World Interview Management
- **Positive Momentum Focus:** Eliminates guilt-inducing sprint deficit warnings in favor of positive streak counters and weekly goal bars.
- **Funnel Conversion Velocity:** Visual telemetry tracking stage advancement and submission volume.
- **Employer Interview Loops (`EmployerInterviewEvent`):** Explicit tracking for phone screens, technical interviews, panel rounds, offers, and rejections.
- **Instant Shell Render:** Redis-cached telemetry with progressive client-side hydration.

### ⚙️ Multi-Tenancy, Quotas & Subscriptions
- Multi-tenant architecture with owner-scoped data isolation and staff impersonation via `django-hijack`.
- Stripe billing integration with Free, Pro, and Unlimited plans enforcing daily LLM token and request quotas (`UsageCounter`).
- Bring-Your-Own-Key (BYOK) with encrypted credential storage (Fernet) alongside platform key fallbacks.
- Emergency automation kill-switch (`stop_llm_requests`) in settings.

---

## 4. Key Differentiators

| Feature | HireEdge | Typical Competitor |
|---------|----------|-------------------|
| **Workflow Scope** | **Stitch 3-Tab Model** (Target & Discover $\rightarrow$ Cockpit $\rightarrow$ Tracker) | Disjointed multi-app point solutions |
| **Automation Ergonomics** | Slide-Over Automation Drawer with on-demand triggers & failure recovery | Rigid email alerts or hidden background cron |
| **Sandbox-to-Pipeline Bridge** | 1-click `+ Add to Review` directly into Review stage | Manual copy-paste between browser tabs |
| **Multi-Source Sourcing** | 6 live channels (Indeed, LinkedIn, Greenhouse, BuiltIn, Levels, Dice) | 1–2 scraping boards or manual copy-paste |
| **Posting Authenticity & Compliance** | 10-point deterministic auditor detecting DOL PERM filings, phantom pools & short windows | Zero authenticity checks (candidates waste hours on fake/internal postings) |
| **AI Resume Rewriting** | 3-agent LangGraph workflow with ATS + Recruiter evaluations | Single-prompt generic LLM rewrite |
| **Local LLM Support** | Hybrid local Ollama routing for evaluation judges | Cloud-only paid API models |
| **In-Funnel Return Loop** | Context-preserved tailoring with "Back to Cockpit (`Esc`)" | External modal or disorienting new window |
| **Tracking Philosophy** | Constructive momentum, streaks & weekly target progress | Punitive agile sprint deficit shaming |
| **Interview Loop Tracking** | First-class `EmployerInterviewEvent` tied to pipeline jobs | Generic external spreadsheet |
| **Self-Service BYOK** | Connect personal keys for OpenAI, Anthropic, Groq, Google, Ollama | Proprietary closed token billing |

---

## 5. Technology Stack Summary

- **Backend:** Python 3.11+, Django 5.2, Django Ninja REST API.
- **AI & Orchestration:** LangGraph, LangChain, OpenAI, Anthropic, Groq, Google Gemini, Ollama.
- **Vector Search & NLP:** `sentence-transformers` (`all-MiniLM-L6-v2`), BM25 keyword matching.
- **Task Queue & Caching:** Huey with Redis broker (`RedisExpireHuey`) and atomic cache locks.
- **Database:** MySQL / MariaDB (production) and SQLite (development).
- **Billing:** Stripe Subscriptions, Checkout, and Billing Portal.
