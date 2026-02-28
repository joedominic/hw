# AI Resume Optimizer

An automated platform that uses multi-agent AI (LangGraph) to tailor resumes to job descriptions iteratively.

## Tech Stack
- **Backend:** Django 6.0.2 + Django Ninja (API)
- **AI Orchestration:** LangGraph + LangChain
- **UI:** Streamlit (Stub)
- **Background Tasks:** Python Threading (No Redis/Celery required)

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

**Terminal 2: Streamlit UI**
```bash
streamlit run ui/app.py
```

## Architecture Notes
- **Why Streamlit?** It provides a high-level abstraction for real-time AI interactions (polling, status bars) which is perfect for this stub.
- **Why Django?** Provides a robust ORM and admin interface for managing resume data and logs.
- **Background Processing:** Background tasks are handled via Python threads to remove external dependencies like Redis. This is suitable for development/testing environments.
- **The Workflow:** The system uses a 3-agent loop (Writer -> ATS Judge -> Recruiter Judge). It iterates up to 3 times or until the average score reaches 85/100.
