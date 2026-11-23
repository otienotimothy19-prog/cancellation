"""IMAP polling (Zoho Mail) with a fake IMAP server, and raw-MIME replies with Zoho-style headers."""

import base64

from django.test import TestCase, override_settings

from cancellations.models import CaseEmail, MailboxState
from cancellations.services.mailbox import MAX_ATTEMPTS, poll

from .factories import PDF, make_sent_case, make_user

ZOHO_AUTH = "mx.zohomail.com; dkim=pass; spf=pass (zohomail.com: domain of insurer.test designates 1.2.3.4 as permitted sender) smtp.mailfrom=b@insurer.test; dmarc=pass header.from=<claims@insurer.test>"


def raw_email(subject, message_id, to="cancellation@imoth.test", auth=ZOHO_AUTH, extra_headers="", attachment=False,
              from_="Claims Desk <claims@insurer.test>"):
    head = (
        f"Delivered-To: {to}\r\n"
        "Return-Path: <b@insurer.test>\r\n"
        + (f"Authentication-Results: {auth}\r\n" if auth else "")
        + extra_headers
        + f"From: {from_}\r\nTo: {to}\r\nSubject: {subject}\r\nMessage-ID: {message_id}\r\n"
        "Date: Mon, 23 Nov 2026 10:00:00 +0300\r\nMIME-Version: 1.0\r\n"
    )
    if not attachment:
        return (head + "Content-Type: text/plain; charset=utf-8\r\n\r\nCancellation confirmed.\r\n").encode()
    pdf = base64.b64encode(PDF).decode()
    return (
        head + 'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/plain\r\n\r\nCredit note attached.\r\n"
        '--B\r\nContent-Type: application/pdf; name="credit-note.pdf"\r\n'
        'Content-Disposition: attachment; filename="credit-note.pdf"\r\nContent-Transfer-Encoding: base64\r\n\r\n'
        f"{pdf}\r\n--B--\r\n"
    ).encode()


class FakeIMAP:
    """Just enough of imaplib.IMAP4 for the poller."""

    def __init__(self, messages, uidvalidity=1):
        self.messages = dict(messages)  # uid -> raw bytes
        self.uidvalidity = uidvalidity
        self.fetched = []
        self.readonly = None

    def select(self, folder, readonly=False):
        self.readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [str(self.uidvalidity).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            uids = sorted(self.messages)
            if args[1] == "UID":
                start = int(args[2].split(":")[0])
                matching = [u for u in uids if u >= start] or uids[-1:]  # IMAP "n:*" quirk
            else:
                matching = uids
            return "OK", [" ".join(map(str, matching)).encode()]
        if command == "FETCH":
            uid = int(args[0])
            assert args[1] == "(BODY.PEEK[])", "must not mark messages as read"
            self.fetched.append(uid)
            return "OK", [(f"{uid} (UID {uid} BODY[] {{1}}".encode(), self.messages[uid]), b")"]
        raise AssertionError(command)

    def logout(self):
        pass


@override_settings(MAIL_DOMAIN="imoth.test", CANCELLATIONS_MAILBOX="cancellation", EMAIL_PROVIDER="zoho",
                   INBOUND_AUTHSERV_ID="mx.zohomail.com", IMAP_FOLDER="INBOX")
class MailboxPollTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.case = make_sent_case(self.user)

    def test_reply_via_plus_address_is_matched_and_verified(self):
        to = f"cancellation+CXL{self.case.pk:06d}@imoth.test"
        imap = FakeIMAP({5: raw_email("RE: hello", "<z1@insurer.test>", to=to, attachment=True)})
        result = poll(client=imap)
        self.assertEqual((result.fetched, result.created, result.error), (1, 1, ""))
        self.assertTrue(imap.readonly)
        email = CaseEmail.objects.get(message_id="<z1@insurer.test>")
        self.assertEqual(email.case, self.case)
        self.assertEqual(email.matched_by, CaseEmail.MatchedBy.PLUS_ADDRESS)
        self.assertTrue(email.sender_verified)
        self.assertEqual(email.documents.get().original_name, "credit-note.pdf")
        self.assertEqual(MailboxState.objects.get(folder="INBOX").last_uid, 5)

    def test_only_new_uids_fetched_on_next_poll(self):
        imap = FakeIMAP({1: raw_email(f"Re: {self.case.reference}", "<a@insurer.test>")})
        poll(client=imap)
        imap.messages[2] = raw_email("unrelated", "<b@other.test>")
        imap.fetched.clear()
        result = poll(client=imap)
        self.assertEqual(imap.fetched, [2])
        self.assertEqual(result.created, 1)
        result = poll(client=imap)  # nothing new; "2:*" quirk returns 2 again but it is filtered
        self.assertEqual(result.fetched, 0)

    def test_uidvalidity_change_rescans_without_duplicates(self):
        imap = FakeIMAP({1: raw_email(f"Re: {self.case.reference}", "<a@insurer.test>")})
        poll(client=imap)
        renumbered = FakeIMAP({77: imap.messages[1]}, uidvalidity=2)
        result = poll(client=renumbered)
        self.assertEqual((result.fetched, result.created, result.duplicates), (1, 0, 1))
        self.assertEqual(CaseEmail.objects.filter(direction="IN").count(), 1)

    def test_without_zoho_auth_header_goes_to_unmatched(self):
        imap = FakeIMAP({1: raw_email(f"Re: {self.case.reference}", "<a@insurer.test>", auth=None)})
        poll(client=imap)
        email = CaseEmail.objects.get(message_id="<a@insurer.test>")
        self.assertIsNone(email.case)
        self.assertEqual(email.suggested_case, self.case)

    def test_forged_zoho_header_below_real_one_is_ignored(self):
        forged = "Authentication-Results: mx.zohomail.com; dmarc=pass\r\n"
        imap = FakeIMAP({1: raw_email(f"Re: {self.case.reference}", "<a@insurer.test>",
                                      auth="mx.zohomail.com; spf=none; dmarc=none", extra_headers=forged)})
        poll(client=imap)
        self.assertIsNone(CaseEmail.objects.get(message_id="<a@insurer.test>").case)

    @override_settings(INBOUND_AUTH_HEADER="ARC-Authentication-Results")
    def test_arc_header_when_configured(self):
        arc = f"ARC-Authentication-Results: i=1; {ZOHO_AUTH}\r\n"
        imap = FakeIMAP({1: raw_email(f"Re: {self.case.reference}", "<a@insurer.test>", auth=None, extra_headers=arc)})
        poll(client=imap)
        self.assertEqual(CaseEmail.objects.get(message_id="<a@insurer.test>").case, self.case)

    def test_failing_message_retried_then_skipped(self):
        from unittest import mock

        imap = FakeIMAP({1: raw_email("x", "<bad@x.test>"), 2: raw_email("y", "<good@x.test>")})
        real_ingest = __import__("cancellations.services.mailbox", fromlist=["ingest"]).ingest

        def flaky(msg):
            if msg.message_id == "<bad@x.test>":
                raise ValueError("boom")
            return real_ingest(msg)

        with mock.patch("cancellations.services.mailbox.ingest", side_effect=flaky):
            for attempt in range(1, MAX_ATTEMPTS):
                result = poll(client=imap)
                self.assertEqual(result.fetched, 0, attempt)
                self.assertEqual(MailboxState.objects.get().last_uid, 0)
            result = poll(client=imap)
        self.assertEqual(result.skipped, [1])
        self.assertEqual(result.created, 1)
        state = MailboxState.objects.get()
        self.assertEqual(state.last_uid, 2)
        self.assertIn("Skipped UID 1", state.last_error)

    def test_imap_failure_is_recorded_not_raised(self):
        class Broken(FakeIMAP):
            def select(self, folder, readonly=False):
                return "NO", [b"no such folder"]

        result = poll(client=Broken({}))
        self.assertIn("SELECT", result.error)
        self.assertIn("SELECT", MailboxState.objects.get().last_error)
