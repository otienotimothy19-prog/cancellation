"""Controlled list of cancellation reason codes and what each one requires before sending."""

from dataclasses import dataclass, field


class DocType:
    SIGNED_REQUEST = "SIGNED_REQUEST"
    PROOF_OF_SALE = "PROOF_OF_SALE"
    EVIDENCE = "EVIDENCE"
    INSURER_NOTICE = "INSURER_NOTICE"
    SIGNED_FORM = "SIGNED_FORM"  # insurer-specific cancellation form (insurer.requires_signed_form)
    NON_PAYMENT_NOTICE = "NON_PAYMENT_NOTICE"
    INSURER_REPLY = "INSURER_REPLY"  # attachment received from the insurer
    CERTIFICATE = "CERTIFICATE"  # cancellation certificate / credit note (Phase 2)
    OTHER = "OTHER"

    CHOICES = [
        (SIGNED_REQUEST, "Signed client request"),
        (PROOF_OF_SALE, "Proof of sale"),
        (EVIDENCE, "Evidence of misrepresentation"),
        (INSURER_NOTICE, "Insurer's notice"),
        (SIGNED_FORM, "Insurer's signed cancellation form"),
        (NON_PAYMENT_NOTICE, "Non-payment notice"),
        (INSURER_REPLY, "Insurer reply attachment"),
        (CERTIFICATE, "Cancellation certificate / credit note"),
        (OTHER, "Other"),
    ]


DocType.STAFF_UPLOAD_CHOICES = [c for c in DocType.CHOICES if c[0] != DocType.INSURER_REPLY]


@dataclass(frozen=True)
class Reason:
    code: str
    label: str
    required_documents: tuple = ()
    required_fields: tuple = field(default_factory=tuple)  # CancellationCase field names


REASONS = {
    r.code: r
    for r in [
        Reason("CLIENT_REQUEST", "Client request", required_documents=(DocType.SIGNED_REQUEST,)),
        Reason("NON_PAYMENT", "Non-payment of premium", required_fields=("outstanding_amount", "notice_date")),
        Reason("ASSET_DISPOSED", "Asset sold / disposed", required_documents=(DocType.PROOF_OF_SALE,)),
        Reason("REPLACED", "Replaced by a new policy", required_fields=("new_policy_number",)),
        Reason("MISREPRESENTATION", "Misrepresentation", required_documents=(DocType.EVIDENCE,)),
        Reason("DUPLICATE", "Duplicate cover", required_fields=("original_cover_reference",)),
        Reason("INSURER_INITIATED", "Insurer initiated", required_documents=(DocType.INSURER_NOTICE,)),
    ]
}

REASON_CHOICES = [(r.code, r.label) for r in REASONS.values()]

FIELD_LABELS = {
    "outstanding_amount": "outstanding amount",
    "notice_date": "notice date",
    "new_policy_number": "new policy number",
    "original_cover_reference": "original cover reference",
}


def doc_label(doc_type):
    return dict(DocType.CHOICES).get(doc_type, doc_type)
