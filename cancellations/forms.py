from django import forms
from django.core.exceptions import ValidationError

from .models import CancellationCase, Cover, Insurer
from .reasons import REASON_CHOICES, DocType
from .services.documents import validate_uploaded_file

DATE = forms.DateInput(attrs={"type": "date"})


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

    NEW_FIELDS = ["client_name", "policy_number", "insurer", "premium", "start_date", "end_date"]

    def clean(self):
        data = super().clean()
        if data.get("existing_cover"):
            return data
        for name in self.NEW_FIELDS:
            if data.get(name) in (None, ""):
                self.add_error(name, "Required for a new cover.")
        if self.errors:
            return data
        if data["end_date"] <= data["start_date"]:
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
        )


class CaseForm(forms.ModelForm):
    reason_code = forms.ChoiceField(choices=[("", "— choose a reason —"), *REASON_CHOICES], required=False)

    class Meta:
        model = CancellationCase
        fields = [
            "reason_code", "reason_note", "effective_date",
            "outstanding_amount", "notice_date", "new_policy_number", "original_cover_reference",
        ]
        widgets = {
            "effective_date": DATE,
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
        if value and cover and not (cover.start_date <= value <= cover.end_date):
            raise ValidationError(
                f"Effective date must fall within the cover period ({cover.start_date:%d %b %Y} – {cover.end_date:%d %b %Y})."
            )
        return value


class DocumentForm(forms.Form):
    doc_type = forms.ChoiceField(label="Document type", choices=DocType.STAFF_UPLOAD_CHOICES, required=False)
    source = forms.ChoiceField(choices=[("client", "Client"), ("staff", "Staff"), ("insurer", "Insurer")], initial="client")
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


class LinkEmailForm(forms.Form):
    case = forms.ModelChoiceField(
        queryset=CancellationCase.objects.exclude(status=CancellationCase.Status.DRAFT).select_related("cover"),
        label="Link to case",
    )
    sender_checked = forms.BooleanField(
        required=False, label="I have checked that this sender genuinely represents the insurer",
    )


class SearchForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    status = forms.ChoiceField(
        required=False, choices=[("", "All statuses"), *CancellationCase.Status.choices],
    )
    view = forms.ChoiceField(required=False, choices=[
        ("", "All cases"), ("open", "Open"), ("stale", "Stale"), ("confirmed_month", "Confirmed this month"),
    ], label="Show")
