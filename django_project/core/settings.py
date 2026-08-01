"""
Django settings for core project.
"""

from pathlib import Path
import os
import sys
import environ
from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Environment
env = environ.Env(
    DEBUG=(bool, True),
)
_env_file = os.environ.get("ENV_FILE") or os.path.join(BASE_DIR.parent, ".env")
if os.path.isfile(_env_file):
    environ.Env.read_env(_env_file)

# Detect manage.py test / pytest early (used by DATABASES and cache).
_IN_TEST = "test" in sys.argv or "pytest" in (sys.argv[0] if sys.argv else "")

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
    "hijack",
    "hijack.contrib.admin",
    "huey.contrib.djhuey",
    "resume_app",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "hijack.middleware.HijackUserMiddleware",
    "resume_app.middleware.LoginRequiredMiddleware",
    "resume_app.abuse.AbuseThrottleMiddleware",
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
                "resume_app.context_processors.experience_context",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases
#
# MySQL/MariaDB when MYSQL_DATABASE is set; otherwise SQLite.
# Tests always use SQLite unless MYSQL_USE_FOR_TESTS=1 (avoids wiping shared MariaDB).

_MYSQL_DATABASE = env("MYSQL_DATABASE", default="")
_USE_MYSQL = bool(_MYSQL_DATABASE) and (
    not _IN_TEST or env.bool("MYSQL_USE_FOR_TESTS", default=False)
)

if _USE_MYSQL:
    try:
        import pymysql

        pymysql.install_as_MySQLdb()
    except ImportError as exc:  # pragma: no cover - misconfigured deploy
        raise ImproperlyConfigured(
            "MYSQL_DATABASE is set but PyMySQL is not installed. "
            "Install with: pip install PyMySQL"
        ) from exc

    # TLS: the MariaDB server negotiates TLS by default. Keep encryption on unless
    # MYSQL_SSL_DISABLED=1. Provide MYSQL_SSL_CA to upgrade to verified TLS.
    _mysql_options = {
        "charset": "utf8mb4",
        "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
    }
    _mysql_ssl_ca = env("MYSQL_SSL_CA", default="")
    if _mysql_ssl_ca:
        # Verified TLS: encrypt + validate the server certificate against the CA.
        _mysql_options["ssl"] = {"ca": _mysql_ssl_ca}
        _mysql_options["ssl_verify_cert"] = True
        _mysql_options["ssl_verify_identity"] = env.bool(
            "MYSQL_SSL_VERIFY_IDENTITY", default=True
        )
    elif env.bool("MYSQL_SSL_DISABLED", default=False):
        # Explicit opt-out (e.g. local socket) — plaintext connection.
        _mysql_options["ssl_disabled"] = True
    # else: default opportunistic TLS (encrypted, unverified) as negotiated by the server.

    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": _MYSQL_DATABASE,
            "USER": env("MYSQL_USER", default=""),
            "PASSWORD": env("MYSQL_PASSWORD", default=""),
            "HOST": env("MYSQL_HOST", default="127.0.0.1"),
            "PORT": env("MYSQL_PORT", default="3306"),
            "OPTIONS": _mysql_options,
            "CONN_MAX_AGE": env.int("MYSQL_CONN_MAX_AGE", default=60),
            "CONN_HEALTH_CHECKS": env.bool("MYSQL_CONN_HEALTH_CHECKS", default=True),
            # Dedicated test database so `manage.py test` never touches the app DB.
            "TEST": {
                "NAME": env("MYSQL_TEST_DATABASE", default=f"test_{_MYSQL_DATABASE}"),
                "CHARSET": "utf8mb4",
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            # Huey + runserver (or multiple workers) contending on SQLite — longer wait + WAL (see resume_app.apps).
            "OPTIONS": {
                "timeout": 30,
            },
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

# Production: set HUEY_IMMEDIATE=0 and configure Redis (HUEY_REDIS_*). Use SIGNUP_ENABLED=0 for invite-only.
# Optional: DEFAULT_FILE_STORAGE for S3-compatible media when running multiple app instances.

# --- Authentication ---
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "pipeline"
LOGOUT_REDIRECT_URL = "login"
SIGNUP_ENABLED = env.bool("SIGNUP_ENABLED", default=True)
DEFAULT_EXPERIENCE_MODE = env("DEFAULT_EXPERIENCE_MODE", default="normal")
REQUIRE_EMAIL_VERIFICATION = env.bool("REQUIRE_EMAIL_VERIFICATION", default=False)
LOGIN_EXEMPT_URL_PREFIXES = (
    "/accounts/",
    "/admin/",
    "/static/",
    "/billing/stripe/webhook/",
    "/legal/",
)

# Transactional email (console backend for local/dev; configure SMTP in production)
EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=25)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=False)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="ResumeElite <noreply@localhost>")
ACCOUNT_EMAIL_BASE_URL = env("ACCOUNT_EMAIL_BASE_URL", default="")

# Fernet encryption: comma-separated urlsafe keys (newest first). Empty = derive from SECRET_KEY.
FERNET_KEYS = env("FERNET_KEYS", default="")

# SaaS plans / quotas / Stripe
SAAS_DEFAULT_PLAN_SLUG = env("SAAS_DEFAULT_PLAN_SLUG", default="free")
SAAS_ENFORCE_QUOTAS = env.bool(
    "SAAS_ENFORCE_QUOTAS",
    default=not _IN_TEST,
)
# When True, staff/superuser skip plan quotas (default False for SaaS safety).
SAAS_STAFF_BYPASS_QUOTAS = env.bool("SAAS_STAFF_BYPASS_QUOTAS", default=False)
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", default="")
STRIPE_PUBLISHABLE_KEY = env("STRIPE_PUBLISHABLE_KEY", default="")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", default="")

# Abuse throttling
ABUSE_THROTTLE_ENABLED = env.bool("ABUSE_THROTTLE_ENABLED", default=True)
ABUSE_AUTH_LIMIT = env.int("ABUSE_AUTH_LIMIT", default=20)
ABUSE_AUTH_WINDOW_SECONDS = env.int("ABUSE_AUTH_WINDOW_SECONDS", default=300)
ABUSE_API_LIMIT = env.int("ABUSE_API_LIMIT", default=120)
ABUSE_API_WINDOW_SECONDS = env.int("ABUSE_API_WINDOW_SECONDS", default=60)

# Production HTTPS / cookies (enabled when DEBUG is False unless overridden)
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=not DEBUG)
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=not DEBUG)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=not DEBUG)
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=(31536000 if not DEBUG else 0))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=not DEBUG)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=False)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# Optional S3-compatible media (django-storages). Leave bucket empty for local filesystem.
AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", default="")
AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default="")
AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL", default="")
AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default="")
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", default="")
AWS_DEFAULT_ACL = env("AWS_DEFAULT_ACL", default="private")
AWS_QUERYSTRING_AUTH = env.bool("AWS_QUERYSTRING_AUTH", default=True)
if AWS_STORAGE_BUCKET_NAME:
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }

# django-hijack: support staff impersonation
HIJACK_PERMISSION_CHECK = "resume_app.hijack_permissions.can_hijack"
HIJACK_INSERT_BEFORE = "<main"

# Per-user LLM usage limits (0 = unlimited)
LLM_USER_DAILY_REQUEST_LIMIT = env.int("LLM_USER_DAILY_REQUEST_LIMIT", default=0)
# Daily token budgets by plan slug (0 = unlimited). Optional LLM_USER_DAILY_TOKEN_LIMIT is a hard ceiling.
LLM_USER_DAILY_TOKEN_LIMIT = env.int("LLM_USER_DAILY_TOKEN_LIMIT", default=0)
LLM_DAILY_TOKEN_LIMIT_BY_PLAN = {
    "free": env.int("LLM_DAILY_TOKEN_LIMIT_FREE", default=200_000),
    "pro": env.int("LLM_DAILY_TOKEN_LIMIT_PRO", default=2_000_000),
    "unlimited": env.int("LLM_DAILY_TOKEN_LIMIT_UNLIMITED", default=0),
}
# Shared burn cap for platform/env API keys across all users (0 = unlimited).
LLM_PLATFORM_DAILY_TOKEN_LIMIT = env.int("LLM_PLATFORM_DAILY_TOKEN_LIMIT", default=5_000_000)
# Wall-clock timeout per gateway/browser-use LLM invoke (seconds; 0 = disabled).
LLM_INVOKE_TIMEOUT_SECONDS = env.int("LLM_INVOKE_TIMEOUT_SECONDS", default=180)
# Max concurrent in-flight LLM calls per user (0 = unlimited).
LLM_USER_MAX_CONCURRENT = env.int("LLM_USER_MAX_CONCURRENT", default=2)
# Free-form POST /llm/complete: max combined system+user characters.
LLM_COMPLETE_MAX_INPUT_CHARS = env.int("LLM_COMPLETE_MAX_INPUT_CHARS", default=16_000)

# Optional simple auth for public APIs (deprecated; session auth is primary).
# When API_ACCESS_TOKEN is set, API endpoints that call _require_api_auth
# will require header X-Api-Token with this exact value. When unset, those
# endpoints remain open (development/demo default).
API_ACCESS_TOKEN = env("API_ACCESS_TOKEN", default=None)

# Optional server-side LLM API keys (if set, client can omit api_key in request)
OPENAI_API_KEY = env("OPENAI_API_KEY", default=None)
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default=None)
GROQ_API_KEY = env("GROQ_API_KEY", default=None)
GOOGLE_API_KEY = env("GOOGLE_API_KEY", default=None)

# Preference / abuse cache — configured after Huey Redis settings below.
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

# --- Resume optimizer: writer context budgets (Phase 1) + local RAG (Phase 2) ---
OPTIMIZER_USE_ROLE_SLICE_FOR_WRITER_JD = True
OPTIMIZER_WRITER_JD_ROLE_MAX_CHARS = 8000
# Body shown to Writer on first pass (then replaced by prior draft on later writer steps).
OPTIMIZER_WRITER_RESUME_MAX_CHARS = 14000
# When hybrid retrieval returns chunks, shrink the parallel full-resume excerpt further.
OPTIMIZER_WRITER_RESUME_MAX_CHARS_WITH_RAG = 8000
# Immutable source anchor passed to Writer (capped).
OPTIMIZER_SOURCE_RESUME_MAX_CHARS = 12000
OPTIMIZER_CONTEXT_NOTES_MAX_CHARS = 4000
OPTIMIZER_CONTEXT_SKILLS_JSON_MAX_CHARS = 8000
OPTIMIZER_CONTEXT_JOB_HIGHLIGHTS_MAX_CHARS = 4000
# Local hybrid retrieval over ResumeChunk rows (dense + BM25).
OPTIMIZER_RETRIEVAL_ENABLED = True
OPTIMIZER_RETRIEVAL_TOP_K = 28
OPTIMIZER_RETRIEVAL_MAX_PACK_CHARS = 12000
OPTIMIZER_RETRIEVAL_DENSE_WEIGHT = 0.75
OPTIMIZER_RETRIEVAL_KEYWORD_WEIGHT = 0.25
# Judge steps: cap draft + cleansed JD payloads; prefer local/small models when configured.
OPTIMIZER_JUDGE_RESUME_MAX_CHARS = 12000
OPTIMIZER_JUDGE_JD_MAX_CHARS = 8000
# When True, ATS/Recruiter judges prefer an is_local (Ollama) preference row.
# Default False: resume optimization should use cloud/remote LLMs whenever available.
OPTIMIZER_JUDGES_PREFER_LOCAL = env.bool("OPTIMIZER_JUDGES_PREFER_LOCAL", default=False)
# Title gate: when title_sim (cosine) is below this, role can add at most JOB_FOCUS_ROLE_MAX_LIFT.
JOB_FOCUS_TITLE_GATE = 0.30  # cosine in [-1,1]; ~25% when converted to 0-100
JOB_FOCUS_ROLE_MAX_LIFT = 0.15  # max extra from role when below gate (so combined <= title_sim + this)
# Job search: over-fetch from API so after disqualifier/dislike filtering we still fill the page.
JOB_SEARCH_FETCH_BUFFER = 150  # fetch this many from JobSpy; then filter and take top DISPLAY_LIMIT
JOB_SEARCH_DISPLAY_LIMIT = 50  # max jobs returned per search (top N after sort)
JOB_SEARCH_HOURS_OLD = 168  # only jobs posted within this many hours (7 days); passed to JobSpy + post-filter

# Adzuna job search API (https://developer.adzuna.com/) — required when "adzuna" is selected as a source.
ADZUNA_APP_ID = env("ADZUNA_APP_ID", default="")
ADZUNA_APP_KEY = env("ADZUNA_APP_KEY", default="")
ADZUNA_COUNTRY = env("ADZUNA_COUNTRY", default="us")
ADZUNA_MAX_PAGES = env.int("ADZUNA_MAX_PAGES", default=3)

# Dice job search (JSON API used by dice.com; optional override of public browser key).
DICE_API_KEY = env("DICE_API_KEY", default="")
DICE_COUNTRY_CODE = env("DICE_COUNTRY_CODE", default="US")
DICE_RADIUS_MILES = env.int("DICE_RADIUS_MILES", default=30)

# Levels.fyi job search (undocumented encrypted API used by levels.fyi/jobs).
LEVELS_FYI_STANDARD_LEVELS = env("LEVELS_FYI_STANDARD_LEVELS", default="")
LEVELS_FYI_OFFSET_STEP = env.int("LEVELS_FYI_OFFSET_STEP", default=10)
LEVELS_FYI_PAGE_DELAY = env.float("LEVELS_FYI_PAGE_DELAY", default=0.35)
LEVELS_FYI_MAX_SCAN_PAGES = env.int("LEVELS_FYI_MAX_SCAN_PAGES", default=30)
# Disliked-job similarity: penalize results similar to disliked (listing-level embedding).
JOB_DISLIKED_SIMILARITY_PENALTY_WEIGHT = 0.4  # penalty = weight * disliked_sim (0–1)
JOB_DISLIKED_SIMILARITY_THRESHOLD = 0.3  # only penalize when similarity above this (0–1)
# Hide jobs with similar_to_disliked_percent >= this (None = never hide, 100 = hide only 100% similar).
JOB_DISLIKED_SIMILARITY_HIDE_THRESHOLD = 100

# Huey async task queue (Redis). Set HUEY_IMMEDIATE=1 to run without Redis (tasks run in-process).
# Defaults target a local, private Redis. Point HUEY_REDIS_HOST at your broker and set
# REDIS_PASSWORD when the instance requires AUTH (recommended on any shared network).
HUEY_IMMEDIATE = env.bool("HUEY_IMMEDIATE", default=False)
HUEY_REDIS_HOST = env("HUEY_REDIS_HOST", default="127.0.0.1")
HUEY_REDIS_PORT = env.int("HUEY_REDIS_PORT", default=6379)
HUEY_REDIS_DB = env.int("HUEY_REDIS_DB", default=0)
# Shared Redis AUTH password for Huey, cache, and LLM limiter (empty = no auth).
REDIS_PASSWORD = env("REDIS_PASSWORD", default="")
# Enable TLS to Redis (rediss://) when the broker terminates TLS.
REDIS_USE_TLS = env.bool("REDIS_USE_TLS", default=False)
# Queue name must be unique per app checkout when sharing a Redis instance
# (e.g. JobApp-Main vs JobApp-Jules). Colliding names steal each other's tasks.
HUEY_NAME = env("HUEY_NAME", default="jobapp-main")

# Task results expire instead of accumulating forever: RedisExpireHuey stores each
# result under its own key with a TTL. Plain RedisHuey keeps them in a hash that is
# only pruned when a caller reads the result — nothing here does, so it grows unbounded.
HUEY_RESULT_EXPIRE_SECONDS = env.int("HUEY_RESULT_EXPIRE_SECONDS", default=86_400)

_huey_connection = {
    "host": HUEY_REDIS_HOST,
    "port": HUEY_REDIS_PORT,
    "db": HUEY_REDIS_DB,
    "read_timeout": 1,
    # Storage kwarg consumed by RedisExpireStorage.
    "expire_time": HUEY_RESULT_EXPIRE_SECONDS,
}
if REDIS_PASSWORD:
    _huey_connection["password"] = REDIS_PASSWORD
if REDIS_USE_TLS:
    # redis-py: ssl connections require the SSL connection class.
    import ssl as _ssl

    _huey_connection["connection_class"] = __import__(
        "redis.connection", fromlist=["SSLConnection"]
    ).SSLConnection
    _huey_connection["ssl_cert_reqs"] = _ssl.CERT_NONE

HUEY = {
    "name": HUEY_NAME,
    "huey_class": "huey.RedisExpireHuey",
    "results": True,
    "store_none": False,
    "immediate": HUEY_IMMEDIATE,
    "utc": True,
    "blocking": True,
    "connection": _huey_connection,
    "consumer": {
        "workers": 2,
        # thread: works on Windows. process: not picklable on Windows (spawn).
        "worker_type": "thread",
        "scheduler_interval": 1,
        "periodic": not HUEY_IMMEDIATE,
    },
}


def _build_redis_url(db: int) -> str:
    """Compose a redis[s]:// URL for cache / limiter from the shared Redis config."""
    scheme = "rediss" if REDIS_USE_TLS else "redis"
    auth = f":{REDIS_PASSWORD}@" if REDIS_PASSWORD else ""
    return f"{scheme}://{auth}{HUEY_REDIS_HOST}:{HUEY_REDIS_PORT}/{db}"

# Django cache: Redis shares abuse counters across Gunicorn workers.
# LocMem for DEBUG / tests / HUEY_IMMEDIATE, or when DJANGO_CACHE_USE_REDIS=0.
_DJANGO_CACHE_URL = env("DJANGO_CACHE_URL", default="")
_USE_REDIS_CACHE = env.bool(
    "DJANGO_CACHE_USE_REDIS",
    default=(
        bool(_DJANGO_CACHE_URL)
        or (not DEBUG and not HUEY_IMMEDIATE and not _IN_TEST)
    ),
)
if _USE_REDIS_CACHE:
    _cache_location = _DJANGO_CACHE_URL or _build_redis_url(
        env.int("DJANGO_CACHE_REDIS_DB", default=1)
    )
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": _cache_location,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "OPTIONS": {"MAX_ENTRIES": 1000},
        }
    }

# --- Autonomous Apply Agent ---
# When true, resolve_and_detect uses a deterministic URL map (CI/unit tests).
# Default false so real Indeed/LinkedIn aggregator URLs use live Playwright resolution.
APPLY_USE_MOCK_RESOLVER = env.bool("APPLY_USE_MOCK_RESOLVER", default=False)
# Max concurrent browser automation steps (keep low; Chromium is memory-heavy).
APPLY_BROWSER_CONCURRENCY = env.int("APPLY_BROWSER_CONCURRENCY", default=2)
# Hard wall-clock cap per browser-touching orchestrator step (seconds).
APPLY_BROWSER_STEP_TIMEOUT_SECONDS = env.int("APPLY_BROWSER_STEP_TIMEOUT_SECONDS", default=360)
# When False, Playwright and browser-use open a visible Chromium window (dev only; Huey must run locally).
APPLY_BROWSER_HEADLESS = env.bool("APPLY_BROWSER_HEADLESS", default=False)

# --- LLM rate limits (Redis, shared across workers). Only providers listed in
# LLM_RATE_LIMIT_BY_PROVIDER are throttled; tune via env vars.
LLM_RATE_LIMIT_ENABLED = env.bool("LLM_RATE_LIMIT_ENABLED", default=True)
# Fail-open only in DEBUG by default; production should fail closed when Redis is down.
LLM_RATE_LIMIT_FAIL_OPEN = env.bool("LLM_RATE_LIMIT_FAIL_OPEN", default=DEBUG)
LLM_RATE_LIMIT_MAX_WAIT_SECONDS = env.int("LLM_RATE_LIMIT_MAX_WAIT_SECONDS", default=120)
LLM_RATE_LIMIT_REDIS_URL = env("LLM_RATE_LIMIT_REDIS_URL", default="")
LLM_RATE_LIMIT_REDIS_DB = env.int("LLM_RATE_LIMIT_REDIS_DB", default=HUEY_REDIS_DB)
LLM_RATE_LIMIT_GROQ_RPM = env.int("LLM_RATE_LIMIT_GROQ_RPM", default=30)
LLM_RATE_LIMIT_GROQ_TPM = env.int("LLM_RATE_LIMIT_GROQ_TPM", default=6000)
LLM_RATE_LIMIT_OPENAI_RPM = env.int("LLM_RATE_LIMIT_OPENAI_RPM", default=60)
LLM_RATE_LIMIT_OPENAI_TPM = env.int("LLM_RATE_LIMIT_OPENAI_TPM", default=90_000)

LLM_RATE_LIMIT_BY_PROVIDER = {
    "Groq": (LLM_RATE_LIMIT_GROQ_RPM, LLM_RATE_LIMIT_GROQ_TPM),
    "OpenAI": (LLM_RATE_LIMIT_OPENAI_RPM, LLM_RATE_LIMIT_OPENAI_TPM),
}

# Pipeline resume summary — LLM batch extraction (OpenAI)
PIPELINE_LLM_BATCH_SIZE = env.int("PIPELINE_LLM_BATCH_SIZE", default=1)
# Local Ollama: multi-JD batches mean one huge prompt + long generation; progress stays at 0 until the batch returns.
PIPELINE_LLM_BATCH_SIZE_OLLAMA_LOCAL = env.int("PIPELINE_LLM_BATCH_SIZE_OLLAMA_LOCAL", default=1)
PIPELINE_LLM_MAX_TOKENS_PER_MINUTE = env.int("PIPELINE_LLM_MAX_TOKENS_PER_MINUTE", default=90000)
PIPELINE_LLM_REQUESTS_PER_MINUTE = env.int("PIPELINE_LLM_REQUESTS_PER_MINUTE", default=60)
# Per batch: total HTTP attempts for transient 429/5xx (1 initial + N-1 retries). Default 3 = two retries.
PIPELINE_LLM_HTTP_MAX_ATTEMPTS = env.int("PIPELINE_LLM_HTTP_MAX_ATTEMPTS", default=3)
# If first response is not valid JSON (e.g. model emitted thinking tags), one extra LLM call with a strict JSON-only tail.
PIPELINE_LLM_JSON_PARSE_RETRY = env.bool("PIPELINE_LLM_JSON_PARSE_RETRY", default=True)
# After a failed parse (and optional retry), write empty skill arrays for the batch instead of failing the run.
PIPELINE_LLM_USE_EMPTY_SKILLS_AFTER_RETRIES = env.bool("PIPELINE_LLM_USE_EMPTY_SKILLS_AFTER_RETRIES", default=True)
# After all JD batches finish: one LLM pass to merge near-duplicates (e.g. architect / architected). Set false to skip.
PIPELINE_LLM_CONSOLIDATE = env.bool("PIPELINE_LLM_CONSOLIDATE", default=True)
# Max strings per key sent into consolidation (sorted); avoids huge prompts on very large runs.
PIPELINE_LLM_CONSOLIDATE_MAX_ITEMS_PER_KEY = env.int("PIPELINE_LLM_CONSOLIDATE_MAX_ITEMS_PER_KEY", default=400)
# Drop keywords that appear fewer than this many times across all batch lines (1 = keep all).
PIPELINE_LLM_KEYWORD_MIN_COUNT = env.int("PIPELINE_LLM_KEYWORD_MIN_COUNT", default=1)

