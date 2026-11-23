import base64
import json

from django.test import TestCase
from django.urls import reverse

from cancellations.models import CaseDocument, CaseEmail, CaseEvent
from cancellations.services.inbound import Attachment, ingest

from .factories import AUTH_PASS, PDF, inbound, make_sent_case, make_user


class IngestIdempotencyTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.case = make_sent_case(self.user)

    def test_same_message_twice_creates_one_record(self):
        msg = inbound(subject=f"Re: {self.case.reference}",
                      attachments=[Attachment("certificate.pdf", "application/pdf", PDF)])
        first, created1 = ingest(msg)
        second, created2 = ingest(msg)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(CaseEmail.objects.filter(direction=CaseEmail.Direction.INBOUND).count(), 1)
        self.assertEqual(CaseDocument.objects.filter(source="insurer").count(), 1)
        self.assertEqual(CaseEvent.objects.filter(action="EMAIL_RECEIVED").count(), 1)

    def test_unmatched_duplicate_is_also_ignored(self):
        msg = inbound(subject="no ref", text="nothing", message_id="<u1@x.test>")
        ingest(msg)
        ingest(msg)
        self.assertEqual(CaseEmail.objects.filter(case__isnull=True).count(), 1)

    def test_missing_message_id_falls_back_to_content_hash(self):
        msg = inbound(message_id="", subject=f"Re: {self.case.reference}")
        ingest(msg)
        ingest(msg)
        self.assertEqual(CaseEmail.objects.filter(direction=CaseEmail.Direction.INBOUND).count(), 1)

    def test_bad_attachment_is_skipped_not_stored(self):
        msg = inbound(subject=f"Re: {self.case.reference}", attachments=[Attachment("run.exe", "application/x", b"MZ..")])
        email, _ = ingest(msg)
        self.assertEqual(email.documents.count(), 0)
        self.assertEqual(email.skipped_attachments[0]["filename"], "run.exe")


class PostmarkWebhookTests(TestCase):
    """End-to-end through the real webhook URL (django-anymail's Postmark parser)."""

    def setUp(self):
        self.user = make_user()
        self.case = make_sent_case(self.user)
        self.url = reverse("anymail:postmark_inbound_webhook")
        self.auth = "Basic " + base64.b64encode(b"hook:secret").decode()

    def payload(self):
        return {
            "FromFull": {"Email": "claims@insurer.test", "Name": "Claims"},
            "ToFull": [{"Email": f"cancellations+CXL{self.case.pk:06d}@imoth.test", "Name": ""}],
            "CcFull": [],
            "OriginalRecipient": f"cancellations+CXL{self.case.pk:06d}@imoth.test",
            "Subject": f"RE: {self.case.reference}",
            "MessageID": "73e6d360-66eb-11e1-8e72-a8904824019b",
            "TextBody": "Cancelled as requested.",
            "HtmlBody": "",
            "Headers": [
                {"Name": "Return-Path", "Value": "<bounce@insurer.test>"},
                {"Name": "Message-ID", "Value": "<abc123@insurer.test>"},
                {"Name": "Authentication-Results", "Value": AUTH_PASS["Authentication-Results"]},
            ],
            "Attachments": [{"Name": "credit-note.pdf", "ContentType": "application/pdf",
                             "Content": base64.b64encode(PDF).decode(), "ContentLength": len(PDF)}],
        }

    def post(self, **extra):
        return self.client.post(self.url, json.dumps(self.payload()), content_type="application/json", **extra)

    def test_requires_basic_auth(self):
        self.assertEqual(self.post().status_code, 400)
        self.assertFalse(CaseEmail.objects.filter(direction=CaseEmail.Direction.INBOUND).exists())

    def test_delivered_twice_stored_once(self):
        self.assertEqual(self.post(HTTP_AUTHORIZATION=self.auth).status_code, 200)
        self.assertEqual(self.post(HTTP_AUTHORIZATION=self.auth).status_code, 200)
        emails = CaseEmail.objects.filter(direction=CaseEmail.Direction.INBOUND)
        self.assertEqual(emails.count(), 1)
        email = emails.get()
        self.assertEqual(email.message_id, "<abc123@insurer.test>")
        self.assertEqual(email.case, self.case)
        self.assertEqual(email.matched_by, CaseEmail.MatchedBy.PLUS_ADDRESS)
        self.assertEqual(email.documents.get().original_name, "credit-note.pdf")
        self.assertEqual(CaseDocument.objects.filter(source="insurer").count(), 1)
