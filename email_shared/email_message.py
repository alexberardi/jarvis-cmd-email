"""Provider-agnostic email data model and utility functions.

Shared by GoogleGmailService, ImapEmailService, and consumers like
email_command.py and email_alert_agent.py.
"""

import re
from dataclasses import dataclass
from datetime import datetime


class EmailConnectionError(Exception):
    """Connection-class failure reaching the user's email server.

    Raised (instead of swallowed into ``[]``/``None``) so callers can SAY the
    mailbox is unreachable — a field incident saw the Proton Bridge die for a
    week while "check my email" answered "You have no unread emails".

    ``description`` is a short human-readable sentence safe to speak aloud,
    e.g. "Couldn't connect to the Proton Mail Bridge at 127.0.0.1:1143".
    """

    def __init__(self, description: str) -> None:
        super().__init__(description)
        self.description = description


@dataclass
class EmailMessage:
    """A single email message with metadata and optional body."""

    id: str
    sender: str  # Full "Name <email>" string
    sender_name: str  # Parsed display name
    subject: str
    snippet: str  # Short preview text
    date: datetime
    is_unread: bool
    body: str = ""  # Plain-text body (truncated for voice)
    thread_id: str = ""  # Thread identifier (Gmail threadId or Message-ID header)
    # Raw recipient header values ("Name <a@x.com>, b@y.com"). Used by the
    # reply rubric to judge whether the user was addressed DIRECTLY (in To)
    # or merely CC'd / part of a list blast.
    to: str = ""
    cc: str = ""
    # List-Unsubscribe actuation data (RFC 2369 / RFC 8058), parsed from headers
    unsubscribe_url: str = ""  # First <https://...> entry in List-Unsubscribe
    unsubscribe_mailto: str = ""  # First <mailto:...> entry, address only
    unsubscribe_one_click: bool = False  # List-Unsubscribe-Post: List-Unsubscribe=One-Click
    # Bulk/automated mail signal beyond unsubscribe headers: Gmail
    # CATEGORY_PROMOTIONS / CATEGORY_UPDATES labels, Precedence: bulk/list/junk,
    # or Auto-Submitted != no. IMAP has no category labels, headers only.
    is_promotional: bool = False


def parse_bulk_headers(precedence: str, auto_submitted: str) -> bool:
    """True when standard automated-mail headers mark a message as bulk.

    Precedence: bulk|list|junk (de-facto standard for automated senders) or
    Auto-Submitted with any value other than "no" (RFC 3834).
    """
    if precedence.strip().lower() in ("bulk", "list", "junk"):
        return True
    auto = auto_submitted.strip().lower()
    return bool(auto) and auto != "no"


def extract_email(sender: str) -> str:
    """Extract email address from a sender string.

    "John Doe <john@x.com>" -> "john@x.com"
    "plain@example.com" -> "plain@example.com"
    """
    match = re.search(r'<([^>]+)>', sender)
    if match:
        return match.group(1)
    return sender.strip()


def extract_name(sender: str) -> str:
    """Extract display name from a sender string.

    "John Doe <john@x.com>" -> "John Doe"
    '"Jane Smith" <jane@x.com>' -> "Jane Smith"
    "plain@example.com" -> "plain@example.com"
    """
    match = re.match(r'^"?([^"<]+?)"?\s*<', sender)
    if match:
        return match.group(1).strip()
    return sender.strip()


def parse_unsubscribe_headers(
    list_unsubscribe: str,
    list_unsubscribe_post: str,
) -> tuple[str, str, bool]:
    """Parse List-Unsubscribe / List-Unsubscribe-Post header values.

    Shared by both services' header-parse sites so the two can't drift.

    Returns ``(url, mailto, one_click)``:
    - ``url`` — the first ``<https://...>`` entry in List-Unsubscribe
    - ``mailto`` — the first ``<mailto:...>`` entry, address only
      (query params like ``?subject=unsubscribe`` are dropped)
    - ``one_click`` — List-Unsubscribe-Post contains
      ``List-Unsubscribe=One-Click`` (case-insensitive, RFC 8058)
    """
    url = ""
    mailto = ""
    for entry in re.findall(r"<([^>]+)>", list_unsubscribe or ""):
        entry = entry.strip()
        if not url and entry.lower().startswith("https://"):
            url = entry
        elif not mailto and entry.lower().startswith("mailto:"):
            mailto = entry[len("mailto:"):].split("?", 1)[0].strip()

    one_click = "list-unsubscribe=one-click" in (list_unsubscribe_post or "").lower()
    return url, mailto, one_click
