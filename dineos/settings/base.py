"""
Base Django settings shared across all environments.
"""
from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*"])

# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]
THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    "channels",
]
LOCAL_APPS = [
    "apps.platform",
    "apps.authentication",
    "apps.restaurant",
    "apps.tables",
    "apps.menu",
    "apps.orders",
    "apps.billing",
    "apps.notifications",
    "apps.kitchen",
    "apps.inventory",
    "apps.dashboard",
    "apps.websockets",
]
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.tenancy.TenantResolverMiddleware",
]

ROOT_URLCONF = "dineos.urls"
WSGI_APPLICATION = "dineos.wsgi.application"
ASGI_APPLICATION = "dineos.asgi.application"

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASES = {
    "default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR}/db.sqlite3")
}

# ---------------------------------------------------------------------------
# Custom user model
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "authentication.User"

# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static / media
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "core.pagination.DineOSPageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}

# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env.int("JWT_ACCESS_MINUTES", 1440)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env.int("JWT_REFRESH_DAYS", 7)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_ALLOW_ALL_ORIGINS = env.bool("CORS_ALLOW_ALL_ORIGINS", default=True)
CORS_ALLOW_CREDENTIALS = True

# ---------------------------------------------------------------------------
# Django Channels (WebSockets) — Redis channel layer for prod, in-memory for dev
# ---------------------------------------------------------------------------
_CHANNEL_REDIS_URL = env("REDIS_URL", default="")
if _CHANNEL_REDIS_URL.startswith("rediss://"):
    _channel_config = {"hosts": [{"address": _CHANNEL_REDIS_URL, "ssl_cert_reqs": None}]}
elif _CHANNEL_REDIS_URL:
    _channel_config = {"hosts": [_CHANNEL_REDIS_URL]}
else:
    _channel_config = None

if _channel_config:
    CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels_redis.core.RedisChannelLayer", "CONFIG": _channel_config}
    }
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# ---------------------------------------------------------------------------
# Kitchen Display Screen (device auth, no user login) — reused pattern from v1
# ---------------------------------------------------------------------------
KDS_HEADER_NAME = "HTTP_X_KDS_API_KEY"

# ---------------------------------------------------------------------------
# Celery (reserved for Phase 2 AI tasks; not used by Phase 1 flows)
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = env("REDIS_URL", default="memory://")
CELERY_RESULT_BACKEND = env("REDIS_RESULT_BACKEND", default="cache+memory://")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_ALWAYS_EAGER", default=False)

# ---------------------------------------------------------------------------
# AI / Groq (reserved for Phase 2)
# ---------------------------------------------------------------------------
GROQ_API_KEY = env("GROQ_API_KEY", default="")
GROQ_MODEL = env("GROQ_MODEL", default="llama-3.3-70b-versatile")
LLM_PROVIDER = env("LLM_PROVIDER", default="mock")

# ---------------------------------------------------------------------------
# Platform-wide tenant defaults — applied by TenantViewSet.perform_create()
# when the Super Admin creates a new client restaurant without specifying
# its own rate.
# ---------------------------------------------------------------------------
DEFAULT_GST_PERCENTAGE = env.float("DEFAULT_GST_PERCENTAGE", default=5.0)
DEFAULT_SERVICE_CHARGE_PERCENTAGE = env.float("DEFAULT_SERVICE_CHARGE_PERCENTAGE", default=0.0)

# ---------------------------------------------------------------------------
# No email/SMS delivery is wired up yet. Until this is True, every
# "sent" secret (2FA code, password-reset/invite token) is a fixed, known
# value instead of a random one — see apps.platform.models._generate_2fa_code
# and apps.authentication.models._generate_reset_token — so the full
# verification flow is still testable end-to-end without a real inbox.
# Flip this on (no other code changes needed) once real delivery exists.
# ---------------------------------------------------------------------------
EMAIL_DELIVERY_ENABLED = env.bool("EMAIL_DELIVERY_ENABLED", default=False)
COMMON_VERIFICATION_TOKEN = env("COMMON_VERIFICATION_TOKEN", default="COMMON-TEST-TOKEN")
COMMON_VERIFICATION_CODE = env("COMMON_VERIFICATION_CODE", default="123456")

# ---------------------------------------------------------------------------
# Logging — send everything to stdout so Render captures it
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"verbose": {"format": "[{levelname}] {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "verbose"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "daphne": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
