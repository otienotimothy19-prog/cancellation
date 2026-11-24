"""One sign-in: the Production Portal posts a signed, single-use pass to /sso/portal/."""

import secrets
import time
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cancellations.models import SsoPass
from cancellations.services import sso

from .factories import make_user

SECRET = "k" * 40
ON = {"CANCELLATIONS_SSO_SECRET": SECRET, "PORTAL_SSO_ENABLED": True,
      "PORTAL_SSO_LOGIN_URL": "https://portal.test/cancellations/open"}


def claims(**kw):
    now = int(time.time())
    return {"iss": sso.ISSUER, "aud": sso.AUDIENCE, "sub": "staff@imoth.test", "name": "Jane Wanjiru",
            "iat": now, "exp": now + 60, "jti": secrets.token_urlsafe(18), **kw}


@override_settings(**ON)
class PortalSignInTests(TestCase):
    url = reverse("portal_sign_in")

    def setUp(self):
        self.user = make_user("jane")
        self.user.email = "Staff@IMOTH.test"  # matched without regard to case
        self.user.save()

    def post(self, token):
        return self.client.post(self.url, {"token": token})

    def test_signs_in_and_opens_dashboard(self):
        r = self.post(sso.make_token(claims()))
        self.assertRedirects(r, reverse("cancellations:dashboard"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        self.assertContains(self.client.get(reverse("cancellations:dashboard")), "Signed in from the Production Portal")

    def test_pass_works_once(self):
        token = sso.make_token(claims())
        self.post(token)
        self.client.logout()
        r = self.post(token)
        self.assertContains(r, "already been used", status_code=400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_refuses_forged_expired_and_foreign_passes(self):
        good = sso.make_token(claims())
        body, sig = good.split(".")
        cases = {
            "tampered signature": f"{body}.{sig[:-2]}AA",
            "other secret": None,
            "expired": sso.make_token(claims(iat=int(time.time()) - 200, exp=int(time.time()) - 100)),
            "from the future": sso.make_token(claims(iat=int(time.time()) + 600, exp=int(time.time()) + 660)),
            "too long-lived": sso.make_token(claims(exp=int(time.time()) + 3600)),
            "other audience": sso.make_token(claims(aud="imoth-underwriting")),
            "other issuer": sso.make_token(claims(iss="someone")),
            "not an email": sso.make_token(claims(sub="jane")),
            "no id": sso.make_token(claims(jti="")),
            "empty": "",
            "garbage": "abc",
        }
        with self.settings(CANCELLATIONS_SSO_SECRET="z" * 40):
            cases["other secret"] = sso.make_token(claims())
        for name, token in cases.items():
            with self.subTest(name):
                r = self.post(token)
                self.assertEqual(r.status_code, 400)
                self.assertNotIn("_auth_user_id", self.client.session)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url, {"token": sso.make_token(claims())}).status_code, 405)

    def test_unknown_person_gets_an_account_without_access(self):
        r = self.post(sso.make_token(claims(sub="new.person@imoth.test", name="New Person")))
        self.assertContains(r, "does not have access to Cancellations yet", status_code=403)
        self.assertNotIn("_auth_user_id", self.client.session)
        created = get_user_model().objects.get(email="new.person@imoth.test")
        self.assertEqual((created.first_name, created.last_name), ("New", "Person"))
        self.assertFalse(created.has_usable_password())
        self.assertFalse(created.groups.exists())

    def test_no_role_inactive_and_superuser_are_refused(self):
        self.user.groups.clear()
        self.assertContains(self.post(sso.make_token(claims())), "does not have access", status_code=403)
        self.user.is_active = False
        self.user.save()
        self.assertContains(self.post(sso.make_token(claims())), "switched off", status_code=403)
        admin = make_user("boss")
        admin.email, admin.is_superuser = "boss@imoth.test", True
        admin.save()
        r = self.post(sso.make_token(claims(sub="boss@imoth.test")))
        self.assertContains(r, "Administrator accounts sign in here with their own password", status_code=403)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_duplicate_email_is_refused(self):
        other = make_user("jane2")
        other.email = "staff@imoth.test"
        other.save()
        self.assertContains(self.post(sso.make_token(claims())), "More than one", status_code=403)

    def test_used_passes_are_cleared_after_expiry(self):
        SsoPass.objects.create(jti="x" * 20, email="a@b.test", expires_at=timezone.now() - timedelta(minutes=5))
        self.post(sso.make_token(claims()))
        self.assertEqual(SsoPass.objects.count(), 1)

    def test_login_page_offers_portal_sign_in(self):
        page = self.client.get(reverse("login"))
        self.assertContains(page, "Sign in with the Production Portal")
        self.assertContains(page, 'href="https://portal.test/cancellations/open"')


class PortalSignInOffTests(TestCase):
    def test_off_without_secret(self):
        r = self.client.post(reverse("portal_sign_in"), {"token": "a.b"})
        self.assertContains(r, "not switched on", status_code=400)
        self.assertNotContains(self.client.get(reverse("login")), "Sign in with the Production Portal")
