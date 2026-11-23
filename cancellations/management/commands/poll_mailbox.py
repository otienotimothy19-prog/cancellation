import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from cancellations.services.mailbox import poll


class Command(BaseCommand):
    help = "Read new insurer replies from the cancellations mailbox (IMAP). Use --loop for the worker."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep polling every MAIL_POLL_INTERVAL seconds.")
        parser.add_argument("--interval", type=int, default=None)

    def handle(self, loop=False, interval=None, **options):
        interval = interval or settings.MAIL_POLL_INTERVAL
        while True:
            close_old_connections()
            r = poll()
            msg = f"fetched={r.fetched} new={r.created} duplicates={r.duplicates} skipped={r.skipped or 0}"
            if r.error:
                self.stderr.write(f"Poll error: {r.error}")
            else:
                self.stdout.write(msg)
            if not loop:
                return
            time.sleep(interval)
