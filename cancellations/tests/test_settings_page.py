"""Settings: administrators give or remove access, and see how the app is connected."""

from django.test import TestCase, override_settings
from django.urls import reverse

from cancellations.permissions import has_cancellations_role

from .factories import make_user


class SettingsPageTests(TestCase):
    def setUp(self):
        self.admin = make_user("boss")
        self.admin.is_superuser = True
        self.admin.email = "boss@imoth.test"
        self.admin.save()
        self.newcomer = make_user("new@imoth.test", role=False)
        self.newcomer.email = "new@imoth.test"
        self.newcomer.set_unusable_password()  # created by a portal sign-in
        self.newcomer.save()
        self.url = reverse("cancellations:settings")

    def access(self, person, action):
        return self.client.post(reverse("cancellations:settings_access", args=[person.pk]), {"action": action},
                                follow=True)

    def test_only_administrators_see_and_open_settings(self):
        staff = make_user("staff")
        self.client.force_login(staff)
        self.assertNotContains(self.client.get(reverse("cancellations:dashboard")), 'href="/settings/"')
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(reverse("cancellations:settings_access", args=[self.newcomer.pk]),
                                          {"action": "grant"}).status_code, 403)
        self.assertFalse(has_cancellations_role(self.newcomer))
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("cancellations:dashboard")), 'href="/settings/"')
        page = self.client.get(self.url)
        self.assertContains(page, "new@imoth.test")
        self.assertContains(page, "No access")

    def test_give_and_remove_access(self):
        self.client.force_login(self.admin)
        r = self.access(self.newcomer, "grant")
        self.assertContains(r, "new@imoth.test can now use Cancellations")
        self.assertTrue(has_cancellations_role(self.newcomer))
        r = self.access(self.newcomer, "revoke")
        self.assertContains(r, "no longer has access")
        self.assertFalse(has_cancellations_role(self.newcomer))

    def test_cannot_remove_own_access_or_grant_switched_off_account(self):
        self.client.force_login(self.admin)
        self.assertContains(self.access(self.admin, "revoke"), "cannot remove your own access")
        self.assertTrue(has_cancellations_role(self.admin))
        self.newcomer.is_active = False
        self.newcomer.save()
        self.assertContains(self.access(self.newcomer, "grant"), "switched off")
        self.assertFalse(self.newcomer.groups.exists())

    def test_get_does_not_change_access(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse("cancellations:settings_access", args=[self.newcomer.pk]), {"action": "grant"})
        self.assertEqual(r.status_code, 405)

    def test_search(self):
        self.client.force_login(self.admin)
        page = self.client.get(self.url, {"q": "new@"})
        self.assertContains(page, "new@imoth.test")
        self.assertNotContains(page, "boss@imoth.test")

    @override_settings(PRODUCTION_PORTAL_URL="https://portal.test", PRODUCTION_PORTAL_TOKEN="t" * 40,
                       MAILBOX_PASSWORD="secret-pw-123", PORTAL_SSO_LOGIN_URL="https://portal.test/cancellations/open")
    def test_connections_show_status_never_secrets(self):
        self.client.force_login(self.admin)
        page = self.client.get(self.url)
        self.assertContains(page, "Production Portal search")
        self.assertContains(page, "portal.test")
        self.assertContains(page, '<td data-label="Signs in with">Production Portal</td>')  # portal-made account
        self.assertContains(page, '<td data-label="Signs in with">Password</td>')  # administrators: password only
        self.assertNotContains(page, "secret-pw-123")
        self.assertNotContains(page, "t" * 40)
