"""Suggest how staff might classify an insurer reply, from its wording.

Suggestion only: it pre-selects an option in "Review this reply" and shows a hint. A human
still records every decision (the "never auto-confirm" rule). Only the insurer's own new
text is read: the quoted copy of our request below it ("Please cancel the following
cover...") is cut off first, otherwise every reply would look like a confirmation.

When the wording points more than one way (e.g. "cancelled?" or "not yet cancelled"), no
suggestion is made.
"""

import re

CONFIRMATION, REJECTION, QUERY = "CONFIRMATION", "REJECTION", "QUERY"

# Where the quoted earlier message starts (Outlook, Gmail, Zoho and plain "> " quoting).
QUOTE_STARTS = [
    re.compile(r"^\s*On .{0,200}wrote:\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*From:\s.+$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*_{5,}\s*$", re.MULTILINE),
    re.compile(r"^\s*>", re.MULTILINE),
    # Sign-off: what follows is a signature or disclaimer ("cannot", "?" there mean nothing).
    re.compile(r"^\s*(kind |best |warm )?regards\b|^\s*(thanks|thank you|yours (faithfully|sincerely|truly))\b[,.!]?\s*$",
               re.IGNORECASE | re.MULTILINE),
]

CONFIRM_PATTERNS = [
    r"\bdone\b",
    r"\b(has|have|had) been (duly )?cancell?ed\b",
    r"\b(is|are|was|were) (now )?cancell?ed\b",
    r"\bcancell?ed (as (requested|advised|per (your )?request))\b",
    r"\bcancell?ation (has been |is |was )?(effected|processed|completed|done|confirmed|actioned)\b",
    r"\b(we|i) (have|hereby) (cancell?ed|confirm(ed)? (the )?cancell?ation)\b",
    r"\bconfirm(ed)? (that )?(the )?(cover|policy|certificate)\b.{0,40}\bcancell?ed\b",
    r"\bnoted and (actioned|cancell?ed|done)\b",
    r"\b(cover|policy|certificate)\b.{0,40}\bterminated\b",
]
REJECT_PATTERNS = [
    r"\b(cannot|can ?not|can't|unable to|will not|won't|not able to) (be )?(cancel|process|effect)",
    r"\b(declined?|rejected?)\b",
    r"\bnot (eligible|possible) (for|to) (cancel|refund)",
]
QUERY_PATTERNS = [
    r"\b(kindly|please|pls) (provide|share|send|forward|attach|advise|confirm|clarify)\b",
    r"\b(we|i) (require|need)\b",
    r"\b(missing|outstanding) (document|form|information|details)\b",
    r"\?",
]
# Words that undo a "cancelled": "not yet cancelled", "will be cancelled", "pending cancellation".
NOT_YET = [
    r"\bnot (yet |been )*(done|cancell?ed|effected|processed)\b",
    r"\b(will|shall|to) be (cancell?ed|done|effected|processed)\b",
    r"\b(pending|awaiting|in progress|under process)\b",
    r"\bonce (we|you|the)\b",
]


def new_text(body):
    """The reply's own text, without the quoted earlier messages or a long signature."""
    text = body or ""
    cut = len(text)
    for pattern in QUOTE_STARTS:
        m = pattern.search(text)
        if m and m.start() < cut:
            cut = m.start()
    return text[:cut][:2000]


def _any(patterns, text):
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def suggest(body):
    """CONFIRMATION, REJECTION, QUERY or None (unclear)."""
    text = new_text(body)
    if not text.strip():
        return None
    confirms = _any(CONFIRM_PATTERNS, text)
    rejects = _any(REJECT_PATTERNS, text)
    queries = _any(QUERY_PATTERNS, text)
    not_yet = _any(NOT_YET, text)
    if rejects and not confirms:
        return REJECTION
    if confirms and not (rejects or queries or not_yet):
        return CONFIRMATION
    if queries and not (confirms or rejects):
        return QUERY
    return None
