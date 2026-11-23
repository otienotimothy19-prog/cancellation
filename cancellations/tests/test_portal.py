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

from .factories import make_cover, make_insurer, make_user, pdf_upload

PORTAL = {"PRODUCTION_PORTAL_URL": "https://portal.test", "PRODUCTION_PORTAL_TOKEN": "t" * 40}

ITEM = {
    "ref": "IMR-00042", "status": "Approved", "insuredName": "Peter Otieno", "phone": "0712***678", "email": "p***@x.test",
    "vehicleRegistration": "KDA 123A", "chassisNumber": "XYZ", "insurer": "Acme Insurance Ltd",
    "policyNumber": "MTR-777", "certificateNumber": "C1234567", "coverType": "Comprehensive",
    "startDate": "2026-01-01", "expiryDate": "2026-12-31T00:00:00Z", "cancelled": False,
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
        self.assertEqual((item["start_date"], item["end_date"]), ("2026-01-01", "2026-12-31"))
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
            "cover-start_date": "2026-01-01", "cover-end_date": "2026-12-31",  # dates without premium: allowed
            "case-reason_code": "CLIENT_REQUEST", "case-effective_date": "2026-07-01",
            "doc-doc_type": "SIGNED_REQUEST", "doc-source": "client", "doc-file": pdf_upload(), "action": "draft",
        })
        case = CancellationCase.objects.get()
        self.assertEqual((case.cover.certificate_number, case.cover.portal_ref), ("C1234567", "IMR-00042"))
        self.assertIsNone(case.refund_amount)
        self.assertIn("Certificate no.: C1234567", render_email(case)[1])
        self.assertContains(self.client.get(case.get_absolute_url()), "IMR-00042")


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
