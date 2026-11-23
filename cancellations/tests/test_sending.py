import datetime as dt
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.test import TestCase

from cancellations.models import CaseEmail
from cancellations.reasons import DocType
from cancellations.services.sending import SendBlocked, send_blockers, send_case
from cancellations.services.workflow import S

from .factories import attach, make_case, make_cover, make_insurer, make_user


class SendBlockingTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def assertBlocked(self, case, expected_fragment):
        with self.assertRaises(SendBlocked) as ctx:
            send_case(case, self.user)
        self.assertTrue(any(expected_fragment in p for p in ctx.exception.problems), ctx.exception.problems)
        case.refresh_from_db()
        self.assertEqual(case.status, S.DRAFT)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(case.events.filter(action="SENT").exists())

    def test_missing_reason(self):
        case = make_case(self.user, reason_code="")
        self.assertBlocked(case, "Reason code is missing")

    def test_missing_effective_date(self):
        case = make_case(self.user, effective_date=None)
        attach(case, self.user)
        self.assertBlocked(case, "Effective date is missing")

    def test_missing_required_document(self):
        case = make_case(self.user, reason_code="CLIENT_REQUEST")
        self.assertBlocked(case, "Signed client request")

    def test_wrong_document_type_does_not_count(self):
        case = make_case(self.user, reason_code="ASSET_DISPOSED")
        attach(case, self.user, DocType.SIGNED_REQUEST)
        self.assertBlocked(case, "Proof of sale")

    def test_each_reason_document_requirement(self):
        for code, doc in [("ASSET_DISPOSED", "Proof of sale"), ("MISREPRESENTATION", "Evidence"),
                          ("INSURER_INITIATED", "Insurer's notice"), ("CLIENT_REQUEST", "Signed client request")]:
            with self.subTest(code=code):
                case = make_case(self.user, cover=make_cover(policy_number=f"P-{code}", insurer=make_insurer(code)),
                                 reason_code=code)
                self.assertTrue(any(doc in p for p in send_blockers(case)))

    def test_reason_specific_fields(self):
        cases = [
            ("NON_PAYMENT", "outstanding amount"), ("NON_PAYMENT", "notice date"),
            ("REPLACED", "new policy number"), ("DUPLICATE", "original cover reference"),
        ]
        for code, fragment in cases:
            with self.subTest(code=code, fragment=fragment):
                case = make_case(self.user, cover=make_cover(policy_number=f"P-{code}-{fragment}",
                                                             insurer=make_insurer(f"{code}{fragment}")),
                                 reason_code=code)
                self.assertTrue(any(fragment in p for p in send_blockers(case)), send_blockers(case))

    def test_non_payment_with_fields_needs_no_document(self):
        case = make_case(self.user, reason_code="NON_PAYMENT", outstanding_amount=Decimal("1200"),
                         notice_date=dt.date(2026, 5, 1))
        self.assertEqual(send_blockers(case), [])

    def test_insurer_signed_form_required(self):
        insurer = make_insurer("Strict Co", requires_signed_form=True)
        case = make_case(self.user, cover=make_cover(insurer))
        attach(case, self.user)
        self.assertBlocked(case, "signed cancellation form")
        attach(case, self.user, DocType.SIGNED_FORM)
        send_case(case, self.user)
        case.refresh_from_db()
        self.assertEqual(case.status, S.SENT)

    def test_cannot_send_twice(self):
        case = make_case(self.user)
        attach(case, self.user)
        send_case(case, self.user)
        self.assertBlocked_after_send(case)

    def assertBlocked_after_send(self, case):
        with self.assertRaises(SendBlocked):
            send_case(case, self.user)
        self.assertEqual(len(mail.outbox), 1)


class SendSuccessTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.case = make_case(self.user, reason_note="Client sold the car")
        attach(self.case, self.user)

    def test_send_emails_insurer_and_marks_sent(self):
        send_case(self.case, self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.SENT)
        self.assertIsNotNone(self.case.sent_at)

        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        ref = self.case.reference
        self.assertRegex(ref, r"^IMOTH-CXL-\d{6}$")
        self.assertEqual(msg.to, ["cancel@insurer.test"])
        self.assertEqual(msg.cc, ["ops@insurer.test"])
        self.assertEqual(msg.reply_to, [f"cancellations+CXL{self.case.pk:06d}@imoth.test"])
        self.assertIn(ref, msg.subject)
        for text in (ref, "POL-1001", "Jane Wanjiru", "Client request", "30 June 2026", "Client sold the car"):
            self.assertIn(text, msg.body)
        self.assertEqual(len(msg.attachments), 1)
        self.assertEqual(msg.attachments[0][0], "request.pdf")

        out = CaseEmail.objects.get(case=self.case, direction=CaseEmail.Direction.OUTBOUND)
        self.assertEqual(msg.extra_headers["Message-ID"], out.message_id)
        event = self.case.events.get(action="SENT")
        self.assertEqual((event.from_status, event.to_status, event.actor), (S.DRAFT, S.SENT, self.user))
        self.assertEqual(event.email, out)

    def test_per_insurer_template(self):
        insurer = self.case.cover.insurer
        insurer.email_template = "Custom for {{ insurer_name }}: {{ policy_number }} / {{ reference }} <{{ client_name }}>"
        insurer.save()
        send_case(self.case, self.user)
        self.assertEqual(mail.outbox[0].body, f"Custom for Acme Insurance: POL-1001 / {self.case.reference} <Jane Wanjiru>")

    def test_provider_failure_keeps_draft_and_logs(self):
        with mock.patch("django.core.mail.EmailMessage.send", side_effect=RuntimeError("provider down")):
            with self.assertRaises(RuntimeError):
                send_case(self.case, self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.DRAFT)
        self.assertFalse(CaseEmail.objects.filter(case=self.case).exists())
        self.assertTrue(self.case.events.filter(action="SEND_FAILED", note__contains="provider down").exists())
        self.assertFalse(self.case.events.filter(action="SENT").exists())
