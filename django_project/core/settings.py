"""
Django settings for core project.
"""

from pathlib import Path
import os
import environ

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Environment
env = environ.Env(
    DEBUG=(bool, True),
)
environ.Env.read_env(os.path.join(BASE_DIR.parent, ".env"))

# SECURITY: keep the secret key used in production secret!
SECRET_KEY = env("SECRET_KEY", default="django-insecure-+a#*@w#!qb+w*1_6vd4my0q2q^!ddes#&#%jueou)q7(5(=v*n")

# SECURITY: don't run with debug turned on in production!
DEBUG = env("DEBUG")

# LLM test + Huey monitor nav links; defaults to DEBUG when unset.
SHOW_DEV_TOOLS = env.bool("SHOW_DEV_TOOLS", default=DEBUG)

# Comma-separated list, e.g. "localhost,127.0.0.1,.example.com"
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "huey.contrib.djhuey",
    "resume_app",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "resume_app.context_processors.dev_tools",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# Default primary key type (silences models.W042)
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = "static/"

import os
MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(BASE_DIR, "media")

# Optional simple auth for public APIs.
# When API_ACCESS_TOKEN is set, API endpoints that call _require_api_auth
# will require header X-Api-Token with this exact value. When unset, those
# endpoints remain open (development/demo default).
API_ACCESS_TOKEN = env("API_ACCESS_TOKEN", default=None)

# Optional server-side LLM API keys (if set, client can omit api_key in request)
OPENAI_API_KEY = env("OPENAI_API_KEY", default=None)
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default=None)
GROQ_API_KEY = env("GROQ_API_KEY", default=None)
GOOGLE_API_KEY = env("GOOGLE_API_KEY", default=None)

# Preference vector cache for job focus ranking (Django-only embeddings)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "OPTIONS": {"MAX_ENTRIES": 100},
    }
}
# Bump when embedding formula changes (v5 = sentence-level role similarity, pref_role_sentences)
PREFERENCE_VECTOR_CACHE_KEY = "job_preference_vector_v5"
# Hybrid focus: alpha * title_sim + (1-alpha) * role_sim (then optionally blended with BM25 keyword score).
# Title weight = alpha (25%); Role weight = 1-alpha (75%).
JOB_FOCUS_TITLE_WEIGHT = 0.25
# Sentence-level role: top-k mean of max similarity (k best-matching sentences).
JOB_FOCUS_ROLE_TOP_K = 10
# Optional BM25 keyword weight in final focus score (0 = off, 0.2 = light boost, 0.5 = strong).
JOB_FOCUS_KEYWORD_WEIGHT = 0.2
# Sentence alignment UI: cap reuse of same liked sentence so one phrase doesn't dominate; show "No strong match" below threshold.
JOB_FOCUS_ALIGNMENT_LIKED_MAX_REUSE = 2
JOB_FOCUS_ALIGNMENT_MIN_SIM = 0.55  # cosine; below this show "No strong match" (~64% when scaled 0–100)
# Resume–job sentence-level match: top-k mean with capped reuse. k=5; each resume sentence at most 2.
JOB_RESUME_TOP_K = 5
JOB_RESUME_MAX_REUSE = 2
# Min similarity (cosine/blended) to count a pair; below this we don't use it in score. 0.45 ≈ 72% when scaled 0–100.
JOB_RESUME_MIN_SIM = 0.45
# Keyword overlap weight: blended = (1 - β)*cosine + β*overlap. 0.2 rewards resume sentences that contain job terms.
JOB_RESUME_KEYWORD_WEIGHT = 0.2
# Title gate: when title_sim (cosine) is below this, role can add at most JOB_FOCUS_ROLE_MAX_LIFT.
JOB_FOCUS_TITLE_GATE = 0.30  # cosine in [-1,1]; ~25% when converted to 0-100
JOB_FOCUS_ROLE_MAX_LIFT = 0.15  # max extra from role when below gate (so combined <= title_sim + this)
# Job search: over-fetch from API so after disqualifier/dislike filtering we still fill the page.
JOB_SEARCH_FETCH_BUFFER = 150  # fetch this many from JobSpy; then filter and take top DISPLAY_LIMIT
JOB_SEARCH_DISPLAY_LIMIT = 50  # max jobs returned per search (top N after sort)
# Disliked-job similarity: penalize results similar to disliked (listing-level embedding).
JOB_DISLIKED_SIMILARITY_PENALTY_WEIGHT = 0.4  # penalty = weight * disliked_sim (0–1)
JOB_DISLIKED_SIMILARITY_THRESHOLD = 0.3  # only penalize when similarity above this (0–1)
# Hide jobs with similar_to_disliked_percent >= this (None = never hide, 100 = hide only 100% similar).
JOB_DISLIKED_SIMILARITY_HIDE_THRESHOLD = 100

# Huey async task queue (Redis). Set HUEY_IMMEDIATE=1 to run without Redis (tasks run in-process).
HUEY_IMMEDIATE = env.bool("HUEY_IMMEDIATE", default=False)
HUEY_REDIS_HOST = env("HUEY_REDIS_HOST", default="192.168.2.174")
HUEY_REDIS_PORT = env.int("HUEY_REDIS_PORT", default=6379)
HUEY_REDIS_DB = env.int("HUEY_REDIS_DB", default=0)
HUEY = {
    "name": "jobapplier",
    "huey_class": "huey.RedisHuey",
    "results": True,
    "store_none": False,
    "immediate": HUEY_IMMEDIATE,
    "utc": True,
    "blocking": True,
    "connection": {
        "host": HUEY_REDIS_HOST,
        "port": HUEY_REDIS_PORT,
        "db": HUEY_REDIS_DB,
        "read_timeout": 1,
    },
    "consumer": {
        "workers": 2,
        # thread: works on Windows. process: not picklable on Windows (spawn).
        "worker_type": "thread",
        "scheduler_interval": 1,
        "periodic": not HUEY_IMMEDIATE,
    },
}

# python-jobspy logs Glassdoor API hiccups at ERROR even when the HTTP request succeeds.
# Downgrade so runserver output stays readable; other JobSpy:* loggers unchanged.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "loggers": {
        "JobSpy:Glassdoor": {
            "level": "WARNING",
        },
    },
}
