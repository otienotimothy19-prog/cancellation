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
from email.utils import parseaddr

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from ..models import CancellationCase, CaseEmail
from ..reasons import DocType
from .documents import save_bytes_document
from .workflow import S, log_event, note_reply_received

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

    from_email: str  # full From header, for display
    from_address: str = ""  # parsed addr-spec of From; the only value used for verification
    envelope_sender: str = ""
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


def _domain(address):
    return address.rsplit("@", 1)[-1].lower().strip(">") if address and "@" in address else ""


def _aligned(domain_a, domain_b):
    """Relaxed DMARC-style alignment: same domain or one is a subdomain of the other."""
    if not domain_a or not domain_b:
        return False
    return domain_a == domain_b or domain_a.endswith("." + domain_b) or domain_b.endswith("." + domain_a)


def _header_values(msg, name):
    """All occurrences of a header, topmost first."""
    values = []
    for key, value in msg.headers.items():
        if key.lower() == name.lower():
            values.extend(v for v in value.split("\n") if v.strip())
    return values


def _first(msg, name):
    """Topmost occurrence only: the receiving provider prepends its own result headers,
    so later copies may have been written by the sender and are not trusted."""
    values = _header_values(msg, name)
    return values[0] if values else ""


# Provider-written SPF result headers (topmost copy), keyed by EMAIL_PROVIDER.
PROVIDER_SPF_HEADERS = {"postmark": "Received-SPF", "mailgun": "X-Mailgun-Spf"}


def _trusted_auth_results(msg):
    """Authentication-Results headers written by our own receiving server (matching authserv-id)."""
    servid = (getattr(settings, "INBOUND_AUTHSERV_ID", "") or "").strip().lower()
    header = getattr(settings, "INBOUND_AUTH_HEADER", "Authentication-Results") or "Authentication-Results"
    top = _first(msg, header)
    # ARC-Authentication-Results starts with an instance tag: "i=1; mx.zohomail.com; ..."
    body = re.sub(r"^\s*i=\d+\s*;", "", top)
    # Only the topmost header, and only if our server wrote it: lower copies (even ones
    # claiming our authserv-id) may have been supplied by the sender.
    if not servid or body.split(";", 1)[0].strip().lower() != servid:
        return []
    return [body]


def sender_authentication(msg):
    """Return (passed, reason) using the receiving provider's SPF/DMARC results.

    Only results written by our provider are read: Authentication-Results whose authserv-id
    equals INBOUND_AUTHSERV_ID, and the provider's own SPF header. Any fail/softfail fails.
    Pass requires positive evidence: dmarc=pass, or SPF pass for an envelope sender aligned
    with the From domain.
    """
    from_domain = _domain(sender_address(msg))
    results = _trusted_auth_results(msg)
    joined = " ".join(results)
    if re.search(r"\bdmarc=fail\b", joined, re.IGNORECASE):
        return False, "DMARC failed"

    spf_values = [m.group(1).lower() for m in re.finditer(r"\bspf=(\w+)", joined, re.IGNORECASE)]
    spf_header = PROVIDER_SPF_HEADERS.get(getattr(settings, "EMAIL_PROVIDER", ""))
    if spf_header and _first(msg, spf_header):
        word = re.match(r"\s*(\w+)", _first(msg, spf_header))
        spf_values.append(word.group(1).lower() if word else "")
    if any(v in ("fail", "softfail") for v in spf_values):
        return False, "SPF failed"

    if re.search(r"\bdmarc=pass\b", joined, re.IGNORECASE):
        return True, "DMARC pass"
    if "pass" in spf_values and _aligned(_domain(msg.envelope_sender), from_domain):
        return True, "SPF pass, aligned envelope sender"
    if not getattr(settings, "INBOUND_REQUIRE_SENDER_AUTH", True):
        return True, "Sender authentication not required (INBOUND_REQUIRE_SENDER_AUTH=false)"
    return False, "no SPF/DMARC pass recorded for the sender's domain"


def sender_address(msg):
    """The parsed From addr-spec. Never derived from the display name."""
    if msg.from_address:
        return msg.from_address.strip().lower()
    _name, addr = parseaddr(msg.from_email or "")
    return addr.strip().lower()


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
        known = case.cover.insurer.is_known_sender(sender)
        authenticated, auth_reason = sender_authentication(msg)
        verified = known and authenticated
        if not verified:
            why = f"{sender or 'sender'} is not a known address of {case.cover.insurer}" if not known else auth_reason
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
                headers={**msg.headers, "_from_address": sender, "_envelope_sender": msg.envelope_sender},
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
                note_reply_received(case, email)
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
    # Manual linking is the human check; an unknown sender needs explicit confirmation.
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
    note_reply_received(case, email)
    return email


def dismiss_email(email, actor):
    email.classification = CaseEmail.Classification.DISMISSED
    email.reviewed_by = actor
    email.reviewed_at = timezone.now()
    email.save(update_fields=["classification", "reviewed_by", "reviewed_at"])


def _as_msg(email):
    return InboundEmail(from_email=email.from_email, from_address=email.headers.get("_from_address", ""))


def from_anymail(message, event_id=None, from_mailbox=False):
    """Convert an AnymailInboundMessage (Postmark, Mailgun or a raw IMAP message) into an InboundEmail."""
    headers = {}
    for key, value in message.items():
        headers[key] = f"{headers[key]}\n{value}" if key in headers else str(value)
    attachments = [
        Attachment(a.get_filename() or "attachment.bin", a.get_content_type(), a.get_content_bytes() or b"")
        for a in message.attachments
    ]
    envelope_sender = message.envelope_sender or ""
    envelope_recipient = message.envelope_recipient or ""
    if from_mailbox:
        # Read from a mailbox (IMAP): the receiving server writes Return-Path and Delivered-To
        # at delivery time, so the topmost copies carry the envelope addresses.
        envelope_sender = envelope_sender or parseaddr(message.get("Return-Path") or "")[1]
        envelope_recipient = envelope_recipient or parseaddr(message.get("Delivered-To") or "")[1]
    return InboundEmail(
        from_email=str(message.from_email) if message.from_email else (message.get("From") or ""),
        from_address=message.from_email.addr_spec if message.from_email else "",
        envelope_sender=envelope_sender,
        to=[a.addr_spec for a in message.to],
        cc=[a.addr_spec for a in message.cc],
        envelope_recipient=envelope_recipient,
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
