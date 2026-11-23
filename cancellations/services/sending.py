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
    return problems + content_blockers(case, set(case.documents.values_list("type", flat=True)))


def content_blockers(case, present_doc_types):
    """Checks on the case's content; works on an unsaved case too (used by the create-form preview)."""
    problems = []
    if not case.reason_code:
        problems.append("Reason code is missing.")
    if not case.effective_date:
        problems.append("Effective date is missing.")
    if case.reason:
        for field in case.reason.required_fields:
            if getattr(case, field) in (None, ""):
                problems.append(f"{case.reason.label} requires the {FIELD_LABELS.get(field, field)}.")
    for doc_type in case.required_document_types():
        if doc_type not in present_doc_types:
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
        "certificate_number": cover.certificate_number or "-",
        "premium": cover.premium if cover.premium is not None else "-",
        "start_date": cover.start_date.strftime("%d %B %Y") if cover.start_date else "-",
        "end_date": cover.end_date.strftime("%d %B %Y") if cover.end_date else "-",
        "reason": case.reason_label,
        "reason_code": case.reason_code,
        "reason_note": case.reason_note,
        "effective_date": case.effective_date.strftime("%d %B %Y") if case.effective_date else "",
        "effective_time": case.effective_time.strftime("%H:%M") if case.effective_time else "",
        "insurer_name": cover.insurer.name,
    }


def render_email(case, reference=None):
    """Return (subject, body) for the insurer's template. Plain text, so autoescape is off.

    `reference` overrides the case reference (the create-form preview has no number yet).
    """
    cover = case.cover
    context = template_context(case)
    if reference:
        context["reference"] = reference
    subject = f"Cancellation request {context['reference']} – Policy {cover.policy_number} – {cover.client_name}"
    template = Template("{% autoescape off %}" + cover.insurer.template_text() + "{% endautoescape %}")
    return subject, template.render(Context(context))


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


def query_email(case):
    """The insurer reply being answered: the latest inbound email classified QUERY."""
    return (case.emails.filter(direction=CaseEmail.Direction.INBOUND, classification=CaseEmail.Classification.QUERY)
            .order_by("-received_at").first())


def follow_up_defaults(case):
    """Prefilled subject/body for answering the insurer's query."""
    cover = case.cover
    query = query_email(case)
    subject = (query.subject if query and case.reference.lower() in query.subject.lower()
               else f"Cancellation request {case.reference} – Policy {cover.policy_number} – {cover.client_name}")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    body = (
        f"Dear {cover.insurer.name} Cancellations Team,\n\n"
        f"Further to your query on our cancellation request {case.reference} "
        f"(policy {cover.policy_number}, {cover.client_name}):\n\n\n\n"
        f"Please reply keeping the reference {case.reference} in the subject.\n\n"
        "Kind regards,\nIMOTH Cancellations\n"
    )
    if query and query.body.strip():
        quoted = "\n".join(f"> {line}" for line in query.body.strip().splitlines()[:40])
        when = query.received_at.strftime("%d %b %Y %H:%M")
        body += f"\n\nOn {when}, {query.from_email} wrote:\n{quoted}\n"
    return subject, body


def send_follow_up(case, actor, body, documents=(), subject=None):
    """Answer an insurer QUERY: email the insurer on the same thread and move the case back to SENT.

    `documents` are CaseDocuments of this case to attach. Same all-or-nothing behaviour as
    send_case: a provider error rolls back and the case stays in QUERY.
    """
    try:
        return _send_follow_up(case, actor, body, documents, subject)
    except SendBlocked:
        raise
    except Exception as exc:
        logger.exception("Follow-up for %s failed", case.reference)
        log_event(case, "SEND_FAILED", actor, case.status, case.status, f"Follow-up: {str(exc)[:480]}")
        raise


@transaction.atomic
def _send_follow_up(case, actor, body, documents, subject):
    case = CancellationCase.objects.select_for_update().select_related("cover__insurer").get(pk=case.pk)
    problems = []
    if case.status != S.QUERY:
        problems.append(f"Case is {case.get_status_display()}; follow-ups answer an insurer query.")
    if not (body or "").strip():
        problems.append("Write the reply to the insurer.")
    documents = list(documents)
    if any(d.case_id != case.pk for d in documents):
        problems.append("Attachments must be documents of this case.")
    if problems:
        raise SendBlocked(problems)

    insurer = case.cover.insurer
    query = query_email(case)
    default_subject, _ = follow_up_defaults(case)
    subject = (subject or "").strip() or default_subject
    if case.reference.lower() not in subject.lower():
        subject = f"{subject} [{case.reference}]"  # keep the reference so the next reply matches

    # Reply to whoever raised the query (sender verified when it was reviewed), copying the
    # insurer's cancellation address and CC list so the cancellations team keeps the thread.
    to = query.headers.get("_from_address") if query else ""
    to = (to or insurer.cancellation_email).lower()
    cc = [a for a in dict.fromkeys([insurer.cancellation_email.lower(), *insurer.cc_addresses]) if a != to]

    thread = [e.message_id for e in case.emails.order_by("received_at") if e.message_id.startswith("<")]
    message_id = make_msgid(idstring=case.plus_token, domain=settings.MAIL_DOMAIN)
    headers = {"Message-ID": message_id, "X-IMOTH-Reference": case.reference}
    if query and query.message_id.startswith("<"):
        headers["In-Reply-To"] = query.message_id
    if thread:
        headers["References"] = " ".join(thread[-10:])
    msg = EmailMessage(subject=subject, body=body, from_email=settings.DEFAULT_FROM_EMAIL, to=[to], cc=cc,
                       reply_to=[case.reply_to_address], headers=headers)
    for doc in documents:
        with doc.file.open("rb") as fh:
            msg.attach(doc.original_name, fh.read(), doc.content_type or None)

    email = CaseEmail.objects.create(
        case=case, direction=CaseEmail.Direction.OUTBOUND, message_id=message_id,
        in_reply_to=headers.get("In-Reply-To", ""), references=headers.get("References", ""),
        from_email=settings.DEFAULT_FROM_EMAIL, to_emails=to, cc_emails=", ".join(cc), subject=subject, body=body,
        classification=CaseEmail.Classification.FOLLOW_UP, sender_verified=True,
    )
    attached = ", ".join(d.original_name for d in documents) or "none"
    transition(case, S.SENT, actor, action="FOLLOW_UP_SENT", email=email,
               note=f"Answered insurer query; sent to {to}; attachments: {attached}. SLA restarted.")

    msg.send(fail_silently=False)  # last step: raising here rolls back the records above

    status = getattr(msg, "anymail_status", None)
    provider_id = getattr(status, "message_id", None) if status else None
    if provider_id:
        email.provider_message_id = str(provider_id)
        email.save(update_fields=["provider_message_id"])
    return case


__all__ = [
    "SendBlocked", "TransitionError", "follow_up_defaults", "render_email", "send_blockers", "send_case",
    "send_follow_up",
]
