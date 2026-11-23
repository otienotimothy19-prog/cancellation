"""SLA follow-up for requests the insurer has not answered.

A SENT case with no insurer reply (no inbound email since the last request or follow-up):
  1. once insurer.sla_hours have passed  -> email a reminder to the staff member (one digest per person)
  2. SLA_STALE_AFTER_REMINDER_HOURS later -> flag the case STALE
A reply clears the flag (see workflow.note_reply_received). Hours are wall-clock hours.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.urls import reverse
from django.utils import timezone

from ..models import CancellationCase, CaseEmail
from .workflow import S, log_event

logger = logging.getLogger(__name__)


@dataclass
class SlaResult:
    reminded: list = field(default_factory=list)  # case references
    flagged_stale: list = field(default_factory=list)
    no_recipient: list = field(default_factory=list)


def awaiting_reply():
    replies = CaseEmail.objects.filter(
        case=OuterRef("pk"), direction=CaseEmail.Direction.INBOUND, received_at__gte=OuterRef("last_sent_at"),
    )
    return (CancellationCase.objects.filter(status=S.SENT, last_sent_at__isnull=False)
            .annotate(has_reply=Exists(replies)).filter(has_reply=False)
            .select_related("cover__insurer", "created_by"))


def recipient_for(case):
    return (case.created_by.email or "").strip() or settings.SLA_ALERT_EMAIL


def check_sla(now=None):
    now = now or timezone.now()
    grace = timedelta(hours=settings.SLA_STALE_AFTER_REMINDER_HOURS)
    result = SlaResult()
    to_remind = defaultdict(list)

    for case in awaiting_reply():
        if now < case.sla_due_at:
            continue
        if case.sla_reminder_sent_at is None:
            to = recipient_for(case)
            if to:
                to_remind[to].append(case)
            else:
                result.no_recipient.append(case.reference)
        elif not case.is_stale and now >= case.sla_reminder_sent_at + grace:
            _flag_stale(case, now, result)

    for to, cases in to_remind.items():
        _send_reminder(to, cases, now, result)
    if result.no_recipient:
        logger.warning("SLA reminder has no recipient (set the user's email or SLA_ALERT_EMAIL): %s",
                       ", ".join(result.no_recipient))
    return result


@transaction.atomic
def _flag_stale(case, now, result):
    # Conditional update so two workers can never flag (and log) the same case twice.
    if CancellationCase.objects.filter(pk=case.pk, status=S.SENT, is_stale=False).update(is_stale=True, stale_at=now):
        hours = (now - case.last_sent_at).total_seconds() / 3600
        log_event(case, "FLAGGED_STALE", None, case.status, case.status,
                  f"No insurer reply {hours:.0f} h after sending (SLA {case.cover.insurer.sla_hours} h); reminder sent "
                  f"{timezone.localtime(case.sla_reminder_sent_at):%d %b %H:%M}")
        result.flagged_stale.append(case.reference)


def _send_reminder(to, cases, now, result):
    with transaction.atomic():
        claimed = [c for c in cases if CancellationCase.objects.filter(
            pk=c.pk, status=S.SENT, sla_reminder_sent_at__isnull=True).update(sla_reminder_sent_at=now)]
        if not claimed:
            return
        lines = []
        for c in claimed:
            sent = timezone.localtime(c.last_sent_at)
            url = settings.APP_BASE_URL.rstrip("/") + reverse("cancellations:case_detail", args=[c.pk]) if settings.APP_BASE_URL else ""
            lines.append(
                f"- {c.reference}  {c.cover.client_name}, {c.cover.number_label.lower()}, {c.cover.insurer}\n"
                f"  sent {sent:%d %b %Y %H:%M}, SLA {c.cover.insurer.sla_hours} h, insurer: {c.cover.insurer.cancellation_email}"
                + (f"\n  {url}" if url else "")
            )
        n = len(claimed)
        stale_in = settings.SLA_STALE_AFTER_REMINDER_HOURS
        body = (
            f"The insurer has not replied to {n} cancellation request{'s' if n != 1 else ''} within the SLA:\n\n"
            + "\n".join(lines)
            + "\n\nPlease chase the insurer, or check the Unmatched inbox in case the reply arrived but was not "
            f"matched. Unanswered cases will be flagged STALE in {stale_in} hour{'s' if stale_in != 1 else ''}.\n"
        )
        EmailMessage(
            subject=f"[IMOTH Cancellations] {n} request{'s' if n != 1 else ''} awaiting insurer reply",
            body=body, from_email=settings.DEFAULT_FROM_EMAIL, to=[to],
        ).send(fail_silently=False)  # failure rolls back the claim, so the next run retries
        for c in claimed:
            log_event(c, "SLA_REMINDER", None, c.status, c.status, f"No reply within {c.cover.insurer.sla_hours} h; reminder emailed to {to}")
            result.reminded.append(c.reference)
