from django.test import TestCase
from django.urls import reverse

from cancellations.models import Insurer

from .factories import make_cover, make_insurer, make_sent_case, make_user

VALID = {
    "name": "Monarch Insurance",
    "cancellation_email": "Cancellations@Monarch.test",
    "cc_list": "ops@monarch.test; claims@monarch.test",
    "known_sender_addresses": "@monarch.test",
    "sla_hours": "48",
    "requires_signed_form": "on",
    "active": "on",
    "email_template": "",
}


class InsurerPageTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_role_required(self):
        self.client.force_login(make_user("norole", role=False))
        for url in (reverse("cancellations:insurer_list"), reverse("cancellations:insurer_create")):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertFalse(Insurer.objects.exists())

    def test_list_shows_insurers_and_sidebar_link(self):
        insurer = make_insurer()
        make_sent_case(self.user, cover=make_cover(insurer))
        r = self.client.get(reverse("cancellations:insurer_list"))
        self.assertContains(r, insurer.name)
        self.assertContains(r, insurer.cancellation_email)
        row = r.context["insurers"][0]
        self.assertEqual((row.cover_count, row.open_count), (1, 1))
        self.assertContains(self.client.get(reverse("cancellations:dashboard")),
                            f'href="{reverse("cancellations:insurer_list")}"')

    def test_create(self):
        r = self.client.post(reverse("cancellations:insurer_create"), VALID)
        self.assertRedirects(r, reverse("cancellations:insurer_list"))
        insurer = Insurer.objects.get()
        self.assertEqual(insurer.cancellation_email, "cancellations@monarch.test")
        self.assertEqual(insurer.cc_list, "ops@monarch.test, claims@monarch.test")
        self.assertEqual(insurer.sla_hours, 48)
        self.assertTrue(insurer.requires_signed_form)
        self.assertTrue(insurer.is_known_sender("anyone@monarch.test"))

    def test_edit_and_deactivate(self):
        insurer = make_insurer()
        data = {**VALID, "name": insurer.name}
        del data["active"]
        r = self.client.post(reverse("cancellations:insurer_edit", args=[insurer.pk]), data)
        self.assertRedirects(r, reverse("cancellations:insurer_list"))
        insurer.refresh_from_db()
        self.assertFalse(insurer.active)
        self.assertEqual(Insurer.objects.count(), 1)

    def test_validation(self):
        make_insurer("Acme Insurance")
        bad = {**VALID, "name": "acme insurance", "cc_list": "ops@x.test, not-an-address",
               "known_sender_addresses": "@good.test, @", "email_template": "Dear {% if %}"}
        r = self.client.post(reverse("cancellations:insurer_create"), bad)
        self.assertEqual(r.status_code, 200)
        errors = r.context["form"].errors
        self.assertEqual(set(errors), {"name", "cc_list", "known_sender_addresses", "email_template"})
        self.assertIn("not-an-address", errors["cc_list"][0])
        self.assertEqual(Insurer.objects.count(), 1)

    def test_custom_template_is_used_when_sending(self):
        r = self.client.post(reverse("cancellations:insurer_create"),
                             {**VALID, "requires_signed_form": "", "email_template": "Cancel {{ policy_number }} ref {{ reference }}"})
        self.assertEqual(r.status_code, 302)
        from cancellations.services.sending import render_email

        case = make_sent_case(self.user, cover=make_cover(Insurer.objects.get()))
        _subject, body = render_email(case)
        self.assertEqual(body, f"Cancel {case.cover.policy_number} ref {case.reference}")
