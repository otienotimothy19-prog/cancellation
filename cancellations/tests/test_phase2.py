import csv
import datetime as dt
import io
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cancellations.models import CancellationCase, CaseEmail
from cancellations.reasons import DocType
from cancellations.services.inbound import ingest
from cancellations.services.sla import check_sla
from cancellations.services.workflow import S, review_inbound_email

from .factories import attach, inbound, make_case, make_cover, make_insurer, make_sent_case, make_user

C = CaseEmail.Classification
TOKEN = "test-token-commission"


def confirm(case, user, date=dt.date(2026, 6, 30), message_id=None):
    reply, _ = ingest(inbound(message_id=message_id or f"<c{case.pk}@insurer.test>", subject=f"Re: {case.reference}"))
    review_inbound_email(reply, C.CONFIRMATION, user, confirmed_effective_date=date)
    case.refresh_from_db()
    return case


class ExportTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.confirmed = confirm(make_sent_case(self.user), self.user)
        self.sent_only = make_sent_case(self.user)  # never confirmed: must not be exported
        self.api = reverse("cancellations:confirmed_api")

    def test_api_requires_token_or_role(self):
        self.assertEqual(self.client.get(self.api).status_code, 401)
        self.assertEqual(self.client.get(self.api, HTTP_AUTHORIZATION="Bearer wrong").status_code, 401)
        self.client.force_login(make_user("norole", role=False))
        self.assertEqual(self.client.get(self.api).status_code, 401)

    def test_api_returns_confirmed_only(self):
        r = self.client.get(self.api, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["count"], 1)
        row = data["results"][0]
        self.assertEqual(row["reference"], self.confirmed.reference)
        self.assertEqual(row["policy_number"], self.confirmed.cover.policy_number)
        self.assertEqual(row["effective_date"], "2026-06-30")
        self.assertEqual(row["requested_effective_date"], "2026-06-30")
        self.assertEqual(row["refund_amount"], "18500.00")
        self.assertEqual(row["currency"], "KES")
        self.assertEqual(row["status"], S.CONFIRMED)

    def test_api_includes_closed_and_filters_by_date(self):
        attach(self.confirmed, self.user, DocType.CERTIFICATE)
        from cancellations.services.workflow import close_case

        close_case(self.confirmed, self.user)
        today = timezone.localdate().isoformat()
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}
        self.assertEqual(self.client.get(self.api, {"confirmed_since": today}, **auth).json()["results"][0]["status"], S.CLOSED)
        self.assertEqual(self.client.get(self.api, {"confirmed_since": tomorrow}, **auth).json()["count"], 0)
        self.assertEqual(self.client.get(self.api, {"confirmed_until": today}, **auth).json()["count"], 1)
        self.assertEqual(self.client.get(self.api, {"confirmed_since": "nonsense"}, **auth).status_code, 400)

    def test_api_pagination(self):
        for i in range(3):
            confirm(make_sent_case(self.user), self.user, message_id=f"<p{i}@insurer.test>")
        auth = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}
        page1 = self.client.get(self.api, {"limit": 2}, **auth).json()
        self.assertEqual((page1["count"], len(page1["results"]), page1["next_offset"]), (4, 2, 2))
        page2 = self.client.get(self.api, {"limit": 2, "offset": 2}, **auth).json()
        self.assertIsNone(page2["next_offset"])
        refs = [r["reference"] for r in page1["results"] + page2["results"]]
        self.assertEqual(len(set(refs)), 4)
        self.assertEqual(self.client.get(self.api, {"limit": 0}, **auth).status_code, 400)

    def test_csv_export(self):
        url = reverse("cancellations:confirmed_csv")
        self.assertEqual(self.client.get(url).status_code, 302)  # login required
        self.client.force_login(self.user)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment;", r["Content-Disposition"])
        rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reference"], self.confirmed.reference)
        self.assertEqual(rows[0]["refund_amount"], "18500.00")
        self.assertEqual(rows[0]["effective_date"], "2026-06-30")

    def test_csv_neutralises_formulas(self):
        cover = self.confirmed.cover
        cover.client_name = "=HYPERLINK(\"http://x\")"
        cover.save()
        self.client.force_login(self.user)
        r = self.client.get(reverse("cancellations:confirmed_csv"))
        self.assertIn("'=HYPERLINK", r.content.decode("utf-8-sig"))


@override_settings(SLA_STALE_AFTER_REMINDER_HOURS=24, SLA_ALERT_EMAIL="")
class SlaTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.user.email = "staff@imoth.test"
        self.user.save()
        self.case = make_sent_case(self.user)  # insurer SLA 48 h
        mail.outbox.clear()
        self.sent_at = self.case.sent_at

    def at(self, hours):
        return self.sent_at + timedelta(hours=hours)

    def test_nothing_before_sla(self):
        r = check_sla(now=self.at(47))
        self.assertEqual((r.reminded, r.flagged_stale), ([], []))
        self.assertEqual(len(mail.outbox), 0)

    def test_reminder_then_stale(self):
        r = check_sla(now=self.at(49))
        self.assertEqual(r.reminded, [self.case.reference])
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["staff@imoth.test"])
        self.assertIn(self.case.reference, msg.body)
        self.case.refresh_from_db()
        self.assertIsNotNone(self.case.sla_reminder_sent_at)
        self.assertFalse(self.case.is_stale)
        self.assertTrue(self.case.events.filter(action="SLA_REMINDER").exists())

        # running again does not re-send
        check_sla(now=self.at(50))
        self.assertEqual(len(mail.outbox), 1)

        # stale only after the grace period following the reminder (reminder was at real "now")
        reminder_at = self.case.sla_reminder_sent_at
        self.assertEqual(check_sla(now=reminder_at + timedelta(hours=23)).flagged_stale, [])
        r = check_sla(now=reminder_at + timedelta(hours=24))
        self.assertEqual(r.flagged_stale, [self.case.reference])
        self.case.refresh_from_db()
        self.assertTrue(self.case.is_stale)
        self.assertEqual(self.case.status, S.SENT)  # STALE is a flag, not a status
        self.assertEqual(self.case.events.filter(action="FLAGGED_STALE").count(), 1)
        check_sla(now=reminder_at + timedelta(hours=30))
        self.assertEqual(self.case.events.filter(action="FLAGGED_STALE").count(), 1)

    def test_reply_stops_reminder_and_clears_stale(self):
        CancellationCase.objects.filter(pk=self.case.pk).update(is_stale=True, sla_reminder_sent_at=self.at(49))
        ingest(inbound(subject=f"Re: {self.case.reference}"))
        self.case.refresh_from_db()
        self.assertFalse(self.case.is_stale)
        self.assertTrue(self.case.events.filter(action="STALE_CLEARED").exists())
        CancellationCase.objects.filter(pk=self.case.pk).update(sla_reminder_sent_at=None)
        self.assertEqual(check_sla(now=self.at(200)).reminded, [])

    def test_unmatched_reply_does_not_count(self):
        ingest(inbound(subject="no reference", text="?", message_id="<u@x.test>"))
        self.assertEqual(check_sla(now=self.at(49)).reminded, [self.case.reference])

    def test_only_sent_cases(self):
        reply, _ = ingest(inbound(subject=f"Re: {self.case.reference}"))
        review_inbound_email(reply, C.QUERY, self.user)
        draft = make_case(self.user)
        self.assertEqual(check_sla(now=self.at(500)).reminded, [])
        self.assertEqual(draft.status, S.DRAFT)

    def test_per_insurer_sla_and_digest(self):
        fast = make_insurer("Fast Co", cancellation_email="c@fast.test", sla_hours=4)
        quick = make_sent_case(self.user, cover=make_cover(fast))
        mail.outbox.clear()  # drop the cancellation request itself
        r = check_sla(now=quick.sent_at + timedelta(hours=5))
        self.assertEqual(r.reminded, [quick.reference])  # 48 h case not yet due
        r = check_sla(now=self.at(49))
        self.assertEqual(r.reminded, [self.case.reference])
        self.assertEqual(len(mail.outbox), 2)

    @override_settings(SLA_ALERT_EMAIL="team@imoth.test")
    def test_fallback_recipient(self):
        self.user.email = ""
        self.user.save()
        check_sla(now=self.at(49))
        self.assertEqual(mail.outbox[0].to, ["team@imoth.test"])

    def test_no_recipient_is_reported_not_marked(self):
        self.user.email = ""
        self.user.save()
        r = check_sla(now=self.at(49))
        self.assertEqual(r.no_recipient, [self.case.reference])
        self.case.refresh_from_db()
        self.assertIsNone(self.case.sla_reminder_sent_at)

    def test_send_failure_is_retried(self):
        with mock.patch("cancellations.services.sla.EmailMessage.send", side_effect=OSError("smtp down")):
            with self.assertRaises(OSError):
                check_sla(now=self.at(49))
        self.case.refresh_from_db()
        self.assertIsNone(self.case.sla_reminder_sent_at)
        self.assertEqual(check_sla(now=self.at(49)).reminded, [self.case.reference])


class DashboardTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_counts(self):
        draft = make_case(self.user)  # draft -> open
        sent = make_sent_case(self.user)  # open
        stale = make_sent_case(self.user)
        CancellationCase.objects.filter(pk=stale.pk).update(is_stale=True)
        confirm(make_sent_case(self.user), self.user)  # confirmed this month
        ingest(inbound(subject="??", text="??", message_id="<x@x.test>"))  # unmatched
        r = self.client.get(reverse("cancellations:dashboard"))
        self.assertEqual(r.context["counts"], {"open": 3, "stale": 1, "unmatched": 1, "confirmed_month": 1})
        self.assertContains(r, "18500.00")
        self.assertContains(r, draft.reference)  # listed under drafts
        self.assertContains(r, stale.reference)  # listed under stale
        self.assertNotContains(r, sent.reference)  # plain SENT cases need no attention

    def test_search_by_reference_jumps_to_case(self):
        case = make_sent_case(self.user)
        r = self.client.get(reverse("cancellations:dashboard"), {"q": case.reference.lower()})
        self.assertRedirects(r, case.get_absolute_url())
        r = self.client.get(reverse("cancellations:dashboard"), {"q": case.cover.policy_number})
        self.assertRedirects(r, case.get_absolute_url())

    def test_search_with_many_results_goes_to_list(self):
        make_sent_case(self.user)
        make_sent_case(self.user)
        r = self.client.get(reverse("cancellations:dashboard"), {"q": "POL-"})
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("cancellations:case_list"), r["Location"])

    def test_case_list_views(self):
        stale = make_sent_case(self.user)
        CancellationCase.objects.filter(pk=stale.pk).update(is_stale=True)
        other = make_sent_case(self.user)
        r = self.client.get(reverse("cancellations:case_list"), {"view": "stale"})
        self.assertContains(r, stale.reference)
        self.assertNotContains(r, other.reference)
