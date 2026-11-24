"""One sign-in with the Production Portal.

The portal (where staff sign in with password and two-factor) posts a pass to /sso/portal/:
base64url(JSON claims) + "." + base64url(HMAC-SHA256(claims, CANCELLATIONS_SSO_SECRET)).
A pass is accepted only if the signature matches, it was issued by the portal for this app,
it has not expired (60 seconds) and it has not been used before.

The pass says who the person is (their email). Access is still decided here: only active
members of the "cancellations" group get in, and administrator accounts (superusers) keep
their own password sign-in, so a portal account can never open the Django admin.
"""

import base64
import binascii
import datetime as dt
import hashlib
import hmac
import json
import re

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

ISSUER = "imoth-production-portal"
AUDIENCE = "imoth-cancellations"
MAX_LIFETIME = 300  # seconds; the portal issues 60
CLOCK_SKEW = 30
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SsoError(Exception):
    pass


def _b64decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(body):
    digest = hmac.new(settings.CANCELLATIONS_SSO_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def make_token(claims):
    """Build a pass (for tests and local checks; the portal makes the real ones)."""
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{body}.{_sign(body)}"


def verify(token, now=None):
    """The pass's claims, or SsoError. Marks the pass used (single use)."""
    from ..models import SsoPass

    if not settings.PORTAL_SSO_ENABLED:
        raise SsoError("Signing in from the Production Portal is not switched on.")
    token = (token or "").strip()
    body, _, sig = token.partition(".")
    if not body or not sig or len(token) > 2000:
        raise SsoError("The sign-in link is incomplete.")
    if not hmac.compare_digest(sig, _sign(body)):
        raise SsoError("The sign-in link was not issued by the Production Portal.")
    try:
        claims = json.loads(_b64decode(body))
    except (ValueError, binascii.Error):
        raise SsoError("The sign-in link is damaged.")
    if not isinstance(claims, dict) or claims.get("iss") != ISSUER or claims.get("aud") != AUDIENCE:
        raise SsoError("The sign-in link is not meant for this app.")
    now_ts = (now or timezone.now()).timestamp()
    iat, exp = claims.get("iat"), claims.get("exp")
    if not isinstance(iat, (int, float)) or not isinstance(exp, (int, float)) or not 0 < exp - iat <= MAX_LIFETIME:
        raise SsoError("The sign-in link is damaged.")
    if iat > now_ts + CLOCK_SKEW:
        raise SsoError("The sign-in link is not valid yet. Check the server clocks.")
    if now_ts > exp + CLOCK_SKEW:
        raise SsoError("The sign-in link has expired. Open Cancellations from the Production Portal again.")
    email, jti = str(claims.get("sub") or "").strip().lower(), str(claims.get("jti") or "")
    if not EMAIL.match(email) or not 16 <= len(jti) <= 64:
        raise SsoError("The sign-in link is damaged.")
    SsoPass.objects.filter(expires_at__lt=timezone.now()).delete()
    try:
        with transaction.atomic():
            SsoPass.objects.create(jti=jti, email=email,
                                   expires_at=dt.datetime.fromtimestamp(exp + CLOCK_SKEW, tz=dt.timezone.utc))
    except IntegrityError:
        raise SsoError("That sign-in link has already been used. Open Cancellations from the Production Portal again.")
    claims["sub"] = email
    return claims


def user_for(claims):
    """(user, problem). The account with this email; one without access is created if there is none."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    email = claims["sub"]
    matches = list(User.objects.filter(email__iexact=email)[:2])
    if len(matches) > 1:
        return None, f"More than one Cancellations account uses {email}. Ask an administrator to fix this."
    if matches:
        user = matches[0]
    else:
        if User.objects.filter(username__iexact=email[:150]).exists():
            return None, f"An account named {email} already exists with a different email. Ask an administrator to fix this."
        first, _, last = str(claims.get("name") or "").strip().partition(" ")
        user = User(username=email[:150], email=email, first_name=first[:150], last_name=last[:150])
        user.set_unusable_password()
        user.save()
    if not user.is_active:
        return None, "Your Cancellations account is switched off. Ask an administrator."
    if user.is_superuser:
        return None, "Administrator accounts sign in here with their own password."
    return user, None
