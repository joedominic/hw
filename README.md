# AI Resume Optimizer

An automated platform that uses multi-agent AI (LangGraph) to tailor resumes to job descriptions iteratively.

## Tech Stack
- **Backend:** Django 6.0.2 + Django Ninja (API)
- **Task Queue:** Celery + Redis
- **AI Orchestration:** LangGraph + LangChain
- **UI:** Streamlit (Stub)

## Setup Instructions

### 1. Prerequisites
- Python 3.12+
- Redis (See below for installation)

### 2. Install Redis (No Docker)
If you cannot use Docker, install Redis natively:
- **macOS (Homebrew):** `brew install redis && brew services start redis`
- **Ubuntu/Debian:** `sudo apt install redis-server && sudo systemctl start redis`
- **Windows:** Use [Memurai](https://www.memurai.com/) or [Redis for Windows](https://github.com/microsoftarchive/redis/releases).

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Database Setup
```bash
cd django_project
python manage.py migrate
```

### 5. Running the Application
You will need three terminal windows:

**Terminal 1: Django Backend**
```bash
cd django_project
python manage.py runserver
```

**Terminal 2: Celery Worker**
```bash
cd django_project
celery -A core worker -l info
```

**Terminal 3: Streamlit UI**
```bash
streamlit run ui/app.py
```

## Architecture Notes
- **Why Streamlit?** It provides a high-level abstraction for real-time AI interactions (polling, status bars) which is perfect for this stub.
- **Why Django?** Provides a robust ORM and admin interface for managing resume data and logs.
- **The Workflow:** The system uses a 3-agent loop (Writer -> ATS Judge -> Recruiter Judge). It iterates up to 3 times or until the average score reaches 85/100.
