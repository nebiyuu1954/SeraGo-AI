"""
Django settings for SeraGo-AI project.

Standalone matching service. It provides:
  - Feature 1: when a job is published, score eligible talents (For You)
  - Feature 2: when a talent applies, score the application immediately

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
# "default" is the SeraGo-AI database (its own PostgreSQL DB — set via
# ConnectionStrings__DefaultConnection, the same .NET-style env name the
# backend uses, or via individual DB_* vars).
# "sectors" is an optional read-only alias for the .NET backend's DB,
# which owns the canonical "Sectors" table the classifier reads live (no sync).

def _db_from_npgsql(value: str) -> dict:
    """Parse a postgresql:// URI or Npgsql key=value string into Django DB settings."""
    value = value.strip()
    if value.startswith(("postgresql://", "postgres://")):
        from urllib.parse import parse_qs, unquote, urlsplit
        u = urlsplit(value)
        db = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": u.path.lstrip("/") or "",
            "USER": unquote(u.username or ""),
            "PASSWORD": unquote(u.password or ""),
            "HOST": u.hostname or "",
            "PORT": str(u.port or ""),
            "OPTIONS": {},
        }
        qs = parse_qs(u.query)
        if qs.get("sslmode"):
            db["OPTIONS"]["sslmode"] = qs["sslmode"][0]
        return db

    db = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "", "USER": "", "PASSWORD": "", "HOST": "", "PORT": "", "OPTIONS": {},
    }
    for pair in value.split(";"):
        if "=" not in pair:
            continue
        key, val = pair.split("=", 1)
        key, val = key.strip().lower(), val.strip()
        if not val:
            continue
        if key == "host":
            db["HOST"] = val
        elif key == "database":
            db["NAME"] = val
        elif key == "username":
            db["USER"] = val
        elif key == "password":
            db["PASSWORD"] = val
        elif key == "port":
            db["PORT"] = val
        elif key in ("ssl mode", "sslmode"):
            db["OPTIONS"]["sslmode"] = val.lower()
    return db


_ai_connection_string = os.environ.get("ConnectionStrings__DefaultConnection", "").strip()
if _ai_connection_string:
    DATABASES = {"default": _db_from_npgsql(_ai_connection_string)}
else:
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

# Neon requires SSL — default sslmode=require for PostgreSQL
if DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql":
    DATABASES["default"]["OPTIONS"].setdefault("sslmode", os.environ.get("DB_SSLMODE", "require"))

# Read-only "sectors" alias → the .NET backend's DB (owns "Sectors"). Optional.
if os.environ.get("SECTORS_DB_HOST"):
    DATABASES["sectors"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("SECTORS_DB_NAME", ""),
        "USER": os.environ.get("SECTORS_DB_USER", ""),
        "PASSWORD": os.environ.get("SECTORS_DB_PASSWORD", ""),
        "HOST": os.environ.get("SECTORS_DB_HOST", ""),
        "PORT": os.environ.get("SECTORS_DB_PORT", ""),
        "OPTIONS": {"sslmode": os.environ.get("SECTORS_DB_SSLMODE", "require")},
    }


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


# ── Matching Engine ───────────────────────────────────────────────────

# Sentence-transformer model (loaded lazily, ~80MB RAM)
MATCHING_MODEL_NAME = os.environ.get("MATCHING_MODEL_NAME", "all-MiniLM-L6-v2")


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
        "ai_service.classifier": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "ai_service.views": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "serago_ai.security": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "serago_ai.debug": {
            "handlers": ["console"],
            "level": "DEBUG",
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


# ── AI / LLM provider ───────────────────────────────────────────────
# Groq (OpenAI-compat) is the default LLM for AI classification.
# When GROQ_API_KEY (or GROQ_CONSOLE_API_KEY, the name the .NET backend
# uses) is set the ai_service can call the LLM directly. When it is absent
# in production, classify calls raise a clear error at startup / request
# time rather than failing silently.
GROQ_API_KEY = (
    os.environ.get("GROQ_API_KEY", "").strip()
    or os.environ.get("GROQ_CONSOLE_API_KEY", "").strip()
)
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b").strip() or "openai/gpt-oss-120b"

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
        "Scores jobs against talents (For You) when a job is published, "
        "and scores applications when a talent applies."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "TAGS": [
        {"name": "Health", "description": "Service health checks"},
        {"name": "Webhooks", "description": "Endpoints called by the .NET backend (require X-Api-Key)"},
        {"name": "Scores", "description": "Read compatibility scores"},
    ],
}
