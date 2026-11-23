from django.db import DatabaseError, transaction
from django.test import TestCase

from cancellations.models import AppendOnlyError, CaseEmail, CaseEvent
from cancellations.services.inbound import ingest, link_email
from cancellations.services.workflow import (
    ALLOWED_TRANSITIONS, S, TransitionError, close_case, review_inbound_email, transition,
)

from .factories import inbound, make_case, make_sent_case, make_user

C = CaseEmail.Classification


class TransitionTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_allowed_map(self):
        self.assertEqual(ALLOWED_TRANSITIONS[S.DRAFT], {S.SENT})
        self.assertEqual(ALLOWED_TRANSITIONS[S.SENT], {S.CONFIRMED, S.REJECTED, S.QUERY})
        self.assertEqual(ALLOWED_TRANSITIONS[S.CLOSED], set())

    def test_every_disallowed_transition_raises(self):
        for from_status in S.values:
            for to_status in S.values:
                if to_status in ALLOWED_TRANSITIONS[from_status]:
                    continue
                with self.subTest(frm=from_status, to=to_status):
                    case = make_case(self.user, cover=None)
                    type(case).objects.filter(pk=case.pk).update(status=from_status)
                    case.refresh_from_db()
                    with self.assertRaises(TransitionError):
                        transition(case, to_status, self.user)
                    case.refresh_from_db()
                    self.assertEqual(case.status, from_status)

    def test_transition_logs_event(self):
        case = make_sent_case(self.user)
        transition(case, S.QUERY, self.user, note="Need ID")
        ev = case.events.last()
        self.assertEqual((ev.from_status, ev.to_status, ev.actor, ev.note), (S.SENT, S.QUERY, self.user, "Need ID"))

    def test_full_lifecycle(self):
        case = make_sent_case(self.user)
        transition(case, S.QUERY, self.user)
        transition(case, S.CONFIRMED, self.user)
        close_case(case, self.user)
        case.refresh_from_db()
        self.assertEqual(case.status, S.CLOSED)
        self.assertEqual(
            list(case.events.exclude(from_status=models_F_to()).values_list("to_status", flat=True)),
            [S.SENT, S.QUERY, S.CONFIRMED, S.CLOSED],
        )


def models_F_to():
    from django.db.models import F

    return F("to_status")


class ReviewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.case = make_sent_case(self.user)
        self.reply, _ = ingest(inbound(subject=f"Re: {self.case.reference}"))

    def test_inbound_reply_never_auto_confirms(self):
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.SENT)
        self.assertEqual(self.reply.classification, C.UNREVIEWED)
        ingest(inbound(message_id="<2@insurer.test>", subject=f"Re: {self.case.reference}", text="CONFIRMED cancelled"))
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.SENT)

    def test_confirm(self):
        review_inbound_email(self.reply, C.CONFIRMATION, self.user, "Insurer confirmed")
        self.case.refresh_from_db()
        self.reply.refresh_from_db()
        self.assertEqual(self.case.status, S.CONFIRMED)
        self.assertEqual(self.reply.classification, C.CONFIRMATION)
        self.assertEqual(self.reply.reviewed_by, self.user)
        ev = self.case.events.last()
        self.assertEqual((ev.action, ev.email, ev.actor), ("MARKED_CONFIRMED", self.reply, self.user))

    def test_reject_and_query(self):
        for cls, status in [(C.REJECTION, S.REJECTED), (C.QUERY, S.QUERY)]:
            with self.subTest(cls=cls):
                case = make_sent_case(self.user, cover=None) if cls == C.QUERY else self.case
                reply, _ = ingest(inbound(message_id=f"<{cls}@insurer.test>", subject=f"Re: {case.reference}"))
                review_inbound_email(reply, cls, self.user)
                case.refresh_from_db()
                self.assertEqual(case.status, status)

    def test_query_then_confirm(self):
        review_inbound_email(self.reply, C.QUERY, self.user)
        reply2, _ = ingest(inbound(message_id="<r2@insurer.test>", subject=f"Re: {self.case.reference}"))
        review_inbound_email(reply2, C.CONFIRMATION, self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.CONFIRMED)

    def test_cannot_confirm_twice(self):
        review_inbound_email(self.reply, C.CONFIRMATION, self.user)
        reply2, _ = ingest(inbound(message_id="<r3@insurer.test>", subject=f"Re: {self.case.reference}"))
        with self.assertRaises(TransitionError):
            review_inbound_email(reply2, C.REJECTION, self.user)

    def test_info_only_keeps_status(self):
        review_inbound_email(self.reply, C.INFO, self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.SENT)
        self.assertTrue(self.case.events.filter(action="EMAIL_REVIEWED").exists())

    def test_unmatched_email_cannot_be_reviewed(self):
        stray, _ = ingest(inbound(message_id="<s@x.test>", subject="hi", text="?"))
        with self.assertRaises(TransitionError):
            review_inbound_email(stray, C.CONFIRMATION, self.user)

    def test_manual_link_of_unknown_sender_requires_check(self):
        stray, _ = ingest(inbound(message_id="<s2@x.test>", from_email="a@broker.test", from_address="a@broker.test",
                                  subject=f"Re: {self.case.reference}"))
        self.assertIsNone(stray.case)
        with self.assertRaises(ValueError):
            link_email(stray, self.case, self.user)
        link_email(stray, self.case, self.user, sender_checked=True)
        stray.refresh_from_db()
        self.assertEqual((stray.case, stray.matched_by, stray.sender_verified), (self.case, "MANUAL", True))
        self.assertTrue(self.case.events.filter(action="EMAIL_LINKED", actor=self.user).exists())


class AuditLogTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.case = make_case(self.user)
        self.event = CaseEvent.objects.create(case=self.case, actor=self.user, action="TEST")

    def test_orm_update_and_delete_blocked(self):
        self.event.note = "changed"
        with self.assertRaises(AppendOnlyError):
            self.event.save()
        with self.assertRaises(AppendOnlyError):
            self.event.delete()

    def test_database_trigger_blocks_bulk_update_and_delete(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            CaseEvent.objects.filter(pk=self.event.pk).update(note="tampered")
        with self.assertRaises(DatabaseError), transaction.atomic():
            CaseEvent.objects.filter(pk=self.event.pk).delete()
        self.assertEqual(CaseEvent.objects.get(pk=self.event.pk).note, "")
