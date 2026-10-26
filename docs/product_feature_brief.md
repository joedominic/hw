# HireEdge — Product Feature Brief
### *For Competitive Market Research Input*

---

## The Problem

Job seekers spend 60–80% of their job hunt on low-leverage tasks: reformatting resumes per job description, tracking dozens of applications across spreadsheets, and manually searching disparate job boards. Despite AI tools promising to help, the market is fragmented — one tool for resume writing, another for job tracking, another for interview prep. There is no end-to-end, AI-native job search operating system.

---

## What HireEdge Does

HireEdge is an **AI-powered job search and career acceleration platform** that unifies the job search lifecycle into a single workspace:

1. **Find** — Discover relevant jobs from multiple sources, ranked by AI fit score.
2. **Evaluate** — AI-vets each job against your resume before you spend time on it.
3. **Tailor** — Multi-agent AI rewrites your resume for the specific job description.
4. **Apply** — Autonomous browser agent fills and submits applications on your behalf.

---

## Core Feature Set

### 🔍 AI-Powered Job Discovery
- Aggregates live job listings from **Indeed, LinkedIn (via JobSpy), Dice, Levels.fyi, BuiltIn, and Adzuna** in a single search
- Semantic matching: sentence-transformer embeddings rank jobs by similarity to your profile
- BM25 keyword matching + preference learning (likes/dislikes feedback loop)
- Seniority-level filtering via local LLM analysis
- Up to **6 job boards simultaneously** per saved search profile
- Saved search profiles with **scheduled automatic runs** (cron-based)

### 📊 Four-Stage Kanban Pipeline
- Visual pipeline board: **Search → Vetting → Applying → Done**
- Automated LLM-based promotion decisions using configurable score thresholds
- Bulk pipeline management with disqualifier rules (title, company, location)
- % match score and AI vetting notes per job card
- Sort by match % or freshness

### ✍️ Multi-Agent Resume Optimization
- **3-agent loop**: Writer → ATS Judge → Recruiter Judge (LangGraph orchestration)
- Local dense retrieval (RAG) over your resume chunks for contextually accurate edits
- ATS score evaluation with keyword gap analysis
- Iterative refinement with up to N configurable rounds
- Export tailored resume as **PDF or DOCX**
- Multiple saved optimizer workflows with custom prompts
- Cover letter generation with **length controls** (Short / Standard / Detailed / Make Shorter / Make Longer)
- Cover letter export as **PDF or DOCX**

### 🤖 Autonomous Apply Agent
- **Playwright-based browser automation** submits applications to ATS systems
- Native adapters for **Greenhouse, Lever, Ashby, iCIMS**
- Generic vision-capable `browser-use` fallback for all other sites
- Two modes: **Semi-Auto** (human review before submit) and **Full-Auto** (graduated confidence threshold)
- Full step-by-step audit trail per application attempt
- Crash recovery: state persisted to DB on every browser step

### 🎯 Job Fit Intelligence
- Per-job **AI fit score (0–100%)** based on resume-to-JD semantic similarity
- **Interview probability estimate** from AI vetting agent
- Resume–JD sentence-level alignment visualization (which resume bullet matched which requirement)
- Job insights: extracted skills, required vs. preferred, role level
- Liked/disliked preference centroid influences all future rankings

### ⚙️ Settings & Customization
- Connect your own LLM API keys (OpenAI, Anthropic, Groq, Google Gemini, Ollama local)
- Platform-managed API keys available (no key required for Free/Pro)
- Custom prompt templates (system prompt library per workflow)
- ATS judge profiles with custom scoring criteria
- Replacement/expansion rules for resume text
- Job disqualifier rules (auto-hide/reject matching jobs)
- Custom optimizer workflows (multi-step, per-job-type)
- Export / delete account (GDPR self-service)

### 💳 Plans & Pricing
| | Free | Pro | Unlimited |
|---|---|---|---|
| **Daily LLM Tokens** | 200,000 | 2,000,000 | Unlimited |
| **LLM Requests/day** | 50 | 500 | Unlimited |
| **Job Searches/day** | Limited | Higher | Unlimited |
| **Apply Runs/day** | Limited | Higher | Unlimited |
| **Storage** | Limited | Higher | Unlimited |
| **Own LLM API Keys** | ✅ | ✅ | ✅ |
| **API Access** | ❌ | ✅ | ✅ |
| **Priority Processing** | ❌ | ✅ | ✅ |

---

## Key Differentiators

| Feature | HireEdge | Typical Competitor |
|---|---|---|
| End-to-end in one tool | ✅ Search → Tailor → Apply | ❌ Point solutions |
| Multi-source job aggregation | ✅ 6 boards | ❌ 1–2 boards |
| Multi-agent AI resume rewrite | ✅ 3-agent LangGraph loop | ❌ Single-pass GPT prompt |
| Local LLM support (Ollama) | ✅ Zero cloud cost option | ❌ Cloud only |
| Autonomous apply agent | ✅ Playwright + ATS adapters | ❌ Browser extension only |
| AI fit score per job | ✅ Semantic + keyword hybrid | ⚠️ Basic keyword match |
| Resume RAG (local retrieval) | ✅ Dense + BM25 over chunks | ❌ Full resume in prompt |
| Cover letter sizing controls | ✅ Short / Standard / Detailed | ❌ Fixed length |
| Daily granular token tracking | ✅ By query + provider + model | ❌ Not exposed |
| Bring Your Own Key | ✅ Any OpenAI-compatible provider | ⚠️ Limited |

---

## Target Persona

**Primary:** Active job seekers in tech (software engineers, PMs, designers, data) applying to 20–50 companies simultaneously. Comfortable with AI tools. Value time over cost.

**Secondary:** Career coaches / recruiters managing client job searches. (Note: current architecture is 1 user = 1 tenant; team/org features not yet built.)

---

## Technology Stack (Brief)

- **Backend**: Python / Django 5.2 + Django Ninja (REST API)
- **AI**: LangGraph multi-agent, LangChain, OpenAI / Anthropic / Groq / Google / Ollama
- **Vector search**: `sentence-transformers` (all-MiniLM-L6-v2) + BM25
- **Browser automation**: Playwright + `browser-use`
- **Task queue**: Huey + Redis
- **Database**: MySQL / SQLite
- **Billing**: Stripe Subscriptions

---

## Key Questions for Competitive Research

1. **Market sizing**: How many active job seekers in the US/EU are using AI-assisted job search tools today? What's the projected growth (2025–2028)?
2. **Competitor landscape**: Who are the top 5–10 direct competitors? Evaluate: Teal, Simplify, Rezi, LazyApply, Sonara, KickResume, Pyjama Jobs, Copilot.cx, ResumeWorded.
3. **Feature gaps**: Which features do competitors offer that are missing here (e.g., LinkedIn auto-apply, direct recruiter outreach, salary negotiation)?
4. **Pricing benchmarks**: What do comparable tools charge? Is a freemium model competitive or should we go direct-pay?
5. **Acquisition channels**: Where do active job seekers discover job search tools? Reddit (r/cscareerquestions), LinkedIn, ProductHunt, job boards, SEO?
6. **Willingness to pay**: What is the average MRR per user for comparable tools? What are common churn triggers?
7. **B2B opportunity**: Is there a market for selling this to staffing firms, bootcamps, or career services at universities?
8. **Regulatory risk**: Are there ToS risks from scraping Indeed/LinkedIn at scale? How do competitors handle this?
9. **Local LLM angle**: Is the "zero cloud cost with your own Ollama server" positioning a meaningful differentiator for privacy-conscious users or developers?
10. **Autonomous apply risk**: Are there legal or reputational risks from fully automated job applications? How do competitors handle disclosure?
