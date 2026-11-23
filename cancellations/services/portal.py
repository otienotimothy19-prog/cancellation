"""Read-only lookup of certificate records in the IMOTH Production Portal.

Uses the portal's existing backend API (GET /api/underwriting/v1/records?by=...&q=...),
authenticated with a Bearer token. Only the search endpoint is called: it returns the
cover details we need with contact details masked, so no client phone or email ever
reaches this app. Every lookup is recorded in the portal's own audit log, with the
staff member's email passed in X-Imoth-Staff.
"""

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from ..models import Cover, Insurer

logger = logging.getLogger(__name__)

SEARCH_BY = {
    "registration": "Vehicle registration",
    "policy": "Policy number",
    "certificate": "Certificate number",
    "name": "Client name",
}


class PortalError(Exception):
    """A lookup failed; the message is safe to show to staff."""


def enabled():
    return bool(settings.PRODUCTION_PORTAL_URL and settings.PRODUCTION_PORTAL_TOKEN)


def _get(path, params, staff_email):
    url = f"{settings.PRODUCTION_PORTAL_URL}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {settings.PRODUCTION_PORTAL_TOKEN}",
        "Accept": "application/json",
        "X-Imoth-Staff": staff_email or "",
        "User-Agent": "imoth-cancellations",
    })
    try:
        with urllib.request.urlopen(request, timeout=settings.PRODUCTION_PORTAL_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            body = {}
        logger.warning("Production Portal returned %s for a search", exc.code)
        if exc.code == 400 and body.get("message"):
            raise PortalError(body["message"]) from exc
        raise PortalError({
            401: "The Production Portal rejected our token. PRODUCTION_PORTAL_TOKEN here must equal CANCELLATIONS_API_TOKEN there.",
            403: "The Production Portal does not accept requests from this server (IP allow-list).",
            404: "The Production Portal API is switched off (CANCELLATIONS_API_TOKEN not set there) or the URL is wrong.",
            429: "Too many searches in a minute. Wait a moment and try again.",
        }.get(exc.code, f"The Production Portal returned an error ({exc.code}).")) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Production Portal unreachable: %s", exc)
        raise PortalError("Could not reach the Production Portal. Try again, or enter the cover by hand.") from exc
    except ValueError as exc:
        raise PortalError("The Production Portal sent an unreadable reply.") from exc


def _iso_date(value):
    """The portal stores dates as text; keep only a valid YYYY-MM-DD prefix."""
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _norm(name):
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).replace(" limited", "").replace(" ltd", "").strip()


def match_insurer(portal_name, insurers=None):
    """Our Insurer for the portal's free-text insurer name, or None. Exact (normalised) first, then containment."""
    want = _norm(portal_name)
    if not want:
        return None
    insurers = list(insurers if insurers is not None else Insurer.objects.filter(active=True))
    for insurer in insurers:
        if _norm(insurer.name) == want:
            return insurer
    close = [i for i in insurers if _norm(i.name) and (_norm(i.name) in want or want in _norm(i.name))]
    return close[0] if len(close) == 1 else None


def is_active(item, today=None):
    """Only a cover that is still in force can be cancelled: not cancelled (in the portal or here) and not expired."""
    today = (today or timezone.localdate()).isoformat()
    if item["cancelled_in_portal"] or item["portal_status"] == "Cancelled":
        return False
    if item["existing_cover_id"] and not item["existing_cover_active"]:
        return False
    return not (item["end_date"] and item["end_date"] < today)


def search(by, term, staff_email):
    """Search the portal. Returns (active covers ready for the New Cancellation form, number hidden as not active)."""
    if not enabled():
        raise PortalError("The Production Portal is not connected.")
    if by not in SEARCH_BY:
        raise PortalError("Choose what to search by.")
    term = (term or "").strip()
    if not 3 <= len(term) <= 100:
        raise PortalError("Enter 3 to 100 characters.")
    data = _get("/api/underwriting/v1/records", {"by": by, "q": term}, staff_email)
    insurers = list(Insurer.objects.filter(active=True))
    results = []
    for item in (data.get("items") or [])[:25]:
        if not isinstance(item, dict):
            continue
        insurer = match_insurer(item.get("insurer"), insurers)
        policy = (item.get("policyNumber") or "").strip()
        cert = (item.get("certificateNumber") or "").strip()
        existing = None
        if insurer and (cert or policy):
            match = Q(certificate_number__iexact=cert) if cert else Q(pk__in=[])
            if policy:
                match |= Q(policy_number__iexact=policy)
            existing = Cover.objects.filter(match, insurer=insurer).first()
        results.append({
            "ref": str(item.get("ref") or "")[:40],
            "client_name": (item.get("insuredName") or "")[:200],
            "policy_number": policy[:100],
            "certificate_number": (item.get("certificateNumber") or "")[:100],
            "vehicle_reg": (item.get("vehicleRegistration") or "")[:200],
            "cover_type": item.get("coverType") or "",
            "insurer_name": item.get("insurer") or "",
            "insurer_id": insurer.pk if insurer else None,
            "start_date": _iso_date(item.get("startDate")),
            "end_date": _iso_date(item.get("expiryDate")),
            "portal_status": item.get("status") or "",
            "cancelled_in_portal": bool(item.get("cancelled")),
            "existing_cover_id": existing.pk if existing else None,
            "existing_cover_active": bool(existing and existing.status == Cover.Status.ACTIVE),
        })
    active = [r for r in results if is_active(r)]
    return active, len(results) - len(active)
