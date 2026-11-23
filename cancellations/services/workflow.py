"""Case state machine. Every status change goes through `transition()` and is logged."""

from django.db import transaction
from django.utils import timezone

from ..models import CancellationCase, CaseEmail, CaseEvent
from ..reasons import DocType

S = CancellationCase.Status

# DRAFT -> SENT -> CONFIRMED | REJECTED | QUERY -> CLOSED
# A QUERY can still be resolved by a later insurer reply (CONFIRMED / REJECTED).
ALLOWED_TRANSITIONS = {
    S.DRAFT: {S.SENT},
    S.SENT: {S.CONFIRMED, S.REJECTED, S.QUERY},
    S.QUERY: {S.SENT, S.CONFIRMED, S.REJECTED, S.CLOSED},  # SENT = follow-up answering the query
    S.CONFIRMED: {S.CLOSED},
    S.REJECTED: {S.CLOSED},
    S.CLOSED: set(),
}

# Review decision on an inbound email -> resulting case status
REVIEW_OUTCOMES = {
    CaseEmail.Classification.CONFIRMATION: S.CONFIRMED,
    CaseEmail.Classification.REJECTION: S.REJECTED,
    CaseEmail.Classification.QUERY: S.QUERY,
}


class TransitionError(Exception):
    pass


def log_event(case, action, actor=None, from_status="", to_status="", note="", email=None):
    return CaseEvent.objects.create(
        case=case, actor=actor, action=action, from_status=from_status or "", to_status=to_status or "",
        note=note, email=email,
    )


def can_transition(from_status, to_status):
    return to_status in ALLOWED_TRANSITIONS.get(from_status, set())


@transaction.atomic
def transition(case, to_status, actor, action=None, note="", email=None):
    """Move a case to `to_status`, enforcing the state machine and writing a case_event."""
    locked = CancellationCase.objects.select_for_update().get(pk=case.pk)
    from_status = locked.status
    if not can_transition(from_status, to_status):
        raise TransitionError(f"Cannot move {locked.reference} from {from_status} to {to_status}.")
    locked.status = to_status
    update_fields = ["status", "updated_at"]
    if to_status == S.SENT:
        now = timezone.now()
        locked.sent_at = locked.sent_at or now  # keeps the first request's time
        locked.last_sent_at = now  # restarts the SLA clock
        locked.sla_reminder_sent_at = None
        locked.is_stale, locked.stale_at = False, None
        update_fields += ["sent_at", "last_sent_at", "sla_reminder_sent_at", "is_stale", "stale_at"]
    locked.save(update_fields=update_fields)
    log_event(locked, action or to_status, actor, from_status, to_status, note, email)
    for name in ("status", "sent_at", "last_sent_at", "sla_reminder_sent_at", "is_stale", "stale_at"):
        setattr(case, name, getattr(locked, name))  # keep the caller's object in sync
    return locked


@transaction.atomic
def review_inbound_email(email, classification, actor, note="", confirmed_effective_date=None,
                         certificate_doc=None, certificate_upload=None):
    """Staff decision on an inbound reply. Only a human ever calls this — nothing is auto-confirmed.

    A CONFIRMATION needs the insurer's confirmed effective date; it records the pro-rata refund,
    marks the cover CANCELLED and, if given, stores the certificate / credit note (either an
    attachment of this email or a new upload).
    """
    if classification == CaseEmail.Classification.CONFIRMATION:
        if not confirmed_effective_date:
            raise TransitionError("Enter the effective date the insurer confirmed.")
        cover = email.case.cover if email.case_id else None
        if cover and not (cover.start_date <= confirmed_effective_date <= cover.end_date):
            raise TransitionError(
                f"Confirmed effective date must fall within the cover period "
                f"({cover.start_date:%d %b %Y} – {cover.end_date:%d %b %Y})."
            )
        if certificate_doc is not None and certificate_doc.email_id != email.pk:
            raise TransitionError("The certificate must be an attachment of this email.")
    if email.direction != CaseEmail.Direction.INBOUND or email.case_id is None:
        raise TransitionError("Only inbound emails linked to a case can be reviewed.")
    if not email.sender_verified:
        raise TransitionError("The sender of this email has not been verified; it cannot be used for a decision.")
    if classification not in (*REVIEW_OUTCOMES, CaseEmail.Classification.INFO):
        raise TransitionError(f"Unknown classification {classification}.")

    case = email.case
    target = REVIEW_OUTCOMES.get(classification)
    if target and not can_transition(case.status, target):
        raise TransitionError(
            f"{case.reference} is {case.get_status_display()}; it cannot be marked {target.label}."
        )

    email.classification = classification
    email.reviewed_by = actor
    email.reviewed_at = timezone.now()
    email.save(update_fields=["classification", "reviewed_by", "reviewed_at"])

    if target == S.CONFIRMED:
        return _confirm(case, email, actor, note, confirmed_effective_date, certificate_doc, certificate_upload)
    if target:
        transition(case, target, actor, action=f"MARKED_{target}", note=note, email=email)
    else:
        log_event(case, "EMAIL_REVIEWED", actor, case.status, case.status, note or "Marked as information only", email)
    return case


def _confirm(case, email, actor, note, effective_date, certificate_doc, certificate_upload):
    from .documents import save_uploaded_document
    from .refunds import refund_breakdown

    cover = case.cover
    refund = refund_breakdown(cover, effective_date)
    locked = transition(
        case, S.CONFIRMED, actor, action="MARKED_CONFIRMED", email=email,
        note=(f"Insurer confirmed effective {effective_date:%d %b %Y}"
              + (f" (requested {case.effective_date:%d %b %Y})" if case.effective_date != effective_date else "")
              + f". Pro-rata refund {refund['amount']} = {cover.premium} × {refund['unearned_days']}"
              + f"/{refund['total_days']} days." + (f" {note}" if note else "")),
    )
    now = timezone.now()
    CancellationCase.objects.filter(pk=case.pk).update(
        confirmed_effective_date=effective_date, confirmed_at=now, refund_amount=refund["amount"], updated_at=now,
    )
    case.confirmed_effective_date, case.confirmed_at, case.refund_amount = effective_date, now, refund["amount"]

    if cover.status != cover.Status.CANCELLED:
        cover.status = cover.Status.CANCELLED
        cover.save(update_fields=["status"])
        log_event(case, "COVER_CANCELLED", actor, locked.status, locked.status,
                  f"Cover {cover.policy_number} marked CANCELLED effective {effective_date:%d %b %Y}")

    if certificate_doc is not None:
        certificate_doc.type = DocType.CERTIFICATE
        certificate_doc.save(update_fields=["type"])
        log_event(case, "CERTIFICATE_RECORDED", actor, locked.status, locked.status,
                  f"Insurer attachment {certificate_doc.original_name}", email)
    if certificate_upload is not None:
        doc = save_uploaded_document(case, certificate_upload, DocType.CERTIFICATE, "insurer", actor)
        log_event(case, "CERTIFICATE_RECORDED", actor, locked.status, locked.status, f"Uploaded {doc.original_name}")
    close_if_complete(case, actor)
    return case


def close_if_complete(case, actor):
    """A confirmed case with its certificate / credit note stored needs nothing more: close it.

    Returns True if the case was closed. Rejected and query cases are never closed automatically.
    """
    current = CancellationCase.objects.get(pk=case.pk)
    if current.status != S.CONFIRMED or not current.has_certificate:
        return False
    transition(case, S.CLOSED, actor, action="CLOSED",
               note="Closed automatically: insurer confirmed and certificate / credit note stored.")
    return True


def note_reply_received(case, email):
    """An insurer reply satisfies the SLA: clear the STALE flag if it was set."""
    if case.is_stale:
        CancellationCase.objects.filter(pk=case.pk).update(is_stale=False)
        case.is_stale = False
        log_event(case, "STALE_CLEARED", None, case.status, case.status, "Insurer replied", email)


@transaction.atomic
def close_case(case, actor, note=""):
    current = CancellationCase.objects.select_for_update().get(pk=case.pk)  # never trust a stale object
    if current.status == S.CONFIRMED and not current.has_certificate:
        raise TransitionError("Store the cancellation certificate or credit note before closing a confirmed case.")
    return transition(case, S.CLOSED, actor, action="CLOSED", note=note)
