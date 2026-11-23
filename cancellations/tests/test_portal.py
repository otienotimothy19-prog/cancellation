"""Production Portal lookup: read-only search that fills the New Cancellation form."""

import io
import json
import urllib.error
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from cancellations.models import CancellationCase
from cancellations.services import portal
from cancellations.services.sending import render_email

from .factories import COVER_END, COVER_START, make_cover, make_insurer, make_user, pdf_upload

PORTAL = {"PRODUCTION_PORTAL_URL": "https://portal.test", "PRODUCTION_PORTAL_TOKEN": "t" * 40}

ITEM = {
    "ref": "IMR-00042", "status": "Approved", "insuredName": "Peter Otieno", "phone": "0712***678", "email": "p***@x.test",
    "vehicleRegistration": "KDA 123A", "chassisNumber": "XYZ", "insurer": "Acme Insurance Ltd",
    "policyNumber": "MTR-777", "certificateNumber": "C1234567", "coverType": "Comprehensive",
    "startDate": "2026-01-01", "expiryDate": "2099-12-31T00:00:00Z", "cancelled": False,
}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def reply(items):
    return FakeResponse(json.dumps({"items": items}).encode())


@override_settings(**PORTAL)
class PortalSearchTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.user.email = "staff@imoth.test"
        self.user.save()
        self.client.force_login(self.user)
        self.insurer = make_insurer("Acme Insurance")
        self.url = reverse("cancellations:portal_search")

    def get(self, by="registration", q="KDA123A", items=(ITEM,)):
        with mock.patch("cancellations.services.portal.urllib.request.urlopen", return_value=reply(list(items))) as m:
            r = self.client.get(self.url, {"by": by, "q": q})
        return r.json(), m

    def test_search_maps_record_and_sends_token_and_staff(self):
        data, m = self.get()
        self.assertTrue(data["ok"])
        item = data["items"][0]
        self.assertEqual(item["policy_number"], "MTR-777")
        self.assertEqual(item["certificate_number"], "C1234567")
        self.assertEqual(item["vehicle_reg"], "KDA 123A")
        self.assertEqual(item["insurer_id"], self.insurer.pk)  # "Acme Insurance Ltd" -> Acme Insurance
        self.assertEqual((item["start_date"], item["end_date"]), ("2026-01-01", "2099-12-31"))
        self.assertNotIn("phone", item)  # contact details never pass through
        request = m.call_args.args[0]
        self.assertEqual(request.full_url, "https://portal.test/api/underwriting/v1/records?by=registration&q=KDA123A")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + "t" * 40)
        self.assertEqual(request.get_header("X-imoth-staff"), "staff@imoth.test")

    def test_unknown_insurer_and_existing_cover(self):
        data, _ = self.get(items=[{**ITEM, "insurer": "Nobody Assurance"}])
        self.assertIsNone(data["items"][0]["insurer_id"])
        cover = make_cover(self.insurer, policy_number="mtr-777")
        data, _ = self.get()
        self.assertEqual(data["items"][0]["existing_cover_id"], cover.pk)
        self.assertTrue(data["items"][0]["existing_cover_active"])

    def test_validation_happens_before_calling_portal(self):
        data, m = self.get(q="ab")
        self.assertFalse(data["ok"])
        m.assert_not_called()
        data, m = self.get(by="phone")
        self.assertFalse(data["ok"])
        m.assert_not_called()

    def test_portal_errors_become_messages(self):
        err = urllib.error.HTTPError("https://portal.test", 401, "no", {}, io.BytesIO(b"{}"))
        with mock.patch("cancellations.services.portal.urllib.request.urlopen", side_effect=err):
            data = self.client.get(self.url, {"by": "policy", "q": "MTR-777"}).json()
        self.assertFalse(data["ok"])
        self.assertIn("token", data["error"])
        with mock.patch("cancellations.services.portal.urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            data = self.client.get(self.url, {"by": "policy", "q": "MTR-777"}).json()
        self.assertIn("Could not reach", data["error"])

    def test_role_required(self):
        self.client.force_login(make_user("norole", role=False))
        self.assertEqual(self.client.get(self.url, {"by": "policy", "q": "MTR-777"}).status_code, 403)

    def test_form_shows_panel_and_saves_certificate_fields(self):
        self.assertContains(self.client.get(reverse("cancellations:case_create")), "Find the certificate in the Production Portal")
        self.client.post(reverse("cancellations:case_create"), {
            "cover-client_name": "Peter Otieno", "cover-policy_number": "MTR-777", "cover-insurer": self.insurer.pk,
            "cover-asset_reference": "KDA 123A", "cover-certificate_number": "C1234567", "cover-portal_ref": "IMR-00042",
            "cover-start_date": COVER_START, "cover-end_date": COVER_END,  # dates without premium: allowed
            "case-reason_code": "CLIENT_REQUEST", "case-effective_date": "2026-07-01",
            "doc-doc_type": "SIGNED_REQUEST", "doc-source": "client", "doc-file": pdf_upload(), "action": "draft",
        })
        case = CancellationCase.objects.get()
        self.assertEqual((case.cover.certificate_number, case.cover.portal_ref), ("C1234567", "IMR-00042"))
        self.assertIsNone(case.refund_amount)
        self.assertIn("Certificate no.: C1234567", render_email(case)[1])
        self.assertContains(self.client.get(case.get_absolute_url()), "IMR-00042")


    def test_only_active_covers_are_listed(self):
        from cancellations.models import Cover

        items = [
            {**ITEM, "ref": "IMR-1"},                                              # active
            {**ITEM, "ref": "IMR-2", "expiryDate": "2020-01-31"},                  # expired
            {**ITEM, "ref": "IMR-3", "cancelled": True, "status": "Cancelled"},    # cancelled in the portal
            {**ITEM, "ref": "IMR-4", "policyNumber": "DONE-1"},                     # already cancelled here
            {**ITEM, "ref": "IMR-5", "expiryDate": None},                          # no expiry recorded: kept
        ]
        make_cover(self.insurer, policy_number="DONE-1", status=Cover.Status.CANCELLED)
        data, _ = self.get(items=items)
        self.assertEqual([i["ref"] for i in data["items"]], ["IMR-1", "IMR-5"])
        self.assertEqual(data["hidden"], 3)


class PortalDisabledTests(TestCase):
    def test_hidden_when_not_configured(self):
        user = make_user()
        self.client.force_login(user)
        self.assertNotContains(self.client.get(reverse("cancellations:case_create")), 'id="portal-search"')
        data = self.client.get(reverse("cancellations:portal_search"), {"by": "policy", "q": "MTR-777"}).json()
        self.assertFalse(data["ok"])


class InsurerMatchTests(TestCase):
    def test_matching(self):
        a, b = make_insurer("Monarch Insurance"), make_insurer("Jubilee Allianz")
        insurers = [a, b]
        self.assertEqual(portal.match_insurer("MONARCH INSURANCE COMPANY LIMITED", insurers), a)
        self.assertEqual(portal.match_insurer("Jubilee Allianz Ltd", insurers), b)
        self.assertIsNone(portal.match_insurer("", insurers))
        self.assertIsNone(portal.match_insurer("Insurance", [a, make_insurer("Kenya Insurance")]))  # ambiguous


@override_settings(**PORTAL)
class PortalCancellationCheckTests(TestCase):
    """After confirmation the worker asks the portal whether it also shows the certificate cancelled."""

    def setUp(self):
        from cancellations.services.workflow import review_inbound_email
        from cancellations.services.inbound import ingest
        from .factories import inbound, make_sent_case

        self.user = make_user()
        self.insurer = make_insurer("Acme Insurance")
        self.case = make_sent_case(self.user, cover=make_cover(self.insurer, certificate_number="C35561500"))
        reply, _ = ingest(inbound(subject=f"Re: {self.case.reference}"))
        review_inbound_email(reply, "CONFIRMATION", self.user, confirmed_effective_date=self.case.effective_date)
        self.case.refresh_from_db()

    def run_check(self, items):
        with mock.patch("cancellations.services.portal.urllib.request.urlopen", return_value=reply(items)) as m:
            result = portal.check_due()
        self.case.refresh_from_db()
        return result, m

    def test_cancelled_in_portal(self):
        result, m = self.run_check([{**ITEM, "certificateNumber": "C35561500", "cancelled": True, "status": "Cancelled"}])
        self.assertEqual(result, {"CANCELLED": 1})
        self.assertEqual(self.case.portal_status, "CANCELLED")
        self.assertIn("by=certificate&q=C35561500", m.call_args.args[0].full_url)
        self.assertIn("cancelled", self.case.events.get(action="PORTAL_CHECK").note)
        self.assertEqual(list(portal.cases_due_for_check()), [])  # settled: not asked again

    def test_still_active_shows_on_dashboard_and_is_rechecked(self):
        self.run_check([{**ITEM, "certificateNumber": "C35561500"}])
        self.assertEqual(self.case.portal_status, "ACTIVE")
        self.client.force_login(self.user)
        dash = self.client.get(reverse("cancellations:dashboard"))
        self.assertContains(dash, "still active in the Production Portal")
        from datetime import timedelta
        from django.utils import timezone
        self.assertEqual(list(portal.cases_due_for_check()), [])
        self.assertEqual(list(portal.cases_due_for_check(timezone.now() + timedelta(hours=2))), [self.case])

    def test_not_in_portal_is_neutral(self):
        # Some requests are for covers the portal does not have: recorded, not flagged.
        self.run_check([])
        self.assertEqual(self.case.portal_status, "NOT_FOUND")
        self.assertEqual(self.case.status, "CLOSED")
        self.client.force_login(self.user)
        dash = self.client.get(reverse("cancellations:dashboard"))
        self.assertEqual(dash.context["follow_up_count"], 0)  # not flagged for follow-up
        page = self.client.get(self.case.get_absolute_url())
        self.assertContains(page, "Not in the portal")
        # Only one event however often it is checked
        self.run_check([])
        self.assertEqual(self.case.events.filter(action="PORTAL_CHECK").count(), 1)

    def test_other_certificates_in_results_do_not_count(self):
        self.run_check([{**ITEM, "certificateNumber": "C99999999", "cancelled": True}])
        self.assertEqual(self.case.portal_status, "NOT_FOUND")

    def test_check_now_button_and_portal_errors(self):
        self.client.force_login(self.user)
        url = reverse("cancellations:case_portal_check", args=[self.case.pk])
        with mock.patch("cancellations.services.portal.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("down")):
            r = self.client.post(url, follow=True)
        self.assertContains(r, "Could not reach")
        self.case.refresh_from_db()
        self.assertEqual(self.case.portal_status, "")  # nothing recorded on error
        with mock.patch("cancellations.services.portal.urllib.request.urlopen",
                        return_value=reply([{**ITEM, "certificateNumber": "C35561500", "cancelled": True}])):
            r = self.client.post(url, follow=True)
        self.assertContains(r, "Cancelled in the Production Portal")

    def test_no_number(self):
        from cancellations.models import Cover
        Cover.objects.filter(pk=self.case.cover_id).update(certificate_number="", policy_number="")
        result, m = self.run_check([])
        self.assertEqual(self.case.portal_status, "NO_NUMBER")
        m.assert_not_called()
