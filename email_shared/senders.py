"""Deterministic bulk/automated-sender detection.

The smart-reply surface drafts replies the user can send with one tap, so a
false positive is expensive: a field incident saw a marketing email from
marketplace-messages@amazon.com get a drafted reply ("I have already rated my
experience…"). The LLM filter alone is insufficient — this module is the
deterministic screen that runs BEFORE any LLM sees a candidate.

Three independent signals; any one marks the sender as bulk:

1. The sender's local-part matches a conservative automated-sender list
   (noreply, notifications, marketing, …) — exact match after normalizing
   dots/dashes/underscores, or a list entry followed by a non-letter
   ("noreply+tag", "alerts-us").
2. The message carries any List-Unsubscribe signal (``unsubscribe_url`` /
   ``unsubscribe_mailto`` / ``unsubscribe_one_click`` — populated on search
   results since the subscription-cleanup work). Humans don't send mail with
   unsubscribe headers.
3. ``is_promotional`` on the message — Gmail CATEGORY_PROMOTIONS /
   CATEGORY_UPDATES labels, Precedence: bulk/list/junk, or Auto-Submitted
   (RFC 3834). The marketplace-messages@ incident sender carries neither
   signal 1 nor 2; Gmail categorizes it as Updates.

Consumers:
- smart_reply agent — drops bulk senders from the candidate list before the
  stage-1 LLM filter; they never reach the LLM and never get drafts.
- email_alerts agent — urgent-keyword matches from bulk senders still post
  (an automated fraud alert from noreply@bank must not be lost) but with
  draft=None forced; VIP senders are explicitly user-listed and are NEVER
  screened.
"""

from typing import Any

from email_shared.email_message import extract_email

# Conservative automated-sender local parts. Exact-match comparison happens
# on the separator-normalized form (dots/dashes/underscores removed), so
# "no-reply", "no.reply", and "no_reply" all collapse to "noreply". The raw
# entries (hyphens kept) also drive the prefix rule below.
_BULK_LOCAL_PARTS: frozenset[str] = frozenset({
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "notifications", "notification", "notify",
    "alerts", "alert",
    "mailer", "mailer-daemon",
    "bounce", "bounces",
    "newsletter", "news", "marketing",
    "promo", "promotions", "offers", "deals", "updates",
    "digest", "auto-confirm", "autoreply", "auto-reply",
})


def _normalize(local_part: str) -> str:
    """Drop dot/dash/underscore separators: "no-reply" / "no.reply" → "noreply"."""
    return local_part.replace(".", "").replace("-", "").replace("_", "")


_NORMALIZED_BULK_LOCAL_PARTS: frozenset[str] = frozenset(
    _normalize(p) for p in _BULK_LOCAL_PARTS
)


def is_bulk_sender(email: Any) -> bool:
    """True when ``email`` is from an automated/bulk sender (see module docstring).

    ``email`` is any object with a ``sender`` attribute ("Name <addr>" or bare
    address); the unsubscribe fields are read via ``getattr`` with falsy
    defaults so non-EmailMessage shapes (tests, partial fetches) are safe.
    """
    # Signal 2: any List-Unsubscribe actuation data — even a human-looking
    # local part (e.g. marketplace-messages@) is bulk if it carries one.
    if (
        getattr(email, "unsubscribe_url", "")
        or getattr(email, "unsubscribe_mailto", "")
        or getattr(email, "unsubscribe_one_click", False)
    ):
        return True

    # Signal 3: provider/header bulk markers — Gmail CATEGORY_PROMOTIONS /
    # CATEGORY_UPDATES labels, Precedence: bulk/list/junk, or Auto-Submitted.
    # Catches transactional senders that carry neither unsubscribe headers
    # nor a pattern-matched local part (the marketplace-messages@ incident).
    if getattr(email, "is_promotional", False):
        return True

    # Signal 1: automated-sender local part.
    address = extract_email(email.sender or "").lower()
    local_part = address.split("@", 1)[0].strip()
    if not local_part:
        return False

    if _normalize(local_part) in _NORMALIZED_BULK_LOCAL_PARTS:
        return True

    # Prefix rule: a list entry followed by a non-letter ("noreply+tag",
    # "alerts-us", "news.daily") is the same automated mailbox. The
    # non-letter requirement keeps human names safe ("newsom@", "alberta@").
    for token in _BULK_LOCAL_PARTS:
        if local_part.startswith(token):
            rest = local_part[len(token):]
            if rest and not rest[0].isalpha():
                return True
    return False
