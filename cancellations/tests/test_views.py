from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from cancellations.models import CancellationCase, Cover
from cancellations.services.documents import validate_file
from cancellations.services.workflow import S

from .factories import COVER_END, COVER_START, PDF, make_case, make_insurer, make_user, pdf_upload


class AccessTests(TestCase):
    def test_anonymous_redirected_to_login(self):
        r = self.client.get(reverse("cancellations:case_list"))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("login"), r["Location"])

    def test_user_without_role_forbidden_even_superuser(self):
        user = make_user("admin", role=False)
        user.is_superuser = user.is_staff = True
        user.save()
        self.client.force_login(user)
        case = make_case(user)
        for url in [reverse("cancellations:case_list"), reverse("cancellations:case_create"),
                    reverse("cancellations:unmatched_list")]:
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(self.client.post(reverse("cancellations:case_send", args=[case.pk])).status_code, 403)

    def test_role_group_created_by_migration(self):
        from django.contrib.auth.models import Group

        self.assertTrue(Group.objects.filter(name="cancellations", permissions__codename="manage_cancellations").exists())


class CreateAndSendFlowTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.insurer = make_insurer()

    def post_create(self, **overrides):
        data = {
            "cover-client_name": "Peter Otieno", "cover-certificate_number": "CMTR-777", "cover-policy_number": "MTR-777", "cover-insurer": self.insurer.pk,
            "cover-asset_reference": "KCB 777Z", "cover-premium": "48000", "cover-start_date": COVER_START,
            "cover-end_date": COVER_END, "case-reason_code": "CLIENT_REQUEST", "case-effective_date": "2026-07-01",
            "doc-doc_type": "SIGNED_REQUEST", "doc-source": "client", "doc-file": pdf_upload(),
        }
        data.update(overrides)
        return self.client.post(reverse("cancellations:case_create"), data)

    def test_create_draft_then_send(self):
        r = self.post_create()
        case = CancellationCase.objects.get()
        self.assertRedirects(r, case.get_absolute_url())
        self.assertEqual(case.status, S.DRAFT)
        self.assertEqual(case.documents.count(), 1)
        self.assertEqual(Cover.objects.get().policy_number, "MTR-777")
        self.assertEqual(list(case.events.values_list("action", flat=True)), ["CREATED", "DOCUMENT_ADDED"])

        self.assertContains(self.client.get(case.get_absolute_url()), case.reference)
        self.client.post(reverse("cancellations:case_send", args=[case.pk]))
        case.refresh_from_db()
        self.assertEqual(case.status, S.SENT)
        self.assertEqual(len(mail.outbox), 1)

    def test_draft_saved_without_document_but_send_blocked(self):
        self.post_create(**{"doc-file": "", "doc-doc_type": ""})
        case = CancellationCase.objects.get()
        r = self.client.post(reverse("cancellations:case_send", args=[case.pk]), follow=True)
        self.assertContains(r, "Required document missing")
        case.refresh_from_db()
        self.assertEqual(case.status, S.DRAFT)
        self.assertEqual(len(mail.outbox), 0)

    def test_effective_date_outside_cover_rejected(self):
        r = self.post_create(**{"cover-start_date": "2020-01-01", "cover-end_date": "2020-12-31"})  # today is outside it
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "within the cover period")
        self.assertFalse(CancellationCase.objects.exists())

    def test_second_open_case_for_same_cover_rejected(self):
        self.post_create()
        cover = Cover.objects.get()
        r = self.client.post(reverse("cancellations:case_create"), {
            "cover-existing_cover": cover.pk, "case-reason_code": "CLIENT_REQUEST", "case-effective_date": "2026-07-01",
            "doc-source": "client",
        })
        self.assertContains(r, "already has an open case")
        self.assertEqual(CancellationCase.objects.count(), 1)

    def test_upload_rejects_bad_type(self):
        r = self.post_create(**{"doc-file": SimpleUploadedFile("evil.exe", b"MZ\x90\x00", "application/octet-stream")})
        self.assertContains(r, "not allowed")
        self.assertFalse(CancellationCase.objects.exists())

    def test_upload_rejects_disguised_file(self):
        r = self.post_create(**{"doc-file": SimpleUploadedFile("fake.pdf", b"<html>not a pdf</html>", "application/pdf")})
        self.assertContains(r, "do not look like")

    @override_settings(MAX_UPLOAD_MB=1)
    def test_upload_rejects_oversized(self):
        big = PDF + b"0" * (1024 * 1024 + 1)
        r = self.post_create(**{"doc-file": SimpleUploadedFile("big.pdf", big, "application/pdf")})
        self.assertContains(r, "larger than 1 MB")

    def test_document_download_requires_role(self):
        self.post_create()
        doc = CancellationCase.objects.get().documents.get()
        url = reverse("cancellations:document_download", args=[doc.pk])
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), PDF)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)


class FileValidationTests(TestCase):
    def test_allowed_signatures(self):
        self.assertEqual(validate_file("a.PDF", PDF[:16], 100), "application/pdf")
        self.assertEqual(validate_file("a.png", b"\x89PNG\r\n\x1a\n....", 100), "image/png")


class ReplyPagesTests(TestCase):
    def setUp(self):
        from .factories import make_sent_case

        self.user = make_user()
        self.client.force_login(self.user)
        self.case = make_sent_case(self.user)

    def test_review_reply_from_case_page(self):
        from cancellations.services.inbound import ingest

        from .factories import inbound

        reply, _ = ingest(inbound(subject=f"Re: {self.case.reference}", text="Cancelled wef 30/06"))
        page = self.client.get(self.case.get_absolute_url())
        self.assertContains(page, "Review this reply")
        self.assertContains(page, "Cancelled wef 30/06")
        self.client.post(reverse("cancellations:email_review", args=[reply.pk]), {"classification": "CONFIRMATION", "confirmed_effective_date": "2026-06-30"})
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.CONFIRMED)

    def test_unmatched_inbox_link(self):
        from cancellations.services.inbound import ingest

        from .factories import inbound

        stray, _ = ingest(inbound(subject="Policy query", text="no reference here"))
        self.assertContains(self.client.get(reverse("cancellations:unmatched_list")), "Policy query")
        self.assertContains(self.client.get(reverse("cancellations:case_list")), "1 unmatched")
        url = reverse("cancellations:unmatched_detail", args=[stray.pk])
        self.assertContains(self.client.get(url), "Link to a case")
        r = self.client.post(url, {"case": self.case.pk})
        self.assertRedirects(r, self.case.get_absolute_url())
        stray.refresh_from_db()
        self.assertEqual(stray.case, self.case)

    def test_search_by_reference_and_policy(self):
        list_url = reverse("cancellations:case_list")
        self.assertContains(self.client.get(list_url, {"q": self.case.reference}), self.case.reference)
        self.assertContains(self.client.get(list_url, {"q": "pol-1001"}), self.case.reference)
        self.assertNotContains(self.client.get(list_url, {"q": "NOPE-1"}), self.case.reference)
