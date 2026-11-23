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

from ..models import CancellationCase, Cover, Insurer

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


# ---------------------------------------------------------------------------
# After confirmation: does the Production Portal also show the cover cancelled?
# ---------------------------------------------------------------------------
PS = CancellationCase.PortalStatus


def _lookup(cover, staff_email):
    """(portal status, matching portal refs) for the cover, looked up by certificate then policy number."""
    for by, number, key in (("certificate", cover.certificate_number, "certificateNumber"),
                            ("policy", cover.policy_number, "policyNumber")):
        number = (number or "").strip()
        if len(number) < 3:
            continue
        data = _get("/api/underwriting/v1/records", {"by": by, "q": number}, staff_email)
        items = [i for i in (data.get("items") or []) if isinstance(i, dict)
                 and (i.get(key) or "").strip().upper() == number.upper()]
        if items:
            cancelled = [i for i in items if i.get("cancelled") or i.get("status") == "Cancelled"]
            refs = [str(i.get("ref") or "") for i in (cancelled or items)]
            return (PS.CANCELLED if cancelled else PS.ACTIVE), refs
        return PS.NOT_FOUND, []  # searched by the best number we have; not there
    return PS.NO_NUMBER, []


def check_case(case, staff_email=None, actor=None):
    """Look the case's cover up in the portal and record the result. Logs an event when it changes.

    Raises PortalError when the portal cannot be asked (nothing is recorded then).
    """
    from .workflow import log_event

    if not enabled():
        raise PortalError("The Production Portal is not connected.")
    status, refs = _lookup(case.cover, staff_email or settings.MAILBOX_USER)
    now = timezone.now()
    previous = CancellationCase.objects.filter(pk=case.pk).values_list("portal_status", flat=True).first()
    CancellationCase.objects.filter(pk=case.pk).update(portal_status=status, portal_checked_at=now)
    case.portal_status, case.portal_checked_at = status, now
    if status != previous:
        where = f" (record {', '.join(refs)})" if refs else ""
        note = {
            PS.CANCELLED: f"The Production Portal shows {case.cover.number_label.lower()} cancelled{where}.",
            PS.ACTIVE: f"{case.cover.number_label} is still active in the Production Portal{where}.",
            PS.NOT_FOUND: f"{case.cover.number_label} is not in the Production Portal (not every cover is).",
            PS.NO_NUMBER: "No certificate or policy number to look up in the Production Portal.",
        }[status]
        log_event(case, "PORTAL_CHECK", actor, case.status, case.status, note)
    return status


def cases_due_for_check(now=None):
    """Confirmed (or closed after confirmation) cases whose portal result may still change."""
    from datetime import timedelta

    from django.db.models import Q

    now = now or timezone.now()
    active_before = now - timedelta(hours=settings.PORTAL_RECHECK_ACTIVE_HOURS)
    missing_before = now - timedelta(hours=settings.PORTAL_RECHECK_NOT_FOUND_HOURS)
    return (CancellationCase.objects.select_related("cover")
            .filter(confirmed_at__gte=now - timedelta(days=settings.PORTAL_CHECK_DAYS))
            .filter(Q(portal_status=PS.NOT_CHECKED)
                    | Q(portal_status=PS.ACTIVE, portal_checked_at__lt=active_before)
                    | Q(portal_status=PS.NOT_FOUND, portal_checked_at__lt=missing_before))
            .order_by("portal_checked_at", "confirmed_at"))


def check_due(limit=50, now=None):
    """Worker pass: check up to `limit` due cases. Returns {status: count}; stops at the first portal error."""
    counts = {}
    if not enabled():
        return counts
    for case in cases_due_for_check(now)[:limit]:
        try:
            status = check_case(case)
        except PortalError as exc:
            logger.warning("Production Portal check stopped: %s", exc)
            counts["error"] = str(exc)
            break
        counts[status] = counts.get(status, 0) + 1
    return counts
