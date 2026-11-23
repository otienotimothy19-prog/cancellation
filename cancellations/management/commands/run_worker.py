import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from cancellations.services.mailbox import poll
from cancellations.services.sla import check_sla

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Background worker: poll the mailbox every MAIL_POLL_INTERVAL s and run SLA checks every SLA_CHECK_INTERVAL s."

    def handle(self, **options):
        last_sla = 0.0
        self.stdout.write("Worker started")
        while True:
            close_old_connections()
            try:
                if settings.EMAIL_PROVIDER == "zoho":
                    r = poll()
                    if r.fetched or r.error:
                        logger.info("Mailbox: fetched=%s new=%s error=%s", r.fetched, r.created, r.error or "-")
                if time.monotonic() - last_sla >= settings.SLA_CHECK_INTERVAL:
                    s = check_sla()
                    last_sla = time.monotonic()
                    if s.reminded or s.flagged_stale:
                        logger.info("SLA: reminded=%s stale=%s", s.reminded, s.flagged_stale)
            except Exception:
                logger.exception("Worker iteration failed; retrying next cycle")
            time.sleep(settings.MAIL_POLL_INTERVAL)
