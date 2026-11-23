from django.test import TestCase, override_settings

from cancellations.models import CaseEmail
from cancellations.services.inbound import ingest, match_case
from cancellations.services.workflow import S

from .factories import AUTH_PASS, inbound, make_case, make_cover, make_insurer, make_sent_case, make_user

M = CaseEmail.MatchedBy


class MatchingMethodsTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.case = make_sent_case(self.user)
        # A second sent case so a match is never "the only case there is".
        self.other = make_sent_case(self.user, cover=make_cover(self.case.cover.insurer, policy_number="POL-2002"))

    def assertMatched(self, msg, method, case=None):
        email, created = ingest(msg)
        self.assertTrue(created)
        self.assertEqual(email.case, case or self.case)
        self.assertEqual(email.matched_by, method)
        self.assertTrue(email.sender_verified)
        self.assertEqual(email.classification, CaseEmail.Classification.UNREVIEWED)
        return email

    def test_1_plus_address(self):
        self.assertMatched(inbound(to=[f"cancellations+CXL{self.case.pk:06d}@imoth.test"]), M.PLUS_ADDRESS)

    def test_plus_address_is_case_insensitive_and_checks_cc_and_envelope(self):
        self.assertMatched(inbound(cc=[f"Cancellations+cxl{self.case.pk:06d}@IMOTH.test"]), M.PLUS_ADDRESS)
        self.assertMatched(
            inbound(message_id="<r2@insurer.test>", envelope_recipient=f"cancellations+CXL{self.other.pk:06d}@imoth.test"),
            M.PLUS_ADDRESS, self.other,
        )

    def test_plus_address_on_another_domain_is_ignored(self):
        result = match_case(inbound(to=[f"cancellations+CXL{self.case.pk:06d}@evil.test"]))
        self.assertIsNone(result.case)

    def test_2_reference_in_subject(self):
        self.assertMatched(inbound(subject=f"RE: Cancellation request {self.case.reference} – confirmed"), M.SUBJECT_REFERENCE)

    def test_3_in_reply_to_header(self):
        sent = self.case.emails.get(direction=CaseEmail.Direction.OUTBOUND)
        self.assertMatched(inbound(in_reply_to=sent.message_id), M.THREAD_HEADERS)

    def test_3_references_header_and_provider_message_id(self):
        sent = self.case.emails.get(direction=CaseEmail.Direction.OUTBOUND)
        sent.provider_message_id = "0a1b2c3d-postmark"
        sent.save()
        # Some providers rewrite Message-ID; the reply then references the provider's id.
        self.assertMatched(inbound(references="<unrelated@x.test> <0a1b2c3d-postmark@mtasv.net>"), M.THREAD_HEADERS)

    def test_4_policy_number_in_body(self):
        self.assertMatched(inbound(text="We confirm policy pol-1001 is cancelled from 30 June."), M.POLICY_NUMBER)

    def test_4_policy_number_from_html_body(self):
        self.assertMatched(inbound(text="", html="<p>Policy <b>POL-2002</b> cancelled.</p>"), M.POLICY_NUMBER, self.other)

    def test_4_certificate_number_in_body(self):
        cover = self.case.cover
        cover.certificate_number = "C35561500"
        cover.save()
        msg = inbound(text="Certificate C35561500 has been cancelled.")
        self.assertMatched(msg, M.POLICY_NUMBER)
        self.assertIn("Certificate number C35561500", match_case(msg).note)

    def test_policy_number_must_be_whole_token(self):
        self.assertIsNone(match_case(inbound(text="Ref POL-10012 is unrelated")).case)

    def test_order_plus_address_wins_over_subject(self):
        msg = inbound(to=[f"cancellations+CXL{self.case.pk:06d}@imoth.test"], subject=f"Re: {self.other.reference}")
        self.assertMatched(msg, M.PLUS_ADDRESS)

    def test_order_subject_wins_over_body_policy_number(self):
        msg = inbound(subject=f"Re: {self.other.reference}", text="Policy POL-1001")
        self.assertMatched(msg, M.SUBJECT_REFERENCE, self.other)


class UnmatchedTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.case = make_sent_case(self.user)

    def assertUnmatched(self, msg, note_contains=""):
        email, created = ingest(msg)
        self.assertTrue(created)
        self.assertIsNone(email.case)
        self.assertTrue(email.is_unmatched)
        self.assertEqual(email.matched_by, M.NONE)
        if note_contains:
            self.assertIn(note_contains, email.match_note)
        self.assertFalse(self.case.events.filter(action="EMAIL_RECEIVED").exists())
        return email

    def test_nothing_matches(self):
        self.assertUnmatched(inbound(subject="Hello", text="Please call me"), "No reference")

    def test_reference_of_unknown_case(self):
        self.assertUnmatched(inbound(subject="Re: IMOTH-CXL-999999"))

    def test_ambiguous_policy_number_is_not_guessed(self):
        other_insurer = make_insurer("Other Co", cancellation_email="c@other.test", known_sender_addresses="")
        make_sent_case(self.user, cover=make_cover(other_insurer, policy_number="POL-1001"))
        self.assertUnmatched(inbound(text="Policy POL-1001 cancelled"), "several open cases")

    def test_policy_number_of_closed_or_draft_case_is_ignored(self):
        draft = make_case(self.user, cover=make_cover(self.case.cover.insurer, policy_number="POL-3003"))
        self.assertEqual(draft.status, S.DRAFT)
        self.assertUnmatched(inbound(text="Policy POL-3003"))

    def test_reply_to_a_draft_reference_is_unmatched(self):
        draft = make_case(self.user, cover=make_cover(self.case.cover.insurer, policy_number="POL-3004"))
        self.assertUnmatched(inbound(subject=f"Re: {draft.reference}"))


class SenderVerificationTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.case = make_sent_case(self.user)

    def test_unknown_sender_goes_to_unmatched_with_suggestion(self):
        email, _ = ingest(inbound(from_email="x@gmail.test", from_address="x@gmail.test", subject=f"Re: {self.case.reference}"))
        self.assertIsNone(email.case)
        self.assertEqual(email.suggested_case, self.case)
        self.assertFalse(email.sender_verified)
        self.assertIn("not a known address", email.match_note)

    def test_display_name_spoof_is_not_trusted(self):
        email, _ = ingest(inbound(
            from_email='"<claims@insurer.test>" <attacker@evil.test>', from_address="attacker@evil.test",
            subject=f"Re: {self.case.reference}",
        ))
        self.assertIsNone(email.case)

    def test_dmarc_fail_is_rejected_even_from_known_address(self):
        email, _ = ingest(inbound(
            subject=f"Re: {self.case.reference}",
            headers={"Authentication-Results": "mx.provider.test; spf=fail; dmarc=fail header.from=insurer.test"},
        ))
        self.assertIsNone(email.case)
        self.assertIn("DMARC failed", email.match_note)

    def test_no_authentication_evidence_is_not_verified(self):
        email, _ = ingest(inbound(subject=f"Re: {self.case.reference}", headers={}))
        self.assertIsNone(email.case)

    @override_settings(EMAIL_PROVIDER="postmark")
    def test_aligned_spf_pass_from_provider_header_is_accepted(self):
        email, _ = ingest(inbound(
            subject=f"Re: {self.case.reference}", envelope_sender="bounce@mail.insurer.test",
            headers={"Received-SPF": "Pass (sender SPF authorized) identity=mailfrom"},
        ))
        self.assertEqual(email.case, self.case)

    @override_settings(EMAIL_PROVIDER="postmark")
    def test_spf_pass_for_unaligned_envelope_sender_is_not_enough(self):
        email, _ = ingest(inbound(
            subject=f"Re: {self.case.reference}", envelope_sender="bounce@bulkmailer.test",
            headers={"Received-SPF": "Pass"},
        ))
        self.assertIsNone(email.case)

    @override_settings(EMAIL_PROVIDER="mailgun")
    def test_mailgun_spf_fail_is_rejected(self):
        email, _ = ingest(inbound(
            subject=f"Re: {self.case.reference}", envelope_sender="b@insurer.test",
            headers={**AUTH_PASS, "X-Mailgun-Spf": "Fail"},
        ))
        self.assertIsNone(email.case)
        self.assertIn("SPF failed", email.match_note)

    def test_forged_lower_header_with_our_authserv_id_is_ignored(self):
        email, _ = ingest(inbound(
            subject=f"Re: {self.case.reference}",
            headers={"Authentication-Results": "mx.provider.test; dmarc=none\nmx.provider.test; dmarc=pass"},
        ))
        self.assertIsNone(email.case)

    def test_auth_results_from_other_servers_are_ignored(self):
        email, _ = ingest(inbound(
            subject=f"Re: {self.case.reference}",
            headers={"Authentication-Results": "attacker.example; spf=pass; dmarc=pass"},
        ))
        self.assertIsNone(email.case)

    def test_provider_spf_header_ignored_when_provider_unknown(self):
        # Under the console backend nobody vouches for Received-SPF, so it is not trusted.
        email, _ = ingest(inbound(
            subject=f"Re: {self.case.reference}", envelope_sender="b@insurer.test", headers={"Received-SPF": "Pass"},
        ))
        self.assertIsNone(email.case)

    def test_forged_lower_auth_header_is_ignored(self):
        # Provider's own (topmost) header says fail; a sender-supplied "pass" below it must not count.
        email, _ = ingest(inbound(
            subject=f"Re: {self.case.reference}",
            headers={"Authentication-Results": "mx.provider.test; dmarc=none\nforged; dmarc=pass"},
        ))
        self.assertIsNone(email.case)

    def test_results_inside_comments_are_ignored(self):
        # Zoho style: an upstream hop's dmarc=pass carried inside the ARC comment is not our verdict.
        email, _ = ingest(inbound(
            subject=f"Re: {self.case.reference}", envelope_sender="",
            headers={"Authentication-Results": "mx.provider.test; dkim=pass; spf=none; "
                     "arc=pass (i=1 dmarc=pass fromdomain:insurer.test (nested dmarc=pass))"},
        ))
        self.assertIsNone(email.case)
        self.assertIn("no SPF/DMARC pass", email.match_note)

    def test_zoho_style_header_with_aligned_spf_passes(self):
        email, _ = ingest(inbound(
            subject=f"Re: {self.case.reference}", envelope_sender="ops@insurer.test",
            headers={"Authentication-Results": "mx.provider.test; dkim=pass; "
                     "spf=pass (provider: domain of insurer.test designates 1.2.3.4 as permitted sender) "
                     "smtp.mailfrom=ops@insurer.test; arc=pass (i=1 dmarc=pass fromdomain:insurer.test)"},
        ))
        self.assertEqual(email.case, self.case)

    def test_cc_list_and_cancellation_address_are_known_senders(self):
        insurer = self.case.cover.insurer
        insurer.known_sender_addresses = ""
        insurer.save()
        self.assertTrue(insurer.is_known_sender("OPS@insurer.test"))
        self.assertTrue(insurer.is_known_sender("cancel@insurer.test"))
        self.assertFalse(insurer.is_known_sender("claims@insurer.test"))
