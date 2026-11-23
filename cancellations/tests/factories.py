import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile

from cancellations.models import CancellationCase, Cover, Insurer
from cancellations.reasons import DocType
from cancellations.services.documents import save_uploaded_document
from cancellations.services.inbound import InboundEmail
from cancellations.services.workflow import S

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
# Headers the receiving provider stamps for an authenticated sender.
AUTH_PASS = {"Authentication-Results": "mx.provider.test; spf=pass; dkim=pass; dmarc=pass header.from=insurer.test"}


def make_user(username="staff", role=True):
    user = get_user_model().objects.create_user(username, password="pw-123456789")
    if role:
        user.groups.add(Group.objects.get_or_create(name="cancellations")[0])
    return user


def make_insurer(name="Acme Insurance", **kw):
    defaults = dict(
        cancellation_email="cancel@insurer.test",
        cc_list="ops@insurer.test",
        known_sender_addresses="@insurer.test",
        sla_hours=48,
    )
    defaults.update(kw)
    insurer, _ = Insurer.objects.get_or_create(name=name, defaults=defaults)
    return insurer


def make_cover(insurer=None, policy_number=None, **kw):
    defaults = dict(
        client_name="Jane Wanjiru", asset_reference="KDA 123A", premium=Decimal("36500.00"),
        start_date=dt.date(2026, 1, 1), end_date=dt.date(2026, 12, 31),
    )
    defaults.update(kw)
    insurer = insurer or make_insurer()
    if policy_number is None:  # POL-1001, then POL-1002, ... per test
        n = 1001
        while Cover.objects.filter(insurer=insurer, policy_number=f"POL-{n}").exists():
            n += 1
        policy_number = f"POL-{n}"
    return Cover.objects.create(insurer=insurer, policy_number=policy_number, **defaults)


def make_case(user, cover=None, reason_code="CLIENT_REQUEST", effective_date=dt.date(2026, 6, 30), **kw):
    return CancellationCase.objects.create(
        cover=cover or make_cover(), reason_code=reason_code, effective_date=effective_date, created_by=user, **kw
    )


def pdf_upload(name="request.pdf", data=PDF):
    return SimpleUploadedFile(name, data, content_type="application/pdf")


def attach(case, user, doc_type=DocType.SIGNED_REQUEST):
    return save_uploaded_document(case, pdf_upload(), doc_type, "client", user)


def make_sent_case(user, **kw):
    """A case already in SENT status with an outbound email recorded."""
    from cancellations.services.sending import send_case

    case = make_case(user, **kw)
    attach(case, user)
    send_case(case, user)
    case.refresh_from_db()
    assert case.status == S.SENT
    return case


def inbound(**kw):
    defaults = dict(
        from_email="Claims Desk <claims@insurer.test>",
        from_address="claims@insurer.test",
        to=["cancellations@imoth.test"],
        subject="Re: your request",
        text="Hello, noted.",
        message_id="<reply-1@insurer.test>",
        headers=dict(AUTH_PASS),
    )
    defaults.update(kw)
    return InboundEmail(**defaults)
