"""Provider-agnostic email data model and utility functions.

Shared by GoogleGmailService, ImapEmailService, and consumers like
email_command.py and email_alert_agent.py.
"""

import re
from dataclasses import dataclass
from datetime import datetime


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
    # List-Unsubscribe actuation data (RFC 2369 / RFC 8058), parsed from headers
    unsubscribe_url: str = ""  # First <https://...> entry in List-Unsubscribe
    unsubscribe_mailto: str = ""  # First <mailto:...> entry, address only
    unsubscribe_one_click: bool = False  # List-Unsubscribe-Post: List-Unsubscribe=One-Click


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
