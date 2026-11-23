"""Case state machine. Every status change goes through `transition()` and is logged."""

from django.db import transaction
from django.utils import timezone

from ..models import CancellationCase, CaseEmail, CaseEvent

S = CancellationCase.Status

# DRAFT -> SENT -> CONFIRMED | REJECTED | QUERY -> CLOSED
# A QUERY can still be resolved by a later insurer reply (CONFIRMED / REJECTED).
ALLOWED_TRANSITIONS = {
    S.DRAFT: {S.SENT},
    S.SENT: {S.CONFIRMED, S.REJECTED, S.QUERY},
    S.QUERY: {S.CONFIRMED, S.REJECTED, S.CLOSED},
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
        locked.sent_at = timezone.now()
        update_fields.append("sent_at")
    locked.save(update_fields=update_fields)
    log_event(locked, action or to_status, actor, from_status, to_status, note, email)
    case.status, case.sent_at = locked.status, locked.sent_at
    return locked


@transaction.atomic
def review_inbound_email(email, classification, actor, note=""):
    """Staff decision on an inbound reply. Only a human ever calls this — nothing is auto-confirmed."""
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

    if target:
        transition(case, target, actor, action=f"MARKED_{target}", note=note, email=email)
    else:
        log_event(case, "EMAIL_REVIEWED", actor, case.status, case.status, note or "Marked as information only", email)
    return case


def close_case(case, actor, note=""):
    return transition(case, S.CLOSED, actor, action="CLOSED", note=note)
