# HireEdge — AI Job Search, Resume Optimizer & Career Pipeline

An enterprise-grade, AI-powered platform for job seekers and hiring professionals. HireEdge uses multi-agent LangGraph workflows to tailor resumes to job descriptions, aggregates and ranks job postings across multiple sources (JobSpy, Indeed, LinkedIn, Dice, Levels.fyi, BuiltIn, Adzuna), manages a four-stage application pipeline (Discovered → Applying → Interview → Offer), and provides AI-powered interview prep and cover letter generation.

## Tech Stack

- **Backend Framework:** Django 5.2.11 + Django Ninja 1.5.3 (RESTful API)
- **Database:** SQLite (local development with WAL & busy timeout) or MySQL / MariaDB (production with TLS support)
- **AI & Agent Orchestration:** LangGraph + LangChain (OpenAI, Anthropic, Groq, Google GenAI, Local/Cloud Ollama)
- **Frontend / UI:** Server-rendered Django Templates + Tailwind CSS (CDN) + vanilla JavaScript `fetch()`
- **Background Tasks:** Huey 2.6+ (Redis-backed asynchronous task queue & periodic scheduler)
- **Embeddings & NLP:** `sentence-transformers` (`all-MiniLM-L6-v2`), `rank-bm25`, `pdfplumber`, `python-docx`
- **Billing & Subscriptions:** Stripe Checkout, Customer Portal & Webhooks (Free, Pro, Unlimited plans with daily request & token quotas)
- **Security:** Fernet encryption for API keys/credentials, `django-hijack` staff impersonation with audit trails, rate limiting, and abuse throttle middleware

---

## Setup Instructions

### 1. Prerequisites
- Python 3.12+
- Redis 5.0+ (running locally at `127.0.0.1:6379` or accessible via `HUEY_REDIS_HOST`)
- (Optional) Docker for containerized deployment

### 2. Install Dependencies
```bash
# Install main dependencies
pip install -r requirements.txt

# Install JobSpy separately with --no-deps (resolves upstream dependency conflicts)
pip install --no-deps -r requirements-jobspy.txt

# Install Playwright browser binaries
playwright install chromium
```

> **Note:** PyTorch CPU wheels can be installed separately for faster installation:
> `pip install torch --index-url https://download.pytorch.org/whl/cpu`

### 3. Environment Configuration
Create a `.env` file in the repository root (see [Environment Variables](#environment-variables)):
```ini
SECRET_KEY=your-secure-secret-key-here
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
HUEY_REDIS_HOST=127.0.0.1
HUEY_REDIS_PORT=6379
```

### 4. Database Setup & Migrations
```bash
cd django_project
python manage.py migrate
python manage.py createsuperuser
```

### 5. Running the Application

For full functionality (API, web UI, asynchronous optimizations, and scheduled searches), run the following services:

**Terminal 1: Web Server**
```bash
cd django_project
python manage.py runserver 127.0.0.1:8000
```

**Terminal 2: Huey Worker & Scheduler**
```bash
cd django_project
python manage.py run_huey
```
*(Alternatively, set `HUEY_IMMEDIATE=1` in `.env` for single-process development without Redis, note that periodic tasks will be disabled).*

Access the application:
- **Web UI:** [http://127.0.0.1:8000/](http://127.0.0.1:8000/)
- **Interactive OpenAPI Docs:** [http://127.0.0.1:8000/api/docs](http://127.0.0.1:8000/api/docs)
- **Django Admin:** [http://127.0.0.1:8000/admin/](http://127.0.0.1:8000/admin/)

---

## Docker Deployment

Build and run using the provided Dockerfile:
```bash
docker build -t resume-elite .
docker run -p 8000:8000 --env-file .env resume-elite
```

---

## Core Features

1. **AI Resume Optimizer (LangGraph):**
   - Single-pass / custom step pipeline: Writer → ATS Judge → Recruiter Judge.
   - Smart context budgeting (`optimizer_budget.py`), role-focused JD extraction, and dense + BM25 local RAG over resume chunks.
   - Cloud-only model routing for high-quality resume generation.

2. **Job Search & Ingestion:**
   - Multi-board aggregation: Indeed, LinkedIn (JobSpy), Dice (API/scraper), Levels.fyi (encrypted API), BuiltIn, and Adzuna (REST API).
   - Preference ranking: Cosine similarity on user liked/disliked vectors + BM25 keyword boosting + whole-word phrase disqualifiers.
   - Seniority check via Ollama Guard.

3. **Kanban Application Pipeline:**
   - Four stages: **Pipeline** (inbox/scoring) → **Vetting** (AI fit check & interview probability) → **Applying** (tailoring & apply agent) → **Done** (applied & interview prep).
   - Automated rule-based and LLM-based stage promotion.

4. **Autonomous Apply Agent:**
   - Semi-auto (human approval required) and Full-auto (autonomous submission on graduated ATS streak).
   - Specialized adapters for Greenhouse, Lever, Ashby, and iCIMS.
   - Generic fallback powered by `browser-use` + LLM vision for custom career portals.
   - Audit logging with screenshots, step capture, and network inspection.

5. **SaaS Billing & Quotas:**
   - Built-in Free, Pro, and Unlimited plans with daily LLM request, token, search, and apply limits.
   - Stripe integration for subscriptions, checkout sessions, billing portal, and webhooks.
   - Customer API Key support (`CustomerApiKey`) for programmatic access.
