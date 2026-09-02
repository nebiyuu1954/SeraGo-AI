"""
Django settings for SeraGo-AI project.

This is a standalone service that provides:
  - Matching engine (job ↔ talent compatibility scoring)
  - AI services (future: resume parsing, cover letter generation, etc.)

It shares the PostgreSQL database with the .NET backend via separate
connection, but its own Django-managed tables live in a dedicated schema.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# ── Security ──────────────────────────────────────────────────────────

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-change-me-in-production",
)

DEBUG = env_bool("DJANGO_DEBUG", default=True)

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]


# ── Apps ──────────────────────────────────────────────────────────────

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # API documentation
    "rest_framework",
    "drf_spectacular",
    # SeraGo apps
    "matching_engine",
    "ai_service",
]

MIDDLEWARE = [
    # Security (runs first — sets HTTPS headers)
    "django.middleware.security.SecurityMiddleware",
    # Request logging (runs early to capture timing)
    "serago_ai.middleware.RequestLoggingMiddleware",
    # Rate limiting (before common middleware to block early)
    "serago_ai.middleware.RateLimitMiddleware",
    # API key auth (protects webhook endpoints)
    "serago_ai.middleware.ApiKeyMiddleware",
    # Django defaults
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "serago_ai.urls"

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
            ],
        },
    },
]

WSGI_APPLICATION = "serago_ai.wsgi.application"


# ── Database ──────────────────────────────────────────────────────────
# Uses the SAME PostgreSQL database as the .NET backend, but Django
# manages its own tables (matching_engine_*, ai_service_*).
# Set DB_* env vars to point at the shared Neon database.

DATABASES = {
    "default": {
        "ENGINE": os.environ.get("DB_ENGINE", "django.db.backends.sqlite3"),
        "NAME": os.environ.get("DB_NAME", str(BASE_DIR / "db.sqlite3")),
        "USER": os.environ.get("DB_USER", ""),
        "PASSWORD": os.environ.get("DB_PASSWORD", ""),
        "HOST": os.environ.get("DB_HOST", ""),
        "PORT": os.environ.get("DB_PORT", ""),
        "OPTIONS": {},
    }
}

# Neon requires SSL — add sslmode=require for PostgreSQL
if DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql":
    DATABASES["default"]["OPTIONS"]["sslmode"] = os.environ.get("DB_SSLMODE", "require")


# ── Password validation ───────────────────────────────────────────────

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ── Internationalization ──────────────────────────────────────────────

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Addis_Ababa"
USE_I18N = True
USE_TZ = True


# ── Static files ──────────────────────────────────────────────────────

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"


# ── Default primary key ──────────────────────────────────────────────

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ── CORS (allow .NET backend to call webhooks) ────────────────────────

CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "https://localhost:5191,http://localhost:5191").split(",")
    if origin.strip()
]


# ── Celery ────────────────────────────────────────────────────────────

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE

# ── Matching Engine ───────────────────────────────────────────────────

# Sentence-transformer model (loaded lazily, ~80MB RAM)
MATCHING_MODEL_NAME = os.environ.get("MATCHING_MODEL_NAME", "all-MiniLM-L6-v2")
HOT_SECTOR_THRESHOLD = int(os.environ.get("HOT_SECTOR_THRESHOLD", "50"))
MAX_ON_DEMAND_CONCURRENT = int(os.environ.get("MAX_ON_DEMAND_CONCURRENT", "3"))


# ── AI Service (future) ──────────────────────────────────────────────

# LLM provider: "openai" | "groq" | "openrouter" | "huggingface"
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai")

# API keys (set in .env)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-70b-versatile")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

HUGGINGFACE_API_KEY = os.environ.get("HUGGINGFACE_API_KEY", "")
HUGGINGFACE_MODEL = os.environ.get("HUGGINGFACE_MODEL", "meta-llama/Llama-2-7b-chat-hf")


# ── Logging ───────────────────────────────────────────────────────────

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "matching_engine": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "ai_service": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "serago_ai.security": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}


# ── Security (production hardened) ────────────────────────────────────

# HTTPS settings — enable on Render (production)
if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", default=True)
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Prevent browser from MIME-sniffing
SECURE_CONTENT_TYPE_NOSNIFF = True

# Clickjacking protection
X_FRAME_OPTIONS = "DENY"

# ── API Key (shared secret with .NET backend) ────────────────────────
# Set MATCHING_API_KEY in .env — same value on both .NET and Django.
# Used by ApiKeyMiddleware to authenticate webhook calls.
MATCHING_API_KEY = os.environ.get("MATCHING_API_KEY", "")

if not MATCHING_API_KEY and not DEBUG:
    import warnings
    warnings.warn(
        "MATCHING_API_KEY is not set! Webhook endpoints are unprotected. "
        "Set it in .env for production.",
        stacklevel=1,
    )


# ── Django REST Framework ─────────────────────────────────────────────
REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}


# ── drf-spectacular (OpenAPI 3.0 / Swagger) ──────────────────────────
SPECTACULAR_SETTINGS = {
    "TITLE": "SeraGo AI — Matching Engine API",
    "DESCRIPTION": (
        "AI-powered job-talent matching service for SeraGo. "
        "Provides sector classification, compatibility scoring, "
        "and real-time match recommendations."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "TAGS": [
        {"name": "Health", "description": "Service health checks"},
        {"name": "Sector Classification", "description": "4-layer job sector classification (alias → keyword → embedding → LLM)"},
        {"name": "Webhooks", "description": "Endpoints called by the .NET backend (require X-Api-Key)"},
        {"name": "Matching", "description": "On-demand matching and score queries"},
        {"name": "Scores", "description": "Read compatibility scores"},
    ],
}
