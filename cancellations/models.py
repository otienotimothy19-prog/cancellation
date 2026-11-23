import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils import timezone

from .reasons import REASON_CHOICES, REASONS, DocType

REFERENCE_PREFIX = "IMOTH-CXL-"
REFERENCE_RE = re.compile(r"IMOTH-CXL-(\d{6,})", re.IGNORECASE)


def split_addresses(value):
    """Split a comma/semicolon/newline separated list of email addresses."""
    return [a.strip().lower() for a in re.split(r"[,;\s]+", value or "") if a.strip()]


DEFAULT_EMAIL_TEMPLATE = """Dear {{ insurer_name }} Cancellations Team,

Please cancel the following cover:

Reference:       {{ reference }}
Client name:     {{ client_name }}
Policy number:   {{ policy_number }}
Vehicle/asset:   {{ asset_reference }}
Certificate no.: {{ certificate_number }}
Reason:          {{ reason }}{% if reason_note %}
Notes:           {{ reason_note }}{% endif %}
Effective date:  {{ effective_date }}

Supporting documents are attached. Please reply to this email (keeping the
reference {{ reference }} in the subject) to confirm the cancellation, the
confirmed effective date and any refund of unearned premium.

Kind regards,
IMOTH Cancellations
"""


class Insurer(models.Model):
    name = models.CharField(max_length=200, unique=True)
    cancellation_email = models.EmailField()
    cc_list = models.TextField(blank=True, help_text="Extra addresses to CC on every request, comma separated.")
    known_sender_addresses = models.TextField(
        blank=True,
        help_text=(
            "Addresses or domains allowed to reply for this insurer, comma separated. "
            "Use a full address (claims@insurer.co.ke) or a domain prefixed with @ (@insurer.co.ke). "
            "The cancellation address and CC list are always allowed."
        ),
    )
    email_template = models.TextField(
        blank=True,
        help_text=(
            "Django template for the email body. Variables: reference, client_name, policy_number, "
            "asset_reference, certificate_number, reason, reason_code, reason_note, effective_date, insurer_name, "
            "premium, start_date, end_date. Leave blank to use the default template."
        ),
    )
    sla_hours = models.PositiveIntegerField(default=72)
    requires_signed_form = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def cc_addresses(self):
        return split_addresses(self.cc_list)

    def template_text(self):
        return self.email_template.strip() or DEFAULT_EMAIL_TEMPLATE

    def is_known_sender(self, address):
        """True if `address` is one of this insurer's known addresses or domains."""
        address = (address or "").strip().lower()
        if not address or "@" not in address:
            return False
        domain = "@" + address.rsplit("@", 1)[1]
        allowed = {self.cancellation_email.lower(), *self.cc_addresses, *split_addresses(self.known_sender_addresses)}
        return address in allowed or domain in allowed


class Cover(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        CANCELLED = "CANCELLED", "Cancelled"

    client_name = models.CharField(max_length=200)
    policy_number = models.CharField(max_length=100)
    insurer = models.ForeignKey(Insurer, on_delete=models.PROTECT, related_name="covers")
    asset_reference = models.CharField("vehicle / asset reference", max_length=200, blank=True)
    certificate_number = models.CharField(max_length=100, blank=True)
    portal_ref = models.CharField(
        "Production Portal reference", max_length=40, blank=True,
        help_text="The certificate record this cover was looked up from, if any.",
    )
    # Optional: only used to calculate the pro-rata refund, which needs all three.
    premium = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["client_name", "policy_number"]
        constraints = [
            models.UniqueConstraint(fields=["insurer", "policy_number"], name="unique_policy_per_insurer"),
        ]

    def __str__(self):
        return f"{self.policy_number} – {self.client_name} ({self.insurer})"

    @property
    def has_cover_period(self):
        return bool(self.start_date and self.end_date)

    @property
    def can_calculate_refund(self):
        return self.premium is not None and self.has_cover_period

    def clean(self):
        if self.start_date and self.end_date and self.end_date <= self.start_date:
            raise ValidationError({"end_date": "End date must be after the start date."})


class CancellationCase(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SENT = "SENT", "Sent"
        CONFIRMED = "CONFIRMED", "Confirmed"
        REJECTED = "REJECTED", "Rejected"
        QUERY = "QUERY", "Query"
        CLOSED = "CLOSED", "Closed"

    cover = models.ForeignKey(Cover, on_delete=models.PROTECT, related_name="cases")
    reason_code = models.CharField(max_length=30, choices=REASON_CHOICES, blank=True)
    reason_note = models.TextField(blank=True)
    effective_date = models.DateField(null=True, blank=True)

    # Reason-specific details (which are required depends on reason_code; see reasons.py)
    outstanding_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    notice_date = models.DateField(null=True, blank=True)
    new_policy_number = models.CharField(max_length=100, blank=True)
    original_cover_reference = models.CharField(max_length=100, blank=True)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, editable=False)
    is_stale = models.BooleanField(default=False, editable=False)
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, editable=False)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True, editable=False)  # first request
    last_sent_at = models.DateTimeField(null=True, blank=True, editable=False)  # latest request or follow-up; SLA clock

    # Recorded when the insurer's confirmation is accepted (Phase 2)
    confirmed_effective_date = models.DateField(null=True, blank=True, editable=False)
    confirmed_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)

    # SLA tracking
    sla_reminder_sent_at = models.DateTimeField(null=True, blank=True, editable=False)
    stale_at = models.DateTimeField(null=True, blank=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        permissions = [("manage_cancellations", "Can create, send and review cancellation cases")]

    def __str__(self):
        return self.reference

    @property
    def reference(self):
        return f"{REFERENCE_PREFIX}{self.pk:06d}" if self.pk else "(unsaved)"

    @property
    def plus_token(self):
        """Token used in the Reply-To plus address, e.g. CXL000123."""
        return f"CXL{self.pk:06d}"

    @property
    def reply_to_address(self):
        return f"{settings.CANCELLATIONS_MAILBOX}+{self.plus_token}@{settings.MAIL_DOMAIN}"

    @property
    def reason(self):
        return REASONS.get(self.reason_code)

    @property
    def reason_label(self):
        return self.reason.label if self.reason else ""

    def get_absolute_url(self):
        return reverse("cancellations:case_detail", args=[self.pk])

    def required_document_types(self):
        types = list(self.reason.required_documents) if self.reason else []
        try:
            cover = self.cover  # may be an unsaved Cover (create-form preview)
        except Cover.DoesNotExist:
            cover = None
        if cover is not None and cover.insurer.requires_signed_form:
            types.append(DocType.SIGNED_FORM)
        return types

    @property
    def has_certificate(self):
        return self.documents.filter(type=DocType.CERTIFICATE).exists()

    @property
    def sla_due_at(self):
        start = self.last_sent_at or self.sent_at
        if not start:
            return None
        from datetime import timedelta

        return start + timedelta(hours=self.cover.insurer.sla_hours)

    @classmethod
    def pk_from_reference(cls, text):
        match = REFERENCE_RE.search(text or "")
        return int(match.group(1)) if match else None


class CaseEmail(models.Model):
    class Direction(models.TextChoices):
        OUTBOUND = "OUT", "Outbound"
        INBOUND = "IN", "Inbound"

    class Classification(models.TextChoices):
        REQUEST = "REQUEST", "Cancellation request (sent)"
        FOLLOW_UP = "FOLLOW_UP", "Follow-up to insurer (sent)"
        UNREVIEWED = "UNREVIEWED", "Not reviewed yet"
        CONFIRMATION = "CONFIRMATION", "Confirmation"
        REJECTION = "REJECTION", "Rejection"
        QUERY = "QUERY", "Query"
        INFO = "INFO", "Information only"
        DISMISSED = "DISMISSED", "Dismissed (not related)"

    class MatchedBy(models.TextChoices):
        PLUS_ADDRESS = "PLUS_ADDRESS", "Plus address"
        SUBJECT_REFERENCE = "SUBJECT_REFERENCE", "Reference in subject"
        THREAD_HEADERS = "THREAD_HEADERS", "In-Reply-To / References"
        POLICY_NUMBER = "POLICY_NUMBER", "Policy number in body"
        MANUAL = "MANUAL", "Linked manually"
        NONE = "NONE", "Unmatched"

    case = models.ForeignKey(CancellationCase, on_delete=models.PROTECT, related_name="emails", null=True, blank=True)
    direction = models.CharField(max_length=3, choices=Direction.choices)
    message_id = models.CharField(max_length=500, unique=True)
    provider_message_id = models.CharField(max_length=255, blank=True, db_index=True)
    in_reply_to = models.CharField(max_length=500, blank=True)
    references = models.TextField(blank=True)
    from_email = models.CharField(max_length=320)
    to_emails = models.TextField(blank=True)
    cc_emails = models.TextField(blank=True)
    subject = models.CharField(max_length=1000, blank=True)
    body = models.TextField(blank=True)
    body_html = models.TextField(blank=True)
    headers = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    classification = models.CharField(max_length=15, choices=Classification.choices)
    matched_by = models.CharField(max_length=20, choices=MatchedBy.choices, blank=True)
    sender_verified = models.BooleanField(default=False)
    match_note = models.TextField(blank=True, help_text="Why the email was or was not matched.")
    suggested_case = models.ForeignKey(
        CancellationCase, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        help_text="Case that matched but was not attached (e.g. sender not verified). Never auto-linked.",
    )
    skipped_attachments = models.JSONField(default=list, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.get_direction_display()}: {self.subject}"

    @property
    def is_unmatched(self):
        return self.direction == self.Direction.INBOUND and self.case_id is None


def document_upload_path(instance, filename):
    # Random names avoid collisions and never expose client data in object keys.
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    folder = f"case-{instance.case_id}" if instance.case_id else "unlinked"
    return f"{folder}/{uuid.uuid4().hex}.{ext}"


class CaseDocument(models.Model):
    class Source(models.TextChoices):
        CLIENT = "client", "Client"
        STAFF = "staff", "Staff"
        INSURER = "insurer", "Insurer"

    case = models.ForeignKey(CancellationCase, on_delete=models.PROTECT, related_name="documents", null=True, blank=True)
    email = models.ForeignKey(CaseEmail, on_delete=models.PROTECT, related_name="documents", null=True, blank=True)
    type = models.CharField(max_length=30, choices=DocType.CHOICES)
    file = models.FileField(upload_to=document_upload_path, max_length=300)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True)
    size = models.PositiveIntegerField(default=0)
    source = models.CharField(max_length=10, choices=Source.choices)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["uploaded_at"]

    def __str__(self):
        return f"{self.get_type_display()}: {self.original_name}"


class AppendOnlyError(Exception):
    pass


class CaseEvent(models.Model):
    """Append-only audit log. Rows can be inserted but never updated or deleted."""

    case = models.ForeignKey(CancellationCase, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    action = models.CharField(max_length=40)
    from_status = models.CharField(max_length=10, blank=True)
    to_status = models.CharField(max_length=10, blank=True)
    note = models.TextField(blank=True)
    email = models.ForeignKey(CaseEmail, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    timestamp = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["timestamp", "pk"]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.action}"

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise AppendOnlyError("case_event rows cannot be modified")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("case_event rows cannot be deleted")


class MailboxState(models.Model):
    """IMAP polling bookmark: the highest UID processed per folder (reset if UIDVALIDITY changes)."""

    folder = models.CharField(max_length=200, unique=True)
    uidvalidity = models.BigIntegerField(null=True, blank=True)
    last_uid = models.BigIntegerField(default=0)
    failing_uid = models.BigIntegerField(null=True, blank=True)
    failures = models.PositiveIntegerField(default=0)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    def __str__(self):
        return f"{self.folder} (last UID {self.last_uid})"
