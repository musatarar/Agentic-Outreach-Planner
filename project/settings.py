"""Django settings. Values come from the environment; see .env.example."""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env (KEY=value lines) without requiring python-dotenv
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# LLM provider/model/key selection lives in the database (LLMConfiguration),
# managed via /api/llm/*. These env vars are the fallback when no key is
# stored (see project/app/services/llm/config.py):
#   claude   -> ANTHROPIC_API_KEY (or CLAUDE_API_KEY, aliased in config.py)
#   chatgpt  -> OPENAI_API_KEY
#   deepseek -> DEEPSEEK_API_KEY
#   groq     -> GROQ_API_KEY

# Grounding verifier strictness for generated outreach copy (MUS-22):
#   off | standard (default) | strict. See project/app/services/verify.py.
COPY_VERIFY_LEVEL = os.environ.get("COPY_VERIFY_LEVEL", "standard")

# --- Planner concurrency, retries and timeouts (MUS-26) -----------------------
# Read here and handed to services/llm/runtime.py as frozen dataclasses --
# nothing under services/llm/ reads Django settings, so those modules stay
# importable without Django configured. The retry defaults are deliberately
# duplicated there (settings must not import app code) and pinned by a test.


def _env_number(name, default, parse, expected):
    """Parse a numeric env var, or return ``default`` when it is unset/blank.

    Blank counts as unset: that is what `.env.example` and docker-compose
    `${VAR:-}` passthrough produce for a value the operator never set.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return parse(raw)
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be {expected}, got {raw!r}.") from None


def _env_int(name, default):
    return _env_number(name, default, int, "a whole number")


def _env_float(name, default):
    return _env_number(name, default, float, "a number")


def _env_list(name):
    """Comma-separated env var -> list of stripped, non-empty entries."""
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


OUTREACH_MAX_IN_FLIGHT = _env_int("OUTREACH_MAX_IN_FLIGHT", 8)
OUTREACH_MAX_ATTEMPTS = _env_int("OUTREACH_MAX_ATTEMPTS", 4)
OUTREACH_INITIAL_BACKOFF_S = _env_float("OUTREACH_INITIAL_BACKOFF_S", 0.5)
OUTREACH_MAX_BACKOFF_S = _env_float("OUTREACH_MAX_BACKOFF_S", 30.0)
OUTREACH_BACKOFF_MULTIPLIER = _env_float("OUTREACH_BACKOFF_MULTIPLIER", 2.0)
# Two nested deadlines: one HTTP attempt, and the whole retry loop for one lead.
OUTREACH_REQUEST_TIMEOUT_S = _env_float("OUTREACH_REQUEST_TIMEOUT_S", 60.0)
OUTREACH_PER_LEAD_TIMEOUT_S = _env_float("OUTREACH_PER_LEAD_TIMEOUT_S", 150.0)

# --- Agentic copy step (MUS-29) ------------------------------------------------
# Gates the tool-calling agent path in plan_outreach(); off means the
# single-shot copy call runs. The three budgets bound one lead's loop:
# provider calls, tool executions, wall-clock seconds.
OUTREACH_AGENT_ENABLED = (os.environ.get("OUTREACH_AGENT_ENABLED") or "").strip().lower() in (
    "1",
    "true",
    "yes",
)
OUTREACH_AGENT_MAX_STEPS = _env_int("OUTREACH_AGENT_MAX_STEPS", 6)
OUTREACH_AGENT_MAX_TOOL_CALLS = _env_int("OUTREACH_AGENT_MAX_TOOL_CALLS", 8)
OUTREACH_AGENT_PER_LEAD_TIMEOUT_S = _env_float("OUTREACH_AGENT_PER_LEAD_TIMEOUT_S", 300.0)


# SECURITY WARNING: keep the secret key used in production secret!
# The key formerly hardcoded here is committed to git and must never be reused.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY is not set. Copy .env.example to .env (it ships a "
        "freshly generated key for local/demo use) or set your own via the "
        "environment for production."
    )

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get("DJANGO_DEBUG", "False") == "True"

ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS")

# Origins (scheme included, e.g. https://demo.example.com) whose POSTs the CSRF
# check trusts. Required whenever TLS terminates in front of the app -- a proxy
# or tunnel -- or the browser's https Origin never matches the http request the
# app sees and every authenticated POST is a 403.
CSRF_TRUSTED_ORIGINS = _env_list("DJANGO_CSRF_TRUSTED_ORIGINS")


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "project.app",
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

ROOT_URLCONF = "project.urls"

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

WSGI_APPLICATION = "project.wsgi.application"


# Database
# DATABASE_URL selects the backend (docker-compose.yml sets it for Postgres).
# Unset or blank falls back to SQLite; blank must count as unset, since
# dj_database_url.config() would otherwise return the dummy backend.

_database_url = os.environ.get("DATABASE_URL", "").strip()
DATABASES = {
    "default": dj_database_url.parse(
        _database_url or f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

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
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = "static/"

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- Magic-link auth (MUS-37) -------------------------------------------------
LOGIN_ALLOWED_EMAILS = {
    e.strip().lower() for e in os.environ.get("LOGIN_ALLOWED_EMAILS", "").split(",") if e.strip()
}
LOGIN_LINK_DELIVERY = os.environ.get("LOGIN_LINK_DELIVERY", "console")  # console | email
LOGIN_TOKEN_TTL_SECONDS = int(os.environ.get("LOGIN_TOKEN_TTL_SECONDS", "900"))
LOGIN_LINK_BASE_URL = os.environ.get("LOGIN_LINK_BASE_URL", "http://127.0.0.1:8000")
LOGIN_RATE_LIMIT_EMAIL = os.environ.get("LOGIN_RATE_LIMIT_EMAIL", "5/hour")
LOGIN_RATE_LIMIT_IP = os.environ.get("LOGIN_RATE_LIMIT_IP", "20/hour")
LOGIN_RESEND_COOLDOWN_SECONDS = int(os.environ.get("LOGIN_RESEND_COOLDOWN_SECONDS", "30"))

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "project.app.authentication.SessionAuthenticationWith401",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "EXCEPTION_HANDLER": "project.app.exceptions.contract_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.ScopedRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {
        "auth_request_ip": LOGIN_RATE_LIMIT_IP,
        "auth_consume_ip": "60/hour",
        "queue_verify": "120/min",
    },
}

# Explicit `project.app` handler: Django's default only attaches one to the
# `django` logger, so INFO records (e.g. console sign-in links) get dropped.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "project.app": {
            "handlers": ["console"],
            "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO"),
        },
    },
}

# --- Triage queue (MUS-39) ----------------------------------------------------
TRIAGE_UNDO_WINDOW_SECONDS = int(os.environ.get("TRIAGE_UNDO_WINDOW_SECONDS", "300"))
TRIAGE_SNOOZE_ON_ACTIVITY_BACKSTOP_DAYS = int(
    os.environ.get("TRIAGE_SNOOZE_ON_ACTIVITY_BACKSTOP_DAYS", "14")
)
TRIAGE_TIMEZONE = os.environ.get("TRIAGE_TIMEZONE", "UTC")
