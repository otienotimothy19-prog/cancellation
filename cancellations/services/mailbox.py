"""Read insurer replies from the cancellations mailbox (Zoho Mail) over IMAP.

The folder is opened read-only and messages are fetched with BODY.PEEK, so polling never
changes what the staff member sees in Zoho (unread stays unread). Progress is tracked by
IMAP UID in MailboxState; ingest() is idempotent on Message-ID, so re-reading a message
(after a crash or a UIDVALIDITY reset) never creates duplicates.
"""

import imaplib
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from anymail.inbound import AnymailInboundMessage
from django.conf import settings
from django.utils import timezone

from ..models import MailboxState
from .inbound import from_anymail, ingest

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # a message that fails this many polls in a row is skipped (and logged)


class MailboxError(Exception):
    pass


@dataclass
class PollResult:
    fetched: int = 0
    created: int = 0
    duplicates: int = 0
    skipped: list = field(default_factory=list)
    error: str = ""


def connect():
    if not settings.MAILBOX_PASSWORD:
        raise MailboxError("MAILBOX_PASSWORD is not set")
    client = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT, timeout=60)
    client.login(settings.MAILBOX_USER, settings.MAILBOX_PASSWORD)
    return client


def _ok(typ, data, what):
    if typ != "OK":
        raise MailboxError(f"IMAP {what} failed: {data!r}")
    return data


def _select(client, folder):
    _ok(*client.select(f'"{folder}"', readonly=True), f"SELECT {folder}")
    typ, data = client.response("UIDVALIDITY")
    try:
        return int(data[0])
    except (TypeError, ValueError, IndexError):
        raise MailboxError(f"No UIDVALIDITY for {folder}: {data!r}")


def _fetch_raw(client, uid):
    data = _ok(*client.uid("FETCH", str(uid), "(BODY.PEEK[])"), f"FETCH {uid}")
    for part in data:
        if isinstance(part, tuple) and len(part) == 2 and isinstance(part[1], bytes):
            return part[1]
    raise MailboxError(f"Message UID {uid} has no body")


def _new_uids(client, state, uidvalidity):
    if state.uidvalidity != uidvalidity:
        # First run, or the server renumbered the folder: rescan recent mail. Idempotency
        # on Message-ID makes re-reading already-stored messages harmless.
        since = (timezone.now() - timedelta(days=settings.IMAP_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        data = _ok(*client.uid("SEARCH", None, "SINCE", since), "SEARCH SINCE")
        state.uidvalidity, state.last_uid = uidvalidity, 0
        state.failing_uid, state.failures = None, 0
    else:
        data = _ok(*client.uid("SEARCH", None, "UID", f"{state.last_uid + 1}:*"), "SEARCH UID")
    # "n:*" always returns the highest UID even when it is below n, hence the filter.
    return sorted(u for u in (int(x) for x in re.findall(rb"\d+", data[0] or b"")) if u > state.last_uid)


def poll(client=None, folder=None, limit=200):
    """Fetch and ingest new messages. Returns a PollResult."""
    folder = folder or settings.IMAP_FOLDER
    state, _ = MailboxState.objects.get_or_create(folder=folder)
    result = PollResult()
    own_client = client is None
    try:
        client = client or connect()
        uidvalidity = _select(client, folder)
        for uid in _new_uids(client, state, uidvalidity)[:limit]:
            try:
                raw = _fetch_raw(client, uid)
                email, created = ingest(from_anymail(AnymailInboundMessage.parse_raw_mime_bytes(raw), from_mailbox=True))
            except (imaplib.IMAP4.error, OSError):
                raise  # connection trouble: stop, retry the same UID next poll
            except Exception as exc:
                state.failures = state.failures + 1 if state.failing_uid == uid else 1
                state.failing_uid = uid
                logger.exception("Could not ingest UID %s in %s (attempt %s)", uid, folder, state.failures)
                if state.failures < MAX_ATTEMPTS:
                    state.last_error = f"UID {uid}: {exc}"
                    break  # retry from this UID next time
                result.skipped.append(uid)
                state.last_error = f"Skipped UID {uid} after {MAX_ATTEMPTS} failures: {exc}"
                logger.error(state.last_error)
            else:
                result.fetched += 1
                result.created += int(created)
                result.duplicates += int(not created)
                state.failing_uid, state.failures = None, 0
            state.last_uid = uid
            state.save()
        else:
            state.last_error = "" if not result.skipped else state.last_error
    except (MailboxError, imaplib.IMAP4.error, OSError) as exc:
        result.error = str(exc)
        state.last_error = f"{timezone.now():%Y-%m-%d %H:%M} {exc}"
        logger.error("Mailbox poll failed: %s", exc)
    finally:
        state.last_polled_at = timezone.now()
        state.save()
        if own_client and client is not None:
            try:
                client.logout()
            except Exception:
                pass
    return result
