"""Settings for the test suite: deterministic, no network, no env required."""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("MAIL_DOMAIN", "imoth.test")
os.environ["EMAIL_PROVIDER"] = "console"
os.environ.setdefault("INBOUND_WEBHOOK_SECRET", "hook:secret")

from .settings import *  # noqa: E402,F401,F403
import tempfile  # noqa: E402

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": tempfile.mkdtemp(prefix="imoth-test-media-"), "base_url": None},
    },
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
