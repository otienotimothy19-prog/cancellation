"""Inbound reply processing: match to a case, verify the sender, store email + attachments.

Matching order (first hit wins, nothing is ever guessed):
  1. plus address      cancellations+CXL000123@<MAIL_DOMAIN> in any recipient
  2. subject reference IMOTH-CXL-000123 in the subject
  3. thread headers    In-Reply-To / References contain a Message-ID we sent
  4. policy number     exactly one open case's policy number appears in the body
Anything else goes to the Unmatched inbox for a human to link.
"""

import hashlib
import html
import logging
import re
from dataclasses import dataclass, field

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from ..models import CancellationCase, CaseEmail, split_addresses
from ..reasons import DocType
from .documents import save_bytes_document
from .workflow import S, log_event

logger = logging.getLogger(__name__)

M = CaseEmail.MatchedBy
OPEN_STATUSES = (S.SENT, S.QUERY)  # cases still waiting for an insurer answer
MSGID_RE = re.compile(r"<([^<>\s]+)>")


@dataclass
class Attachment:
    filename: str
    content_type: str
    content: bytes


@dataclass
class InboundEmail:
    """Provider-neutral inbound email."""

    from_email: str
    to: list = field(default_factory=list)
    cc: list = field(default_factory=list)
    envelope_recipient: str = ""
    subject: str = ""
    text: str = ""
    html: str = ""
    message_id: str = ""
    provider_message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    headers: dict = field(default_factory=dict)
    attachments: list = field(default_factory=list)

    def recipients(self):
        return [a.lower() for a in [*self.to, *self.cc, self.envelope_recipient] if a]

    def body_text(self):
        if self.text.strip():
            return self.text
        return html.unescape(re.sub(r"<[^>]+>", " ", self.html or ""))

    def stable_message_id(self):
        """Idempotency key: the Message-ID header, else the provider id, else a content hash."""
        if self.message_id.strip():
            return self.message_id.strip()
        if self.provider_message_id:
            return f"<provider-{self.provider_message_id}>"
        digest = hashlib.sha256(
            "\n".join([self.from_email, self.subject, self.text, self.html, ",".join(self.to)]).encode()
        ).hexdigest()
        return f"<sha256-{digest}@inbound.invalid>"


@dataclass
class MatchResult:
    case: CancellationCase | None
    method: str
    note: str


def _sendable_case(pk):
    """Return the case for pk if it exists and has been sent (drafts never receive replies)."""
    if pk is None:
        return None
    return CancellationCase.objects.exclude(status=S.DRAFT).select_related("cover__insurer").filter(pk=pk).first()


def match_by_plus_address(msg):
    pattern = re.compile(
        rf"^{re.escape(settings.CANCELLATIONS_MAILBOX)}\+cxl(\d{{6,}})@{re.escape(settings.MAIL_DOMAIN)}$",
        re.IGNORECASE,
    )
    for address in msg.recipients():
        m = pattern.match(address.strip())
        if m:
            case = _sendable_case(int(m.group(1)))
            if case:
                return case, f"Plus address {address}"
    return None, ""


def match_by_subject(msg):
    pk = CancellationCase.pk_from_reference(msg.subject)
    case = _sendable_case(pk)
    return (case, f"Reference {case.reference} in subject") if case else (None, "")


def match_by_thread_headers(msg):
    ids = MSGID_RE.findall(f"{msg.in_reply_to} {msg.references}")
    if not ids:
        return None, ""
    full_ids = [f"<{i}>" for i in ids]
    local_parts = [i.split("@", 1)[0] for i in ids]
    sent = (
        CaseEmail.objects.filter(direction=CaseEmail.Direction.OUTBOUND, case__isnull=False)
        .filter(models_q_message_ids(full_ids, local_parts))
        .select_related("case")
        .order_by("-received_at")
    )
    cases = {e.case_id: e.case for e in sent}
    if len(cases) == 1:
        case = next(iter(cases.values()))
        return case, "In-Reply-To/References header matches a sent request"
    if len(cases) > 1:
        return None, "Thread headers point at more than one case"
    return None, ""


def models_q_message_ids(full_ids, local_parts):
    from django.db.models import Q

    return Q(message_id__in=full_ids) | Q(provider_message_id__in=local_parts + [i.strip("<>") for i in full_ids])


def match_by_policy_number(msg):
    body = msg.body_text()
    if not body.strip():
        return None, ""
    candidates = CancellationCase.objects.filter(status__in=OPEN_STATUSES).select_related("cover__insurer")
    hits = {}
    for case in candidates:
        pn = case.cover.policy_number.strip()
        if pn and re.search(rf"(?<![A-Za-z0-9]){re.escape(pn)}(?![A-Za-z0-9])", body, re.IGNORECASE):
            hits[case.pk] = case
    if len(hits) == 1:
        case = next(iter(hits.values()))
        return case, f"Policy number {case.cover.policy_number} in body (only open case with that number)"
    if len(hits) > 1:
        refs = ", ".join(c.reference for c in hits.values())
        return None, f"Body mentions policy numbers of several open cases ({refs}); not guessing"
    return None, ""


MATCHERS = [
    (M.PLUS_ADDRESS, match_by_plus_address),
    (M.SUBJECT_REFERENCE, match_by_subject),
    (M.THREAD_HEADERS, match_by_thread_headers),
    (M.POLICY_NUMBER, match_by_policy_number),
]


def match_case(msg):
    notes = []
    for method, matcher in MATCHERS:
        case, note = matcher(msg)
        if case:
            return MatchResult(case, method, note)
        if note:
            notes.append(note)
    return MatchResult(None, M.NONE, "; ".join(notes) or "No reference, thread or policy number found")


def authentication_failed(msg):
    """True if the receiving provider recorded a DMARC failure for this message."""
    results = " ".join(v for k, v in msg.headers.items() if k.lower() == "authentication-results")
    return bool(re.search(r"\bdmarc=fail\b", results, re.IGNORECASE))


def sender_address(msg):
    addresses = split_addresses(msg.from_email)
    # from_email may be "Name <a@b>"; take the bracketed part when present.
    m = re.search(r"<([^>]+)>", msg.from_email or "")
    return (m.group(1) if m else (addresses[-1] if addresses else "")).lower()


def ingest(msg: InboundEmail):
    """Store an inbound email exactly once. Returns (CaseEmail, created)."""
    message_id = msg.stable_message_id()[:500]
    existing = CaseEmail.objects.filter(message_id=message_id).first()
    if existing:
        return existing, False

    result = match_case(msg)
    sender = sender_address(msg)
    case, suggested, verified = result.case, None, False
    note = result.note
    if case:
        verified = case.cover.insurer.is_known_sender(sender) and not authentication_failed(msg)
        if not verified:
            why = "DMARC failed" if authentication_failed(msg) else f"{sender} is not a known address of {case.cover.insurer}"
            note = f"{note}. NOT attached: {why}. Link manually after checking the sender."
            suggested, case = case, None

    try:
        with transaction.atomic():
            email = CaseEmail.objects.create(
                case=case,
                direction=CaseEmail.Direction.INBOUND,
                message_id=message_id,
                provider_message_id=(msg.provider_message_id or "")[:255],
                in_reply_to=(msg.in_reply_to or "")[:500],
                references=msg.references or "",
                from_email=(msg.from_email or "")[:320],
                to_emails=", ".join(msg.to),
                cc_emails=", ".join(msg.cc),
                subject=(msg.subject or "")[:1000],
                body=msg.text or "",
                body_html=msg.html or "",
                headers=msg.headers,
                received_at=timezone.now(),
                classification=CaseEmail.Classification.UNREVIEWED,
                matched_by=result.method if case else M.NONE,
                sender_verified=verified,
                match_note=note,
                suggested_case=suggested,
            )
            skipped = []
            for att in msg.attachments:
                try:
                    save_bytes_document(
                        case=case, email=email, name=att.filename or "attachment.bin", data=att.content,
                        doc_type=DocType.INSURER_REPLY, source="insurer",
                    )
                except ValidationError as exc:
                    skipped.append({"filename": att.filename, "reason": "; ".join(exc.messages)})
            if skipped:
                email.skipped_attachments = skipped
                email.save(update_fields=["skipped_attachments"])
            if case:
                log_event(case, "EMAIL_RECEIVED", None, case.status, case.status,
                          f"Matched by {email.get_matched_by_display()}: {note}", email)
    except IntegrityError:
        # A concurrent delivery of the same message won the race.
        return CaseEmail.objects.get(message_id=message_id), False
    return email, True


@transaction.atomic
def link_email(email, case, actor, sender_checked=False):
    """Manually attach an unmatched inbound email (and its attachments) to a case."""
    email = CaseEmail.objects.select_for_update().get(pk=email.pk)
    if not email.is_unmatched:
        raise ValueError("This email is already linked to a case.")
    if case.status == S.DRAFT:
        raise ValueError("Replies cannot be linked to a draft that has not been sent.")
    known = case.cover.insurer.is_known_sender(sender_address(_as_msg(email)))
    if not known and not sender_checked:
        raise ValueError("The sender is not a known address of this insurer. Confirm you have verified the sender.")
    email.case = case
    email.matched_by = M.MANUAL
    email.sender_verified = True
    email.suggested_case = None
    email.save(update_fields=["case", "matched_by", "sender_verified", "suggested_case"])
    email.documents.update(case=case)
    note = "Linked manually" + ("" if known else f"; sender {email.from_email} verified by {actor}")
    log_event(case, "EMAIL_LINKED", actor, case.status, case.status, note, email)
    return email


def dismiss_email(email, actor):
    email.classification = CaseEmail.Classification.DISMISSED
    email.reviewed_by = actor
    email.reviewed_at = timezone.now()
    email.save(update_fields=["classification", "reviewed_by", "reviewed_at"])


def _as_msg(email):
    return InboundEmail(from_email=email.from_email)


def from_anymail(message, event_id=None):
    """Convert an AnymailInboundMessage (Postmark or Mailgun) into an InboundEmail."""
    headers = {}
    for key, value in message.items():
        headers[key] = f"{headers[key]}\n{value}" if key in headers else str(value)
    attachments = [
        Attachment(a.get_filename() or "attachment.bin", a.get_content_type(), a.get_content_bytes() or b"")
        for a in message.attachments
    ]
    return InboundEmail(
        from_email=str(message.from_email) if message.from_email else (message.get("From") or ""),
        to=[a.addr_spec for a in message.to],
        cc=[a.addr_spec for a in message.cc],
        envelope_recipient=message.envelope_recipient or "",
        subject=message.subject or "",
        text=message.text or "",
        html=message.html or "",
        message_id=(message.get("Message-ID") or "").strip(),
        provider_message_id=event_id or "",
        in_reply_to=(message.get("In-Reply-To") or "").strip(),
        references=(message.get("References") or "").strip(),
        headers=headers,
        attachments=attachments,
    )
