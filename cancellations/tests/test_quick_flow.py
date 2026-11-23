"""The short path: Save & send in one step (with preview), and one-click review links."""

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from cancellations.models import CancellationCase
from cancellations.services.inbound import ingest
from cancellations.services.workflow import S

from .factories import inbound, make_insurer, make_sent_case, make_user, pdf_upload


class SaveAndSendTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.insurer = make_insurer()

    def data(self, **overrides):
        d = {
            "cover-client_name": "Peter Otieno", "cover-policy_number": "MTR-777", "cover-insurer": self.insurer.pk,
            "cover-asset_reference": "KCB 777Z", "cover-premium": "48000", "cover-start_date": "2026-01-01",
            "cover-end_date": "2026-12-31", "case-reason_code": "CLIENT_REQUEST", "case-effective_date": "2026-07-01",
            "doc-doc_type": "SIGNED_REQUEST", "doc-source": "client",
        }
        d.update(overrides)
        return d

    def test_save_and_send_in_one_step(self):
        r = self.client.post(reverse("cancellations:case_create"), {**self.data(), "doc-file": pdf_upload(), "action": "send"},
                             follow=True)
        case = CancellationCase.objects.get()
        self.assertEqual(case.status, S.SENT)
        self.assertContains(r, f"{case.reference} sent to cancel@insurer.test")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(list(case.events.values_list("action", flat=True)), ["CREATED", "DOCUMENT_ADDED", "SENT"])

    def test_save_and_send_with_missing_document_keeps_draft(self):
        r = self.client.post(reverse("cancellations:case_create"),
                             {**self.data(**{"doc-doc_type": ""}), "action": "send"}, follow=True)
        case = CancellationCase.objects.get()
        self.assertEqual(case.status, S.DRAFT)
        self.assertContains(r, "not sent yet")
        self.assertContains(r, "Required document missing: Signed client request")
        self.assertEqual(len(mail.outbox), 0)

    def test_plain_save_is_still_a_draft(self):
        self.client.post(reverse("cancellations:case_create"), {**self.data(), "doc-file": pdf_upload(), "action": "draft"})
        self.assertEqual(CancellationCase.objects.get().status, S.DRAFT)
        self.assertEqual(len(mail.outbox), 0)

    def preview(self, **overrides):
        return self.client.post(reverse("cancellations:case_preview"), self.data(**overrides)).json()

    def test_preview_shows_exact_email_and_saves_nothing(self):
        res = self.preview(**{"doc-has_file": "1"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["to"], "cancel@insurer.test")
        self.assertEqual(res["cc"], ["ops@insurer.test"])
        self.assertIn("MTR-777", res["subject"])
        for text in ("Peter Otieno", "MTR-777", "KCB 777Z", "Client request", "01 July 2026"):
            self.assertIn(text, res["body"])
        self.assertFalse(CancellationCase.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_preview_lists_what_blocks_sending(self):
        res = self.preview(**{"doc-has_file": "0"})
        self.assertFalse(res["ok"])
        self.assertIn("Required document missing: Signed client request.", res["problems"])
        self.assertTrue(res["body"])  # still shows the email so it can be checked
        insurer = make_insurer("Strict Co", requires_signed_form=True)
        res = self.preview(**{"doc-has_file": "1", "cover-insurer": insurer.pk})
        self.assertTrue(any("signed cancellation form" in p for p in res["problems"]), res["problems"])
        res = self.preview(**{"case-reason_code": "NON_PAYMENT", "doc-has_file": "0"})
        self.assertTrue(any("outstanding amount" in p for p in res["problems"]), res["problems"])

    def test_preview_reports_form_errors(self):
        res = self.preview(**{"cover-client_name": "", "case-reason_code": "BOGUS"})
        self.assertFalse(res["ok"])
        self.assertTrue(res["form_errors"])
        joined = " ".join(res["problems"])
        self.assertIn("Required for a new cover", joined)  # cover section
        self.assertIn("Select a valid choice", joined)  # case section, reported in the same pass
        res = self.preview(**{"case-effective_date": "2027-06-01"})
        self.assertIn("within the cover period", " ".join(res["problems"]))

    def test_preview_requires_role(self):
        self.client.force_login(make_user("norole", role=False))
        self.assertEqual(self.client.post(reverse("cancellations:case_preview"), self.data()).status_code, 403)


class ReviewLinkTests(TestCase):
    def test_dashboard_links_straight_to_the_reply(self):
        user = make_user()
        self.client.force_login(user)
        case = make_sent_case(user)
        reply, _ = ingest(inbound(subject=f"Re: {case.reference}"))
        anchor = f"{case.get_absolute_url()}#email-{reply.pk}"
        self.assertContains(self.client.get(reverse("cancellations:dashboard")), f'href="{anchor}"')
        self.assertContains(self.client.get(case.get_absolute_url()), f'id="email-{reply.pk}"')


class SimplifiedFormTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.insurer = make_insurer()

    def test_effective_date_defaults_to_today(self):
        from django.utils import timezone

        r = self.client.get(reverse("cancellations:case_create"))
        self.assertEqual(r.context["case_form"]["effective_date"].value(), timezone.localdate())
        self.assertContains(r, "Client &amp; cancellation")
        self.assertContains(r, "Document &amp; review")
        self.assertNotContains(r, 'name="doc-source"')

    def test_upload_needs_only_file_and_type(self):
        self.client.post(reverse("cancellations:case_create"), {
            "cover-client_name": "Peter Otieno", "cover-policy_number": "MTR-778", "cover-insurer": self.insurer.pk,
            "case-reason_code": "CLIENT_REQUEST", "case-effective_date": "2026-07-01",
            "doc-doc_type": "SIGNED_REQUEST", "doc-file": pdf_upload(), "action": "draft",
        })
        doc = CancellationCase.objects.get().documents.get()
        self.assertEqual(doc.source, "client")

    def test_date_inputs_render_iso_values(self):
        from django.utils import timezone

        r = self.client.get(reverse("cancellations:case_create"))
        self.assertContains(r, f'value="{timezone.localdate().isoformat()}"')

    def test_effective_time_defaults_to_now_and_reaches_the_email(self):
        import datetime as dt

        from cancellations.services.sending import render_email

        r = self.client.get(reverse("cancellations:case_create"))
        self.assertIsNotNone(r.context["case_form"]["effective_time"].value())
        self.assertContains(r, 'type="time"')
        self.client.post(reverse("cancellations:case_create"), {
            "cover-client_name": "Peter Otieno", "cover-policy_number": "MTR-779", "cover-insurer": self.insurer.pk,
            "case-reason_code": "CLIENT_REQUEST", "case-effective_date": "2026-07-01", "case-effective_time": "14:30",
            "doc-doc_type": "SIGNED_REQUEST", "doc-file": pdf_upload(), "action": "draft",
        })
        case = CancellationCase.objects.get()
        self.assertEqual(case.effective_time, dt.time(14, 30))
        self.assertIn("Effective date:  01 July 2026 at 14:30", render_email(case)[1])
        self.assertContains(self.client.get(case.get_absolute_url()), "01 Jul 2026 at 14:30")
