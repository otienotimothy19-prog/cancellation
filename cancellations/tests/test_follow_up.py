from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from cancellations.models import CancellationCase, CaseEmail
from cancellations.reasons import DocType
from cancellations.services.inbound import ingest
from cancellations.services.sending import SendBlocked, send_follow_up
from cancellations.services.sla import check_sla
from cancellations.services.workflow import S, review_inbound_email

from .factories import PDF, attach, inbound, make_sent_case, make_user

C = CaseEmail.Classification


class FollowUpTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.user.email = "staff@imoth.test"
        self.user.save()
        self.case = make_sent_case(self.user)
        self.request_email = self.case.emails.get(direction=CaseEmail.Direction.OUTBOUND)
        self.query, _ = ingest(inbound(
            from_email="Underwriter <uw@insurer.test>", from_address="uw@insurer.test",
            subject=f"RE: Cancellation request {self.case.reference}", text="Please send the logbook copy.",
            message_id="<query-1@insurer.test>",
        ))
        review_inbound_email(self.query, C.QUERY, self.user, "Needs logbook")
        self.case.refresh_from_db()
        self.first_sent_at = self.case.sent_at
        mail.outbox.clear()

    def test_follow_up_threads_reply_and_returns_to_sent(self):
        logbook = attach(self.case, self.user, DocType.OTHER)
        send_follow_up(self.case, self.user, "Logbook attached.", [logbook])

        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["uw@insurer.test"])  # whoever raised the query
        self.assertEqual(msg.cc, ["cancel@insurer.test", "ops@insurer.test"])
        self.assertEqual(msg.reply_to, [self.case.reply_to_address])
        self.assertIn(self.case.reference, msg.subject)
        self.assertTrue(msg.subject.startswith("RE:"))
        self.assertEqual(msg.extra_headers["In-Reply-To"], "<query-1@insurer.test>")
        self.assertIn(self.request_email.message_id, msg.extra_headers["References"])
        self.assertIn("<query-1@insurer.test>", msg.extra_headers["References"])
        self.assertEqual([a[0] for a in msg.attachments], ["request.pdf"])

        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.SENT)
        self.assertEqual(self.case.sent_at, self.first_sent_at)  # original send time kept
        self.assertGreater(self.case.last_sent_at, self.first_sent_at)
        out = self.case.emails.get(classification=C.FOLLOW_UP)
        self.assertEqual(out.message_id, msg.extra_headers["Message-ID"])
        ev = self.case.events.get(action="FOLLOW_UP_SENT")
        self.assertEqual((ev.from_status, ev.to_status, ev.email, ev.actor), (S.QUERY, S.SENT, out, self.user))

    def test_insurer_reply_to_follow_up_matches_and_can_confirm(self):
        send_follow_up(self.case, self.user, "Logbook attached.")
        out = self.case.emails.get(classification=C.FOLLOW_UP)
        # Reply with no reference or plus address: matched purely by thread headers.
        reply, _ = ingest(inbound(subject="ok", text="Done.", message_id="<r2@insurer.test>", in_reply_to=out.message_id))
        self.assertEqual(reply.case, self.case)
        self.assertEqual(reply.matched_by, CaseEmail.MatchedBy.THREAD_HEADERS)
        review_inbound_email(reply, C.CONFIRMATION, self.user, confirmed_effective_date=self.case.effective_date)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.CONFIRMED)

    def test_sla_restarts_from_follow_up(self):
        CancellationCase.objects.filter(pk=self.case.pk).update(
            sla_reminder_sent_at=timezone.now(), is_stale=True, stale_at=timezone.now())
        send_follow_up(self.case, self.user, "Logbook attached.")
        self.case.refresh_from_db()
        self.assertFalse(self.case.is_stale)
        self.assertIsNone(self.case.sla_reminder_sent_at)
        mail.outbox.clear()
        # The query reply came before the follow-up, so it no longer counts as an answer.
        self.assertEqual(check_sla(now=self.case.last_sent_at + timedelta(hours=47)).reminded, [])
        self.assertEqual(check_sla(now=self.case.last_sent_at + timedelta(hours=49)).reminded, [self.case.reference])

    def test_blocked_unless_query_and_body(self):
        with self.assertRaises(SendBlocked):
            send_follow_up(self.case, self.user, "   ")
        other = make_sent_case(self.user)
        mail.outbox.clear()  # drop other's own cancellation request
        with self.assertRaises(SendBlocked):
            send_follow_up(other, self.user, "hello")  # SENT, not QUERY
        foreign_doc = attach(other, self.user, DocType.OTHER)
        with self.assertRaises(SendBlocked):
            send_follow_up(self.case, self.user, "hello", [foreign_doc])
        self.assertEqual(len(mail.outbox), 0)

    def test_send_failure_keeps_query_and_logs(self):
        with mock.patch("django.core.mail.EmailMessage.send", side_effect=OSError("smtp down")):
            with self.assertRaises(OSError):
                send_follow_up(self.case, self.user, "Logbook attached.")
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.QUERY)
        self.assertFalse(self.case.emails.filter(classification=C.FOLLOW_UP).exists())
        self.assertTrue(self.case.events.filter(action="SEND_FAILED", note__contains="Follow-up").exists())

    def test_follow_up_via_page_with_new_upload(self):
        self.client.force_login(self.user)
        page = self.client.get(self.case.get_absolute_url())
        self.assertContains(page, "Reply to the insurer")
        self.assertContains(page, "&gt; Please send the logbook copy.")  # query quoted in the draft
        r = self.client.post(reverse("cancellations:case_follow_up", args=[self.case.pk]), {
            "subject": f"RE: {self.case.reference}", "body": "Logbook attached.",
            "new_doc_type": DocType.OTHER, "new_file": SimpleUploadedFile("logbook.pdf", PDF, "application/pdf"),
        }, follow=True)
        self.assertContains(r, "SLA has restarted")
        self.assertEqual([a[0] for a in mail.outbox[0].attachments], ["logbook.pdf"])
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.SENT)
        self.assertNotContains(self.client.get(self.case.get_absolute_url()), "Reply to the insurer")
