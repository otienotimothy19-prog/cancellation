"""Hook the email provider's inbound webhook (via django-anymail) into our ingest pipeline."""

import logging

from anymail.signals import inbound
from django.dispatch import receiver

from .services.inbound import from_anymail, ingest

logger = logging.getLogger(__name__)


@receiver(inbound, dispatch_uid="imoth_cancellations_inbound")
def handle_inbound(sender, event, esp_name, **kwargs):
    msg = from_anymail(event.message, event_id=event.event_id)
    email, created = ingest(msg)
    logger.info(
        "Inbound %s email %s: %s (case=%s, matched_by=%s)",
        esp_name, email.message_id, "stored" if created else "duplicate ignored", email.case_id, email.matched_by,
    )
