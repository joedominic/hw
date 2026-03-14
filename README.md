# AI Resume Optimizer

An automated platform that uses multi-agent AI (LangGraph) to tailor resumes to job descriptions iteratively.

## Tech Stack
- **Backend:** Django 5.2.11 + Django Ninja (API)
- **AI Orchestration:** LangGraph + LangChain
- **UI:** Streamlit (Stub)
- **Background Tasks:** Huey (Redis-backed async task queue)

## Setup Instructions

### 1. Prerequisites
- Python 3.12+

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Database Setup
```bash
cd django_project
python manage.py migrate
```

### 4. Running the Application
You will need two terminal windows:

**Terminal 1: Django Backend**
```bash
cd django_project
python manage.py runserver
```

**Terminal 2: Huey worker** (required for resume optimization)
```bash
cd django_project
python manage.py run_huey
```
Uses Redis at `192.168.2.174:6379` by default; set `HUEY_REDIS_HOST`, `HUEY_REDIS_PORT`, `HUEY_REDIS_DB` to override.

**Terminal 3 (optional): Streamlit UI**
```bash
streamlit run ui/app.py
```

## Architecture Notes
- **Why Streamlit?** It provides a high-level abstraction for real-time AI interactions (polling, status bars) which is perfect for this stub.
- **Why Django?** Provides a robust ORM and admin interface for managing resume data and logs.
- **Background Processing:** Resume optimization runs in Huey workers backed by Redis (default: `192.168.2.174`). Run `python manage.py run_huey` from `django_project/` so enqueued tasks execute.
- **The Workflow:** The system uses a 3-agent loop (Writer -> ATS Judge -> Recruiter Judge). It iterates up to 3 times or until the average score reaches 85/100.
