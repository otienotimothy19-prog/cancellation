"""
IMOTH Cancellations settings.

All configuration comes from environment variables (see .env.example and README).
"""

import os
import sys
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# The build step (`manage.py collectstatic`, run automatically by the DigitalOcean/Heroku Python
# buildpack) only copies static files. It must work without runtime secrets, which App Platform
# usually provides at run time only. Every other command and the web server still require them.
BUILD_STEP = len(sys.argv) > 1 and sys.argv[1] == "collectstatic"


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        if BUILD_STEP:
            return f"build-step-placeholder-{name.lower()}"
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
                "cancellations.context_processors.navigation",
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
LOGIN_REDIRECT_URL = "cancellations:dashboard"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "en-gb"
TIME_ZONE = env("TIME_ZONE", "Africa/Nairobi")
USE_I18N = True
USE_TZ = True

# --- Static files (served by WhiteNoise) ------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
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
EMAIL_PROVIDER = env("EMAIL_PROVIDER", "console").lower()  # zoho | postmark | mailgun | console
MAIL_DOMAIN = env("MAIL_DOMAIN", "example.com")  # our sending/receiving domain
CANCELLATIONS_MAILBOX = env("CANCELLATIONS_MAILBOX", "cancellations")  # local part
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", f"IMOTH Cancellations <{CANCELLATIONS_MAILBOX}@{MAIL_DOMAIN}>")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

ANYMAIL = {
    # "user:password" that the provider must send as HTTP Basic auth on the inbound webhook.
    "WEBHOOK_SECRET": env("INBOUND_WEBHOOK_SECRET", ""),
}
# Mailbox account (Zoho Mail): used for SMTP sending and IMAP polling of replies.
MAILBOX_USER = env("MAILBOX_USER", f"{CANCELLATIONS_MAILBOX}@{MAIL_DOMAIN}")
MAILBOX_PASSWORD = env("MAILBOX_PASSWORD", "")  # Zoho app-specific password
IMAP_HOST = env("IMAP_HOST", "imappro.zoho.com")
IMAP_PORT = int(env("IMAP_PORT", "993"))
IMAP_FOLDER = env("IMAP_FOLDER", "INBOX")
IMAP_LOOKBACK_DAYS = int(env("IMAP_LOOKBACK_DAYS", "14"))  # first run / UIDVALIDITY reset
MAIL_POLL_INTERVAL = int(env("MAIL_POLL_INTERVAL", "60"))  # seconds between polls in --loop mode

if EMAIL_PROVIDER == "zoho":
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = env("SMTP_HOST", "smtppro.zoho.com")
    EMAIL_PORT = int(env("SMTP_PORT", "465"))
    EMAIL_USE_SSL = EMAIL_PORT == 465
    EMAIL_USE_TLS = EMAIL_PORT == 587
    EMAIL_HOST_USER = MAILBOX_USER
    EMAIL_HOST_PASSWORD = env("MAILBOX_PASSWORD", required=True)
    EMAIL_TIMEOUT = 30
elif EMAIL_PROVIDER == "postmark":
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
# authserv-id of the Authentication-Results header your provider writes (the text before the
# first ";"). Headers with any other id are ignored because senders can forge them.
INBOUND_AUTHSERV_ID = env("INBOUND_AUTHSERV_ID", "mx.zohomail.com" if EMAIL_PROVIDER == "zoho" else "")
# Which header carries the receiving server's verdict. Zoho may stamp
# "ARC-Authentication-Results" instead; check a real reply with `manage.py check_mailbox`.
INBOUND_AUTH_HEADER = env("INBOUND_AUTH_HEADER", "Authentication-Results")

# --- Exports / API ------------------------------------------------------------
CURRENCY = env("CURRENCY", "KES")
# Bearer tokens accepted by /api/confirmed-cancellations/ (comma separated, one per consuming system).
EXPORT_API_TOKENS = env_list("EXPORT_API_TOKENS")

# --- SLA ----------------------------------------------------------------------
# Reminder goes to the user who created the case (their email), else this address.
SLA_ALERT_EMAIL = env("SLA_ALERT_EMAIL", "")
SLA_STALE_AFTER_REMINDER_HOURS = int(env("SLA_STALE_AFTER_REMINDER_HOURS", "24"))
SLA_CHECK_INTERVAL = int(env("SLA_CHECK_INTERVAL", "300"))  # seconds, in the worker loop
APP_BASE_URL = env("APP_BASE_URL", "")  # e.g. https://cancellations.imoth.co.ke (links in reminder emails)
