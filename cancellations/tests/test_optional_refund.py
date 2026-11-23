"""Premium and cover dates are optional: without them a case still sends and confirms, just with no refund."""

import datetime as dt

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from cancellations.models import CancellationCase, CaseEmail
from cancellations.services.inbound import ingest
from cancellations.services.sending import render_email
from cancellations.services.workflow import S, review_inbound_email

from .factories import inbound, make_cover, make_insurer, make_sent_case, make_user, pdf_upload

TOKEN = "test-token-commission"


class OptionalRefundFieldsTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.insurer = make_insurer()

    def data(self, **overrides):
        d = {
            "cover-client_name": "Peter Otieno", "cover-policy_number": "MTR-777", "cover-insurer": self.insurer.pk,
            "case-reason_code": "CLIENT_REQUEST", "case-effective_date": "2026-07-01",
            "doc-doc_type": "SIGNED_REQUEST", "doc-source": "client",
        }
        d.update(overrides)
        return d

    def test_send_without_premium_or_dates(self):
        r = self.client.post(reverse("cancellations:case_create"),
                             {**self.data(), "doc-file": pdf_upload(), "action": "send"}, follow=True)
        case = CancellationCase.objects.get()
        self.assertEqual(case.status, S.SENT, r.content[:2000])
        self.assertEqual(len(mail.outbox), 1)
        self.assertIsNone(case.cover.premium)
        self.assertContains(self.client.get(case.get_absolute_url()), "Not recorded")

    def test_partial_refund_fields_are_rejected(self):
        r = self.client.post(reverse("cancellations:case_create"), {**self.data(), "cover-premium": "48000", "action": "draft"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(CancellationCase.objects.exists())
        self.assertIn("start_date", r.context["cover_form"].errors)
        self.assertIn("end_date", r.context["cover_form"].errors)

    def test_all_three_still_work(self):
        r = self.client.post(reverse("cancellations:case_create"), {
            **self.data(), "cover-premium": "48000", "cover-start_date": "2026-01-01", "cover-end_date": "2026-12-31",
            "action": "draft"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(CancellationCase.objects.get().cover.can_calculate_refund)

    def test_confirm_without_refund_inputs(self):
        cover = make_cover(self.insurer, premium=None, start_date=None, end_date=None)
        case = make_sent_case(self.user, cover=cover)
        _subject, body = render_email(case)
        self.assertNotIn("None", body)
        reply, _ = ingest(inbound(subject=f"Re: {case.reference}"))
        review_inbound_email(reply, CaseEmail.Classification.CONFIRMATION, self.user,
                             confirmed_effective_date=dt.date(2027, 3, 1))  # no cover period to check against
        case.refresh_from_db()
        self.assertEqual(case.status, S.CONFIRMED)
        self.assertIsNone(case.refund_amount)
        self.assertIn("No refund calculated", case.events.get(action="MARKED_CONFIRMED").note)
        self.assertContains(self.client.get(case.get_absolute_url()), "No refund calculated")
        self.assertEqual(self.client.get(reverse("cancellations:dashboard")).status_code, 200)

        row = self.client.get(reverse("cancellations:confirmed_api"), HTTP_AUTHORIZATION=f"Bearer {TOKEN}").json()["results"][0]
        self.assertEqual((row["refund_amount"], row["premium"], row["cover_start_date"]), (None, None, None))
        csv_text = self.client.get(reverse("cancellations:confirmed_csv")).content.decode("utf-8-sig")
        self.assertNotIn("None", csv_text)
