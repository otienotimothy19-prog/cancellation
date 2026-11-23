"""
IMOTH Cancellations settings.

All configuration comes from environment variables (see .env.example and README).
"""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        raise ImproperlyConfigured(f"Environment variable {name} is required")
    return value


def env_bool(name, default=False):
    return str(os.environ.get(name, default)).strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DEBUG = env_bool("DEBUG", False)
SECRET_KEY = env("SECRET_KEY", "dev-insecure-key-change-me" if DEBUG else None, required=True)
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "anymail",
    "cancellations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
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

WSGI_APPLICATION = "config.wsgi.application"

# --- Database ---------------------------------------------------------------
# PostgreSQL in every deployed environment via DATABASE_URL. Falls back to a local
# SQLite file only when DATABASE_URL is unset (handy for quick local runs/tests).
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=int(env("DB_CONN_MAX_AGE", "60")),
        ssl_require=env_bool("DB_SSL_REQUIRE", False),
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "cancellations:case_list"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "en-gb"
TIME_ZONE = env("TIME_ZONE", "Africa/Nairobi")
USE_I18N = True
USE_TZ = True

# --- Static files (served by WhiteNoise) ------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# --- Private file storage ---------------------------------------------------
# Case documents are NEVER served directly by URL; they are streamed through an
# authenticated view. Use S3-compatible object storage (DigitalOcean Spaces) in
# production because App Platform's filesystem is ephemeral.
if env("AWS_STORAGE_BUCKET_NAME"):
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": env("AWS_STORAGE_BUCKET_NAME"),
            "endpoint_url": env("AWS_S3_ENDPOINT_URL"),
            "region_name": env("AWS_S3_REGION_NAME"),
            "access_key": env("AWS_ACCESS_KEY_ID"),
            "secret_key": env("AWS_SECRET_ACCESS_KEY"),
            "location": env("AWS_LOCATION", "case-documents"),
            "default_acl": "private",
            "querystring_auth": True,
            "file_overwrite": False,
        },
    }
else:
    # Local folder outside any web root; there is no MEDIA_URL route.
    STORAGES["default"] = {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": env("PRIVATE_MEDIA_ROOT", str(BASE_DIR / "private_media")), "base_url": None},
    }

MAX_UPLOAD_MB = int(env("MAX_UPLOAD_MB", "10"))
DATA_UPLOAD_MAX_MEMORY_SIZE = 30 * 1024 * 1024  # inbound webhooks carry base64 attachments
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# --- Email ------------------------------------------------------------------
EMAIL_PROVIDER = env("EMAIL_PROVIDER", "console").lower()  # postmark | mailgun | console
MAIL_DOMAIN = env("MAIL_DOMAIN", "example.com")  # our sending/receiving domain
CANCELLATIONS_MAILBOX = env("CANCELLATIONS_MAILBOX", "cancellations")  # local part
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", f"IMOTH Cancellations <{CANCELLATIONS_MAILBOX}@{MAIL_DOMAIN}>")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

ANYMAIL = {
    # "user:password" that the provider must send as HTTP Basic auth on the inbound webhook.
    "WEBHOOK_SECRET": env("INBOUND_WEBHOOK_SECRET", ""),
}
if EMAIL_PROVIDER == "postmark":
    EMAIL_BACKEND = "anymail.backends.postmark.EmailBackend"
    ANYMAIL["POSTMARK_SERVER_TOKEN"] = env("POSTMARK_SERVER_TOKEN", required=True)
elif EMAIL_PROVIDER == "mailgun":
    EMAIL_BACKEND = "anymail.backends.mailgun.EmailBackend"
    ANYMAIL["MAILGUN_API_KEY"] = env("MAILGUN_API_KEY", required=True)
    ANYMAIL["MAILGUN_SENDER_DOMAIN"] = env("MAILGUN_SENDER_DOMAIN", MAIL_DOMAIN)
    ANYMAIL["MAILGUN_WEBHOOK_SIGNING_KEY"] = env("MAILGUN_WEBHOOK_SIGNING_KEY", required=True)
    if env("MAILGUN_API_URL"):
        ANYMAIL["MAILGUN_API_URL"] = env("MAILGUN_API_URL")  # e.g. https://api.eu.mailgun.net/v3
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# --- Security ---------------------------------------------------------------
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", False)  # App Platform redirects at the edge
SECURE_HSTS_SECONDS = int(env("SECURE_HSTS_SECONDS", "0"))
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_AGE = 8 * 60 * 60

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}

# Inbound replies are attached to a case only if the sender is one of the insurer's known
# addresses AND the receiving provider recorded SPF/DMARC pass for it. Set to false only
# if your provider adds no authentication headers (replies then still need a known address).
INBOUND_REQUIRE_SENDER_AUTH = env_bool("INBOUND_REQUIRE_SENDER_AUTH", True)
