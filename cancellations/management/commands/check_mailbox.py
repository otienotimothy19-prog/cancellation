from anymail.inbound import AnymailInboundMessage
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from cancellations.services.inbound import from_anymail, sender_address, sender_authentication
from cancellations.services.mailbox import MailboxError, _fetch_raw, _ok, _select, connect


class Command(BaseCommand):
    help = (
        "Check the IMAP connection and show how the newest messages would be authenticated. "
        "Read-only: nothing is stored and nothing is marked as read."
    )

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=3)

    def handle(self, count=3, **options):
        try:
            client = connect()
        except Exception as exc:
            raise CommandError(f"Login to {settings.IMAP_HOST} as {settings.MAILBOX_USER} failed: {exc}")
        try:
            uidvalidity = _select(client, settings.IMAP_FOLDER)
            data = _ok(*client.uid("SEARCH", None, "ALL"), "SEARCH")
            uids = [int(u) for u in (data[0] or b"").split()][-count:]
            self.stdout.write(self.style.SUCCESS(
                f"Connected to {settings.IMAP_HOST} as {settings.MAILBOX_USER}; "
                f"{settings.IMAP_FOLDER} UIDVALIDITY={uidvalidity}, checking last {len(uids)} message(s)"))
            for uid in reversed(uids):
                msg = from_anymail(AnymailInboundMessage.parse_raw_mime_bytes(_fetch_raw(client, uid)), from_mailbox=True)
                ok, reason = sender_authentication(msg)
                self.stdout.write(f"\nUID {uid}: {msg.subject!r} from {sender_address(msg)}")
                for name in ("Authentication-Results", "ARC-Authentication-Results", "Received-SPF", "Return-Path"):
                    for key, value in msg.headers.items():
                        if key.lower() == name.lower():
                            self.stdout.write(f"  {key}: {value.splitlines()[0][:200]}")
                verdict = self.style.SUCCESS("PASS") if ok else self.style.WARNING("NOT VERIFIED")
                self.stdout.write(f"  -> sender authentication: {verdict} ({reason})")
            self.stdout.write(
                f"\nTrusting header {settings.INBOUND_AUTH_HEADER!r} with authserv-id {settings.INBOUND_AUTHSERV_ID!r}."
            )
        except MailboxError as exc:
            raise CommandError(str(exc))
        finally:
            client.logout()
