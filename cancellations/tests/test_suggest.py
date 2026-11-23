"""Reply wording suggests an outcome (pre-selected only; staff still record the decision)."""

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from cancellations.services.inbound import ingest
from cancellations.services.suggest import CONFIRMATION, QUERY, REJECTION, new_text, suggest
from cancellations.services.workflow import S

from .factories import inbound, make_sent_case, make_user

OUR_REQUEST = """

On Mon, 23 Nov 2026 at 21:14, IMOTH Cancellations <cancellation@imoth.co.ke> wrote:
> Please cancel the following cover:
> Reference: IMOTH-CXL-000003
> Please reply to confirm the cancellation, the confirmed effective date and any refund.
"""


class SuggestTests(SimpleTestCase):
    def test_confirmations(self):
        for text in ["done", "Done.\n\nRegards,\nRyan Mwaura | Risk Analyst",
                     "The policy has been cancelled.", "Cancellation effected as requested",
                     "Noted and actioned", "Certificate C35561500 is now cancelled"]:
            with self.subTest(text=text):
                self.assertEqual(suggest(text + OUR_REQUEST), CONFIRMATION)

    def test_not_confirmations(self):
        cases = {
            "Not yet done, will revert": None,
            "This will be cancelled once we receive the signed form": None,
            "Kindly provide the signed cancellation form": QUERY,
            "Is the vehicle sold?": QUERY,
            "We are unable to cancel this policy as a claim is open": REJECTION,
            "Request declined": REJECTION,
            "Cancellation pending approval": None,
            "": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(suggest(text + OUR_REQUEST), expected)

    def test_quoted_request_is_ignored(self):
        # Only our quoted request mentions cancelling: no suggestion.
        self.assertEqual(new_text("Thanks" + OUR_REQUEST).strip(), "")
        self.assertIsNone(suggest("Received, we will revert." + OUR_REQUEST))

    def test_disclaimer_after_sign_off_is_ignored(self):
        body = "Done\n\nKind regards\nJane\nDisclaimer: this email cannot be relied upon. Questions? Call us."
        self.assertEqual(suggest(body), CONFIRMATION)


class SuggestionOnCasePageTests(TestCase):
    def test_confirmation_is_preselected_but_not_recorded(self):
        user = make_user()
        self.client.force_login(user)
        case = make_sent_case(user)
        ingest(inbound(subject=f"Re: {case.reference}", text="done" + OUR_REQUEST))
        page = self.client.get(case.get_absolute_url())
        self.assertContains(page, "looks like <strong>a confirmation</strong>")
        self.assertRegex(page.content.decode(), r'value="CONFIRMATION"[^>]*checked')
        case.refresh_from_db()
        self.assertEqual(case.status, S.SENT)  # nothing recorded until staff press the button
        self.assertContains(self.client.get(reverse("cancellations:dashboard")), case.reference)  # still to review
