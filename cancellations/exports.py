"""Confirmed cancellations for other systems (e.g. a commission system): CSV download and JSON API."""

import csv
import datetime as dt
import hmac
import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.http import require_GET

from .models import CancellationCase
from .permissions import cancellations_role_required, has_cancellations_role

logger = logging.getLogger(__name__)

FIELDS = [
    "reference", "case_id", "status", "certificate_number", "policy_number", "client_name", "insurer", "asset_reference",
    "premium", "cover_start_date", "cover_end_date", "requested_effective_date", "requested_effective_time", "effective_date",
    "refund_amount", "currency", "reason_code", "confirmed_at",
]
MAX_LIMIT = 500


class BadRequest(ValueError):
    pass


def _parse_bound(value, name, end_of_day=False):
    """Accept YYYY-MM-DD or an ISO-8601 datetime; naive values use the app time zone."""
    if not value:
        return None
    try:
        d = parse_date(value)  # a bare date: whole-day bound (checked first; parse_datetime accepts it too)
        parsed = None if d else parse_datetime(value)
    except ValueError:
        d = parsed = None
    if d:
        parsed = dt.datetime.combine(d + dt.timedelta(days=1) if end_of_day else d, dt.time.min)
    elif parsed is None:
        raise BadRequest(f"{name} must be YYYY-MM-DD or an ISO-8601 datetime")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def confirmed_cases(params):
    """Cases whose cancellation the insurer confirmed (still CONFIRMED or since CLOSED)."""
    qs = (CancellationCase.objects.filter(confirmed_at__isnull=False)
          .select_related("cover__insurer").order_by("confirmed_at", "pk"))
    since = _parse_bound(params.get("confirmed_since"), "confirmed_since")
    until = _parse_bound(params.get("confirmed_until"), "confirmed_until", end_of_day=True)
    if since:
        qs = qs.filter(confirmed_at__gte=since)
    if until:
        qs = qs.filter(confirmed_at__lt=until)
    if params.get("insurer"):
        qs = qs.filter(cover__insurer__name__iexact=params["insurer"])
    return qs


def row(case):
    cover = case.cover
    return {
        "reference": case.reference,
        "case_id": case.pk,
        "status": case.status,
        "certificate_number": cover.certificate_number,
        "policy_number": cover.policy_number,
        "client_name": cover.client_name,
        "insurer": cover.insurer.name,
        "asset_reference": cover.asset_reference,
        "premium": str(cover.premium) if cover.premium is not None else None,
        "cover_start_date": cover.start_date.isoformat() if cover.start_date else None,
        "cover_end_date": cover.end_date.isoformat() if cover.end_date else None,
        "requested_effective_date": case.effective_date.isoformat() if case.effective_date else None,
        "requested_effective_time": case.effective_time.strftime("%H:%M") if case.effective_time else None,
        "effective_date": case.confirmed_effective_date.isoformat(),
        "refund_amount": str(case.refund_amount) if case.refund_amount is not None else None,
        "currency": settings.CURRENCY,
        "reason_code": case.reason_code,
        "confirmed_at": timezone.localtime(case.confirmed_at).isoformat(),
    }


@cancellations_role_required
@require_GET
def confirmed_csv(request):
    try:
        cases = confirmed_cases(request.GET)
    except BadRequest as exc:
        return HttpResponse(str(exc), status=400, content_type="text/plain")
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="confirmed-cancellations-{stamp}.csv"'
    response.write("﻿")  # BOM so Excel opens UTF-8 correctly
    writer = csv.DictWriter(response, fieldnames=FIELDS)
    writer.writeheader()
    for case in cases.iterator():
        writer.writerow({k: _csv_safe(v) for k, v in row(case).items()})
    return response


def _csv_safe(value):
    """Neutralise spreadsheet formula injection in free-text fields."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r") and not _is_number(value):
        return "'" + value
    return value


def _is_number(value):
    try:
        float(value)
        return True
    except ValueError:
        return False


def _token_ok(request):
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return False
    supplied = header[7:].strip().encode()
    return any(hmac.compare_digest(supplied, t.encode()) for t in settings.EXPORT_API_TOKENS if t)


@require_GET
def confirmed_api(request):
    """GET /api/confirmed-cancellations/?confirmed_since=2026-11-01&limit=100&offset=0

    Auth: `Authorization: Bearer <token>` (EXPORT_API_TOKENS), or a logged-in cancellations user.
    """
    if not (_token_ok(request) or has_cancellations_role(request.user)):
        response = JsonResponse({"error": "authentication required"}, status=401)
        response["WWW-Authenticate"] = 'Bearer realm="imoth-cancellations"'
        return response
    try:
        cases = confirmed_cases(request.GET)
        limit = min(int(request.GET.get("limit", 100)), MAX_LIMIT)
        offset = int(request.GET.get("offset", 0))
        if limit < 1 or offset < 0:
            raise ValueError
    except BadRequest as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except ValueError:
        return JsonResponse({"error": "limit must be 1-500 and offset >= 0"}, status=400)
    total = cases.count()
    page = [row(c) for c in cases[offset:offset + limit]]
    logger.info("Confirmed-cancellations API: %s rows (offset %s) to %s", len(page), offset,
                "token" if _token_ok(request) else request.user)
    return JsonResponse({
        "count": total,
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if offset + limit < total else None,
        "results": page,
    })
