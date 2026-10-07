# HireEdge — Product Feature Brief

*Executive summary of product capabilities, architectural differentiators, and market positioning.*

---

## 1. The Problem

Job seekers spend 60–80% of their job hunt on low-leverage, fragmented tasks: tailoring resumes to each individual job description, manually checking multiple disconnected job boards, losing track of application stages across ad-hoc spreadsheets, and guessing at hiring requirements.

Despite the proliferation of point-solution AI tools, the workflow remains disjointed: one tool rewrites a resume, another tracks submissions, and search aggregators bombard users with duplicate postings.

---

## 2. What HireEdge Does

HireEdge is an **AI-powered job search and career acceleration operating system** that integrates the complete job search lifecycle into a single unified workspace:

1. **Discover:** Aggregates live job postings across six major channels, deduplicating listings and ranking them with semantic fit and user preference learning.
2. **Automate:** Automates recurring search tasks via the **Career Cockpit**, with on-demand manual triggers, cooldown protection, and full ingestion breakdowns.
3. **Evaluate:** Pre-scores job opportunities with AI fit checks and keyword alignment before the user commits time.
4. **Tailor:** Deploys a multi-agent LangGraph workflow (Writer → ATS Judge → Recruiter Judge) to tailor resumes and generate custom cover letters.
5. **Track & Momentum:** Manages candidates across a 4-stage Kanban pipeline, monitoring interview loops (`EmployerInterviewEvent`) and application velocity in a real-time Performance Dashboard.

---

## 3. Core Feature Set

### 🔍 Multi-Source Job Discovery & Ranking
- Aggregates live listings from **Indeed, LinkedIn (via JobSpy), Greenhouse direct boards, BuiltIn, Levels.fyi, and Dice**.
- Semantic matching using `sentence-transformers` (`all-MiniLM-L6-v2`) embeddings.
- Dynamic preference learning: user likes and dislikes continuously adjust a profile centroid vector.
- Automatic deduplication across boards: identical roles on multiple aggregators collapse to a single listing.
- Negative keyword disqualifiers: auto-screens out unwanted companies, titles, or locations.

### 🕹️ Career Cockpit & Search Automation
- Centralized scheduling of automated search tasks (daily, weekdays, weekly, custom cron).
- On-demand **"Run Search Now"** execution with atomic pending state preventing premature polling exit.
- 60-minute manual trigger cooldown with **automatic failure reset** (zero lockout on failed runs).
- Interactive Run Inspection Modal detailing disposition breakdowns (New, Reposted, Already in Pipeline, Expired, Eliminated, Previously Removed) with CSV and JSON exports.
- Skill Radar visualizing top technical and domain skills in demand.

### ✍️ Multi-Agent AI Resume Optimization
- **3-Agent LangGraph Pipeline:**
  1. *Writer Node:* Tailors bullet points to highlight relevant impact for the target role.
  2. *ATS Judge Node:* Evaluates keyword coverage, formatting, and applicant tracking fit.
  3. *Recruiter Judge Node:* Evaluates executive readability, brevity, and business impact.
- **Hybrid Local LLM Routing:** Run ATS and Recruiter judges on local Ollama models (`OPTIMIZER_JUDGES_PREFER_LOCAL=True`) to achieve zero marginal cloud API cost on evaluation steps.
- **Semantic JD Cleansing:** Extracts core role requirements using semantic slicing to minimize input token burn.
- **Artifact Generation:** Export tailored resumes and cover letters (with Short / Standard / Detailed controls) as PDF or DOCX.

### 📊 Kanban Pipeline & Interview Tracking
- 4-stage pipeline: **Pipeline (New) → Vetting (Review) → Applying (Tailoring) → Done (Applied)**.
- Post-application interview tracking via `EmployerInterviewEvent` (Recruiter Screen, Hiring Manager, Tech Screen, On-Site, Offer).
- Progressive-load Performance Dashboard (`/performance/`) tracking submission volume and funnel conversion velocity.

### ⚙️ Multi-Tenancy, Quotas & Subscriptions
- Multi-tenant architecture with owner-scoped data isolation and staff impersonation via `django-hijack`.
- Stripe billing integration with Free, Pro, and Unlimited plans enforcing daily LLM token and request quotas (`UsageCounter`).
- Bring-Your-Own-Key (BYOK) with encrypted credential storage (Fernet) alongside platform key fallbacks.
- Emergency automation kill-switch (`stop_llm_requests`) in settings.

---

## 4. Key Differentiators

| Feature | HireEdge | Typical Competitor |
|---------|----------|-------------------|
| **Workflow Scope** | Unified Search → Automation → Tailor → Interview Tracking | Fragmented point solutions |
| **Multi-Source Sourcing** | 6 channels (Indeed, LinkedIn, Greenhouse, BuiltIn, Levels.fyi, Dice) | 1–2 boards or manual copy-paste |
| **Search Automation Hub** | Career Cockpit with on-demand trigger, cooldowns & inspection modal | Static email alerts or basic cron |
| **AI Resume Rewriting** | 3-agent LangGraph workflow with ATS + Recruiter evaluations | Single-prompt generic LLM rewrite |
| **Local LLM Support** | Hybrid local Ollama routing for evaluation judges | Cloud-only paid API models |
| **Preference Learning** | Semantic centroid learns from user likes/dislikes | Keyword filtering only |
| **Interview Tracking** | First-class `EmployerInterviewEvent` linked to pipeline jobs | Generic external spreadsheet |
| **Self-Service BYOK** | Connect personal keys for OpenAI, Anthropic, Groq, Google, Ollama | Proprietary closed token billing |

---

## 5. Technology Stack Summary

- **Backend:** Python 3.11+, Django 5.2, Django Ninja REST API.
- **AI & Orchestration:** LangGraph, LangChain, OpenAI, Anthropic, Groq, Google Gemini, Ollama.
- **Vector Search & NLP:** `sentence-transformers` (`all-MiniLM-L6-v2`), BM25 keyword matching.
- **Task Queue & Caching:** Huey with Redis broker (`RedisExpireHuey`) and atomic cache locks.
- **Database:** MySQL / MariaDB (production) and SQLite (development).
- **Billing:** Stripe Subscriptions, Checkout, and Billing Portal.
