from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.template import Context, Template, TemplateSyntaxError

from .models import CancellationCase, CaseDocument, Cover, Insurer, split_addresses
from .reasons import REASON_CHOICES, DocType
from .services.documents import validate_uploaded_file

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")  # browsers need ISO dates in type=date


class CoverChoiceForm(forms.Form):
    """Pick an existing active cover or enter a new one."""

    existing_cover = forms.ModelChoiceField(
        queryset=Cover.objects.filter(status=Cover.Status.ACTIVE).select_related("insurer"),
        required=False, empty_label="— enter a new cover below —",
    )
    client_name = forms.CharField(max_length=200, required=False)
    policy_number = forms.CharField(max_length=100, required=False)
    insurer = forms.ModelChoiceField(queryset=Insurer.objects.filter(active=True), required=False)
    asset_reference = forms.CharField(label="Vehicle / asset reference", max_length=200, required=False)
    premium = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False)
    start_date = forms.DateField(required=False, widget=DATE)
    end_date = forms.DateField(required=False, widget=DATE)
    certificate_number = forms.CharField(max_length=100, required=False)
    portal_ref = forms.CharField(max_length=40, required=False, widget=forms.HiddenInput)

    NEW_FIELDS = ["client_name", "policy_number", "insurer"]

    def clean(self):
        data = super().clean()
        if data.get("existing_cover"):
            return data
        for name in self.NEW_FIELDS:
            if data.get(name) in (None, ""):
                self.add_error(name, "Required for a new cover.")
        # Optional cover period: both dates or neither. A premium needs the period (for the refund).
        if bool(data.get("start_date")) != bool(data.get("end_date")):
            missing = "end_date" if data.get("start_date") else "start_date"
            self.add_error(missing, "Enter both cover dates, or leave both blank.")
        elif data.get("premium") is not None and not data.get("start_date"):
            self.add_error("start_date", "The refund needs the cover dates as well as the premium.")
            self.add_error("end_date", "The refund needs the cover dates as well as the premium.")
        if self.errors:
            return data
        if data.get("start_date") and data.get("end_date") and data["end_date"] <= data["start_date"]:
            self.add_error("end_date", "End date must be after the start date.")
        if Cover.objects.filter(insurer=data["insurer"], policy_number__iexact=data["policy_number"].strip()).exists():
            self.add_error("policy_number", "This policy already exists for that insurer — select it above instead.")
        return data

    def get_or_create_cover(self):
        data = self.cleaned_data
        if data.get("existing_cover"):
            return data["existing_cover"]
        return Cover.objects.create(
            client_name=data["client_name"].strip(), policy_number=data["policy_number"].strip(),
            insurer=data["insurer"], asset_reference=data["asset_reference"].strip(), premium=data["premium"],
            start_date=data["start_date"], end_date=data["end_date"],
            certificate_number=(data.get("certificate_number") or "").strip(), portal_ref=data.get("portal_ref") or "",
        )


class CaseForm(forms.ModelForm):
    reason_code = forms.ChoiceField(choices=[("", "— choose a reason —"), *REASON_CHOICES], required=False)

    class Meta:
        model = CancellationCase
        fields = [
            "reason_code", "reason_note", "effective_date", "effective_time",
            "outstanding_amount", "notice_date", "new_policy_number", "original_cover_reference",
        ]
        widgets = {
            "effective_date": DATE,
            "effective_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "notice_date": DATE,
            "reason_note": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {"reason_note": "Reason note (optional)"}

    def __init__(self, *args, cover=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.cover = cover or (self.instance.cover if self.instance.cover_id else None)

    def clean_effective_date(self):
        value = self.cleaned_data.get("effective_date")
        cover = self.cover
        if value and cover and cover.has_cover_period and not (cover.start_date <= value <= cover.end_date):
            raise ValidationError(
                f"Effective date must fall within the cover period ({cover.start_date:%d %b %Y} – {cover.end_date:%d %b %Y})."
            )
        return value


class DocumentForm(forms.Form):
    doc_type = forms.ChoiceField(label="Document type", choices=DocType.STAFF_UPLOAD_CHOICES, required=False)
    # Who provided the file. Optional: New Cancellation only asks for the file and its type (defaults to client).
    source = forms.ChoiceField(choices=[("client", "Client"), ("staff", "Staff"), ("insurer", "Insurer")],
                               initial="client", required=False)
    file = forms.FileField(required=False)

    def __init__(self, *args, required=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.required = required

    def clean(self):
        data = super().clean()
        if data.get("file"):
            if not data.get("doc_type"):
                self.add_error("doc_type", "Choose the document type.")
        elif self.required:
            self.add_error("file", "Choose a file to upload.")
        return data

    def clean_source(self):
        return self.cleaned_data.get("source") or "client"

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if f:
            validate_uploaded_file(f)
        return f


class ReviewForm(forms.Form):
    classification = forms.ChoiceField(choices=[
        ("CONFIRMATION", "Confirmed — insurer has cancelled"),
        ("REJECTION", "Rejected — insurer refuses"),
        ("QUERY", "Query — insurer needs more information"),
        ("INFO", "Information only — no status change"),
    ], widget=forms.RadioSelect)
    note = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)
    confirmed_effective_date = forms.DateField(required=False, widget=DATE,
                                               label="Effective date confirmed by the insurer")
    certificate_doc = forms.IntegerField(required=False)
    certificate_file = forms.FileField(required=False, label="…or upload the certificate / credit note")

    def clean_certificate_file(self):
        f = self.cleaned_data.get("certificate_file")
        if f:
            validate_uploaded_file(f)
        return f

    def clean(self):
        data = super().clean()
        if data.get("classification") == "CONFIRMATION" and not data.get("confirmed_effective_date"):
            self.add_error("confirmed_effective_date", "Enter the effective date the insurer confirmed.")
        return data


class FollowUpForm(forms.Form):
    """Answer an insurer QUERY on the same email thread."""

    subject = forms.CharField(max_length=300)
    body = forms.CharField(widget=forms.Textarea(attrs={"rows": 12}), label="Message")
    documents = forms.ModelMultipleChoiceField(
        queryset=CaseDocument.objects.none(), required=False, widget=forms.CheckboxSelectMultiple,
        label="Attach documents already on the case",
    )
    new_doc_type = forms.ChoiceField(label="New document type", choices=DocType.STAFF_UPLOAD_CHOICES, required=False)
    new_file = forms.FileField(required=False, label="…and/or upload a new document to attach")

    def __init__(self, *args, case=None, **kwargs):
        super().__init__(*args, **kwargs)
        if case is not None:
            self.fields["documents"].queryset = case.documents.all()
            self.fields["documents"].label_from_instance = lambda d: f"{d.original_name} ({d.get_type_display()})"

    def clean_new_file(self):
        f = self.cleaned_data.get("new_file")
        if f:
            validate_uploaded_file(f)
        return f

    def clean(self):
        data = super().clean()
        if data.get("new_file") and not data.get("new_doc_type"):
            self.add_error("new_doc_type", "Choose the type of the new document.")
        return data


class LinkEmailForm(forms.Form):
    case = forms.ModelChoiceField(
        queryset=CancellationCase.objects.exclude(status=CancellationCase.Status.DRAFT).select_related("cover"),
        label="Link to case",
    )
    sender_checked = forms.BooleanField(
        required=False, label="I have checked that this sender genuinely represents the insurer",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Searchable labels: reference, policy, client and status
        self.fields["case"].queryset = self.fields["case"].queryset.order_by("-pk")
        self.fields["case"].widget.attrs.update({"size": 8, "class": "case-select"})
        self.fields["case"].label_from_instance = lambda c: (
            f"{c.reference} · {c.cover.policy_number} · {c.cover.client_name} ({c.get_status_display()})"
        )


class SearchForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    status = forms.ChoiceField(
        required=False, choices=[("", "All statuses"), *CancellationCase.Status.choices],
    )
    view = forms.ChoiceField(required=False, choices=[
        ("", "All cases"), ("open", "Open"), ("stale", "Stale"), ("confirmed_month", "Confirmed this month"),
    ], label="Show")


class InsurerForm(forms.ModelForm):
    """Insurer settings used when sending requests and checking replies."""

    TEMPLATE_VARIABLES = (
        "reference", "client_name", "policy_number", "asset_reference", "certificate_number", "premium",
        "start_date", "end_date",
        "reason", "reason_code", "reason_note", "effective_date", "effective_time", "insurer_name",
    )

    class Meta:
        model = Insurer
        fields = [
            "name", "cancellation_email", "cc_list", "known_sender_addresses", "sla_hours",
            "requires_signed_form", "active", "email_template",
        ]
        labels = {
            "cancellation_email": "Cancellation email",
            "cc_list": "CC on every request",
            "known_sender_addresses": "Other addresses allowed to reply",
            "sla_hours": "Reply expected within (hours)",
            "requires_signed_form": "Requires a signed cancellation form",
            "email_template": "Email template",
        }
        help_texts = {
            "cc_list": "Comma separated, e.g. ops@insurer.co.ke, claims@insurer.co.ke",
            "known_sender_addresses": (
                "Full addresses (claims@insurer.co.ke) or a whole domain starting with @ (@insurer.co.ke). "
                "The cancellation email and CC addresses are always allowed."
            ),
            "sla_hours": "After this a reminder is sent; with no reply after that the case is flagged stale.",
            "requires_signed_form": "Sending is blocked until a signed form is attached to the case.",
            "active": "Inactive insurers stay on existing cases but can't be chosen for new ones.",
        }
        widgets = {
            "cc_list": forms.TextInput(),
            "known_sender_addresses": forms.TextInput(),
            "email_template": forms.Textarea(attrs={"rows": 14, "class": "mono", "spellcheck": "false"}),
        }

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        clash = Insurer.objects.filter(name__iexact=name).exclude(pk=self.instance.pk)
        if clash.exists():
            raise ValidationError("An insurer with this name already exists.")
        return name

    def clean_cancellation_email(self):
        return self.cleaned_data["cancellation_email"].strip().lower()

    @staticmethod
    def _addresses(value, allow_domains):
        bad = []
        for item in split_addresses(value):
            candidate = "x" + item if allow_domains and item.startswith("@") else item
            try:
                validate_email(candidate)
            except ValidationError:
                bad.append(item)
        if bad:
            raise ValidationError("Not a valid address: %(bad)s", params={"bad": ", ".join(bad)})
        return ", ".join(split_addresses(value))

    def clean_cc_list(self):
        return self._addresses(self.cleaned_data["cc_list"], allow_domains=False)

    def clean_known_sender_addresses(self):
        return self._addresses(self.cleaned_data["known_sender_addresses"], allow_domains=True)

    def clean_email_template(self):
        text = self.cleaned_data["email_template"].replace("\r\n", "\n")
        if not text.strip():
            return ""
        try:
            Template("{% autoescape off %}" + text + "{% endautoescape %}").render(
                Context({v: v for v in self.TEMPLATE_VARIABLES}))
        except TemplateSyntaxError as exc:
            raise ValidationError("The template has an error: %(error)s", params={"error": exc}) from exc
        return text
