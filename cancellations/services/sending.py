"""Validate and send a cancellation request to the insurer."""

import logging
from email.utils import make_msgid

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.template import Context, Template

from ..models import CancellationCase, CaseDocument, CaseEmail
from ..reasons import FIELD_LABELS, doc_label
from .workflow import S, TransitionError, log_event, transition

logger = logging.getLogger(__name__)


class SendBlocked(Exception):
    def __init__(self, problems):
        self.problems = problems
        super().__init__("; ".join(problems))


def send_blockers(case):
    """Return a list of human-readable reasons the case cannot be sent (empty = OK to send)."""
    problems = []
    if case.status != S.DRAFT:
        problems.append(f"Case is {case.get_status_display()}, only drafts can be sent.")
    if not case.reason_code:
        problems.append("Reason code is missing.")
    if not case.effective_date:
        problems.append("Effective date is missing.")
    if case.reason:
        for field in case.reason.required_fields:
            if getattr(case, field) in (None, ""):
                problems.append(f"{case.reason.label} requires the {FIELD_LABELS.get(field, field)}.")
    present = set(case.documents.values_list("type", flat=True))
    for doc_type in case.required_document_types():
        if doc_type not in present:
            problems.append(f"Required document missing: {doc_label(doc_type)}.")
    insurer = case.cover.insurer
    if not insurer.cancellation_email:
        problems.append(f"{insurer} has no cancellation email address.")
    return problems


def template_context(case):
    cover = case.cover
    return {
        "reference": case.reference,
        "client_name": cover.client_name,
        "policy_number": cover.policy_number,
        "asset_reference": cover.asset_reference or "-",
        "premium": cover.premium,
        "start_date": cover.start_date.strftime("%d %B %Y"),
        "end_date": cover.end_date.strftime("%d %B %Y"),
        "reason": case.reason_label,
        "reason_code": case.reason_code,
        "reason_note": case.reason_note,
        "effective_date": case.effective_date.strftime("%d %B %Y") if case.effective_date else "",
        "insurer_name": cover.insurer.name,
    }


def render_email(case):
    """Return (subject, body) for the insurer's template. Plain text, so autoescape is off."""
    cover = case.cover
    subject = f"Cancellation request {case.reference} – Policy {cover.policy_number} – {cover.client_name}"
    template = Template("{% autoescape off %}" + cover.insurer.template_text() + "{% endautoescape %}")
    return subject, template.render(Context(template_context(case)))


def send_case(case, actor):
    """Send the cancellation request. Raises SendBlocked if validation fails.

    Database records are written first and the email is sent last inside the same
    transaction, so a provider error rolls everything back and the case stays DRAFT.
    """
    try:
        return _send_case(case, actor)
    except SendBlocked:
        raise
    except Exception as exc:
        logger.exception("Sending %s failed", case.reference)
        log_event(case, "SEND_FAILED", actor, case.status, case.status, str(exc)[:500])
        raise


@transaction.atomic
def _send_case(case, actor):
    # Lock the row so a double-click cannot send twice.
    case = CancellationCase.objects.select_for_update().select_related("cover__insurer").get(pk=case.pk)
    problems = send_blockers(case)
    if problems:
        raise SendBlocked(problems)

    insurer = case.cover.insurer
    subject, body = render_email(case)
    message_id = make_msgid(idstring=case.plus_token, domain=settings.MAIL_DOMAIN)
    msg = EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[insurer.cancellation_email],
        cc=insurer.cc_addresses,
        reply_to=[case.reply_to_address],
        headers={"Message-ID": message_id, "X-IMOTH-Reference": case.reference},
    )
    docs = list(case.documents.filter(source__in=[CaseDocument.Source.CLIENT, CaseDocument.Source.STAFF]))
    for doc in docs:
        with doc.file.open("rb") as fh:
            msg.attach(doc.original_name, fh.read(), doc.content_type or None)

    email = CaseEmail.objects.create(
        case=case,
        direction=CaseEmail.Direction.OUTBOUND,
        message_id=message_id,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to_emails=insurer.cancellation_email,
        cc_emails=", ".join(insurer.cc_addresses),
        subject=subject,
        body=body,
        classification=CaseEmail.Classification.REQUEST,
        sender_verified=True,
    )
    attached = ", ".join(d.original_name for d in docs) or "none"
    transition(
        case, S.SENT, actor, action="SENT", email=email,
        note=f"Sent to {insurer.cancellation_email}; reply-to {case.reply_to_address}; attachments: {attached}",
    )

    msg.send(fail_silently=False)  # last step: raising here rolls back the records above

    status = getattr(msg, "anymail_status", None)
    provider_id = getattr(status, "message_id", None) if status else None
    if provider_id:
        # Providers such as Postmark may replace our Message-ID; keep theirs for thread matching.
        email.provider_message_id = str(provider_id)
        email.save(update_fields=["provider_message_id"])
    return case


__all__ = ["SendBlocked", "TransitionError", "render_email", "send_blockers", "send_case"]
