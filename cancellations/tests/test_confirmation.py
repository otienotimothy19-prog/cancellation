import datetime as dt
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from cancellations.models import CaseEmail, Cover
from cancellations.reasons import DocType
from cancellations.services.inbound import Attachment, ingest
from cancellations.services.refunds import pro_rata_refund, unearned_days
from cancellations.services.workflow import S, TransitionError, close_case, review_inbound_email

from .factories import PDF, inbound, make_sent_case, make_user

C = CaseEmail.Classification
D = dt.date


class ProRataRefundTests(TestCase):
    """Cover 1 Jan - 31 Dec 2026 (365 days, both ends included); effective date = first uncovered day."""

    def test_mid_term(self):
        # 30 Jun .. 31 Dec = 185 unearned days
        self.assertEqual(unearned_days(D(2026, 1, 1), D(2026, 12, 31), D(2026, 6, 30)), 185)
        self.assertEqual(pro_rata_refund(Decimal("36500"), D(2026, 1, 1), D(2026, 12, 31), D(2026, 6, 30)), Decimal("18500.00"))

    def test_from_inception_is_full_refund(self):
        self.assertEqual(pro_rata_refund(Decimal("36500"), D(2026, 1, 1), D(2026, 12, 31), D(2026, 1, 1)), Decimal("36500.00"))

    def test_last_day_and_after_expiry(self):
        self.assertEqual(pro_rata_refund(Decimal("36500"), D(2026, 1, 1), D(2026, 12, 31), D(2026, 12, 31)), Decimal("100.00"))
        self.assertEqual(pro_rata_refund(Decimal("36500"), D(2026, 1, 1), D(2026, 12, 31), D(2027, 1, 1)), Decimal("0.00"))

    def test_leap_year_and_rounding(self):
        # 1 Mar 2027 - 29 Feb 2028 = 366 days; effective 1 Dec 2027 -> 91 days unearned
        amount = pro_rata_refund(Decimal("10000"), D(2027, 3, 1), D(2028, 2, 29), D(2027, 12, 1))
        self.assertEqual(amount, Decimal("2486.34"))  # 10000*91/366 = 2486.338.. -> half-up

    def test_zero_premium(self):
        self.assertEqual(pro_rata_refund(Decimal("0"), D(2026, 1, 1), D(2026, 12, 31), D(2026, 6, 30)), Decimal("0.00"))


class ConfirmTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.case = make_sent_case(self.user)  # premium 36500, cover 2026, requested 30 Jun 2026
        self.reply, _ = ingest(inbound(
            subject=f"Re: {self.case.reference}",
            attachments=[Attachment("credit-note.pdf", "application/pdf", PDF)],
        ))

    def confirm(self, **kw):
        kw.setdefault("confirmed_effective_date", D(2026, 6, 30))
        return review_inbound_email(self.reply, C.CONFIRMATION, self.user, **kw)

    def test_confirm_records_date_refund_and_cancels_cover(self):
        self.confirm(confirmed_effective_date=D(2026, 7, 1))
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.CONFIRMED)
        self.assertEqual(self.case.confirmed_effective_date, D(2026, 7, 1))
        self.assertEqual(self.case.refund_amount, Decimal("18400.00"))  # 184/365 of 36500
        self.assertIsNotNone(self.case.confirmed_at)
        self.assertEqual(self.case.cover.status, Cover.Status.CANCELLED)
        actions = list(self.case.events.values_list("action", flat=True))
        self.assertIn("MARKED_CONFIRMED", actions)
        self.assertIn("COVER_CANCELLED", actions)
        note = self.case.events.get(action="MARKED_CONFIRMED").note
        self.assertIn("18400.00", note)
        self.assertIn("requested 30 Jun 2026", note)

    def test_date_required_and_within_cover(self):
        with self.assertRaises(TransitionError):
            self.confirm(confirmed_effective_date=None)
        with self.assertRaises(TransitionError):
            self.confirm(confirmed_effective_date=D(2027, 2, 1))
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.SENT)
        self.assertEqual(self.case.cover.status, Cover.Status.ACTIVE)

    def test_certificate_from_email_attachment(self):
        doc = self.reply.documents.get()
        self.confirm(certificate_doc=doc)
        doc.refresh_from_db()
        self.assertEqual(doc.type, DocType.CERTIFICATE)
        self.assertTrue(self.case.has_certificate)
        self.assertTrue(self.case.events.filter(action="CERTIFICATE_RECORDED").exists())
        # confirmed + certificate = complete, so it closes by itself
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.CLOSED)
        closed = self.case.events.get(action="CLOSED")
        self.assertEqual((closed.from_status, closed.to_status, closed.actor), (S.CONFIRMED, S.CLOSED, self.user))
        self.assertIn("automatically", closed.note)
        self.assertEqual(self.case.confirmed_effective_date, D(2026, 6, 30))  # confirmation data kept

    def test_certificate_upload(self):
        self.confirm(certificate_upload=SimpleUploadedFile("cert.pdf", PDF, "application/pdf"))
        self.assertEqual(self.case.documents.get(type=DocType.CERTIFICATE).original_name, "cert.pdf")

    def test_certificate_must_belong_to_the_email(self):
        other = make_sent_case(self.user)
        other_reply, _ = ingest(inbound(message_id="<o@insurer.test>", subject=f"Re: {other.reference}",
                                        attachments=[Attachment("x.pdf", "application/pdf", PDF)]))
        with self.assertRaises(TransitionError):
            self.confirm(certificate_doc=other_reply.documents.get())

    def test_cannot_close_confirmed_case_without_certificate(self):
        self.confirm()
        with self.assertRaises(TransitionError):
            close_case(self.case, self.user)

    def test_rejection_does_not_cancel_cover(self):
        review_inbound_email(self.reply, C.REJECTION, self.user)
        self.case.cover.refresh_from_db()
        self.assertEqual(self.case.cover.status, Cover.Status.ACTIVE)
        self.case.refresh_from_db()
        self.assertIsNone(self.case.refund_amount)

    def test_confirm_via_page(self):
        self.client.force_login(self.user)
        doc = self.reply.documents.get()
        r = self.client.post(reverse("cancellations:email_review", args=[self.reply.pk]), {
            "classification": "CONFIRMATION", "confirmed_effective_date": "2026-06-30", "certificate_doc": doc.pk,
        }, follow=True)
        self.assertContains(r, "pro-rata refund 18500.00")
        self.assertContains(r, "case is closed")
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.CLOSED)
        self.assertTrue(self.case.has_certificate)

    def test_confirm_without_certificate_stays_open_until_upload(self):
        self.client.force_login(self.user)
        self.client.post(reverse("cancellations:email_review", args=[self.reply.pk]), {
            "classification": "CONFIRMATION", "confirmed_effective_date": "2026-06-30"})
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.CONFIRMED)
        # Another document type does not close it...
        self.client.post(reverse("cancellations:document_upload", args=[self.case.pk]), {
            "doc-doc_type": DocType.OTHER, "doc-source": "insurer",
            "doc-file": SimpleUploadedFile("note.pdf", PDF, "application/pdf")})
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.CONFIRMED)
        # ...the certificate does.
        r = self.client.post(reverse("cancellations:document_upload", args=[self.case.pk]), {
            "doc-doc_type": DocType.CERTIFICATE, "doc-source": "insurer",
            "doc-file": SimpleUploadedFile("cert.pdf", PDF, "application/pdf")}, follow=True)
        self.assertContains(r, "complete and has been closed")
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.CLOSED)

    def test_rejected_case_is_not_auto_closed(self):
        review_inbound_email(self.reply, C.REJECTION, self.user)
        self.client.force_login(self.user)
        self.client.post(reverse("cancellations:document_upload", args=[self.case.pk]), {
            "doc-doc_type": DocType.CERTIFICATE, "doc-source": "insurer",
            "doc-file": SimpleUploadedFile("cert.pdf", PDF, "application/pdf")})
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.REJECTED)

    def test_confirm_via_page_without_date_is_refused(self):
        self.client.force_login(self.user)
        r = self.client.post(reverse("cancellations:email_review", args=[self.reply.pk]),
                             {"classification": "CONFIRMATION"}, follow=True)
        self.assertContains(r, "Enter the effective date the insurer confirmed")
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.SENT)
