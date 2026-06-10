"""Full-featured email command — list, read, search, send, reply, archive, trash, star.

Supports multiple email backends via EMAIL_PROVIDER secret:
- "gmail" (default) — Google Gmail REST API with OAuth2
- "imap" — Generic IMAP/SMTP (Proton Mail Bridge, Fastmail, etc.)
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import requests

try:
    from jarvis_log_client import JarvisLogger
    logger = JarvisLogger(service="jarvis-node")
except ImportError:
    import logging
    logger = logging.getLogger("jarvis-cmd-email")

from jarvis_command_sdk import (
    AuthenticationConfig,
    CommandExample,
    CommandResponse,
    FastPathPattern,
    IJarvisButton,
    IJarvisCommand,
    IJarvisParameter,
    IJarvisSecret,
    InteractiveAction,
    InteractiveList,
    InteractiveRow,
    InteractiveSection,
    JarvisInbox,
    JarvisParameter,
    JarvisSecret,
    JarvisStorage,
    PreRouteResult,
    RequestInformation,
    callback,
)

from email_shared.email_message import EmailMessage, extract_email
from email_shared.email_service_factory import create_email_service, get_email_provider
from email_shared.triage import build_triage_body, build_triage_payload

# Default OAuth client ID — shipped with Jarvis so users don't need to create
# their own Google Cloud project. Override via GMAIL_CLIENT_ID secret if needed.
_DEFAULT_CLIENT_ID = "683175564329-24fi9h6hck48hfrbjhb24vf12680e5ec.apps.googleusercontent.com"

# Ordinal words -> integers for voice ("read the third email")
_ORDINALS: dict[str, int] = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5,
    "6th": 6, "7th": 7, "8th": 8, "9th": 9, "10th": 10,
    "last": -1,
}

_ALL_ACTIONS = [
    "list", "read", "search", "send", "reply", "forward",
    "archive", "trash", "star", "unstar", "mark_read", "mark_unread", "triage",
    "unsubscribe_scan",
]

# Triage callback verb -> spoken result template ({n} = success count).
_TRIAGE_RESULT_TEMPLATES = {
    "mark_read": "Marked {n} read.",
    "archive": "Archived {n}.",
    "star": "Starred {n}.",
}

# Unread emails pulled into the triage interactive list.
_TRIAGE_MAX_RESULTS = 25

# Subscription cleanup scan — aged-unread mail over a 90-day window. Gmail
# prefilters to bulk-mail categories; IMAP has no categories, so the 7-day
# "aged" cut happens client-side instead.
_UNSUB_GMAIL_QUERY = (
    "is:unread older_than:7d newer_than:90d (category:promotions OR category:updates)"
)
_UNSUB_IMAP_QUERY = "is:unread newer_than:90d"
_UNSUB_MAX_RESULTS = 50
_UNSUB_AGED_DAYS = 7  # IMAP client-side: keep only mail older than this
_UNSUB_MIN_MESSAGES = 3  # senders below this never surface as candidates
_UNSUB_MAX_CANDIDATES = 15  # one full fetch per candidate — keep it bounded
_UNSUB_TTL_HOURS = 24  # actuation records expire; stale lists must re-scan
_UNSUB_EMPTY_TEXT = "No stale subscriptions found."

# --- Pre-route patterns ---
# Ordinal | number alternation used in several patterns. Longest-first so
# "seventh" doesn't get caught by "seven".
_ORDINAL_ALT = "|".join(sorted(_ORDINALS, key=len, reverse=True))

# "check my email" / "any new emails" / "what's in my inbox" — all list.
_LIST_RE = re.compile(
    r"^\s*(?:"
    r"check\s+(?:my\s+)?(?:email|emails|inbox|gmail|mail|messages)"
    r"|any\s+new\s+(?:emails?|messages|mail)"
    r"|what'?s\s+(?:in\s+)?my\s+inbox"
    r"|do\s+i\s+have\s+(?:any\s+)?(?:emails?|mail|messages|new\s+messages)"
    r"|read\s+my\s+emails?"
    r"|show\s+me\s+my\s+(?:inbox|emails?|mail)"
    r"|what\s+emails?\s+do\s+i\s+have"
    r")\s*[?.!]*$",
    re.IGNORECASE,
)

# "read email N" / "read the Nth email" / "open email number N"
_READ_RE = re.compile(
    r"^\s*(?:read|open|show\s+me)\s+(?:the\s+)?"
    r"(?:email\s+(?:number\s+)?(?P<num>\d+|" + _ORDINAL_ALT + r")"
    r"|(?P<num2>" + _ORDINAL_ALT + r")\s+(?:email|one)"
    r"|(?P<num3>\d+)(?:st|nd|rd|th)?\s+email)"
    r"\s*[?.!]*$",
    re.IGNORECASE,
)

# "archive email N" / "archive the Nth email" / "move email N to archive"
_ARCHIVE_RE = re.compile(
    r"^\s*archive\s+(?:the\s+)?"
    r"(?:email\s+(?:number\s+)?(?P<num>\d+|" + _ORDINAL_ALT + r")"
    r"|(?P<num2>" + _ORDINAL_ALT + r")\s+email)"
    r"\s*[?.!]*$",
    re.IGNORECASE,
)

_ARCHIVE_TO_RE = re.compile(
    r"^\s*move\s+email\s+(?P<num>\d+|" + _ORDINAL_ALT + r")\s+to\s+(?:the\s+)?archive\s*[?.!]*$",
    re.IGNORECASE,
)

# "delete email N" / "trash the Nth email" / "remove email N"
_TRASH_RE = re.compile(
    r"^\s*(?:delete|trash|remove)\s+(?:the\s+)?"
    r"(?:email\s+(?:number\s+)?(?P<num>\d+|" + _ORDINAL_ALT + r")"
    r"|(?P<num2>" + _ORDINAL_ALT + r")\s+email)"
    r"\s*[?.!]*$",
    re.IGNORECASE,
)

# "star email N" / "star the Nth email"
_STAR_RE = re.compile(
    r"^\s*star\s+(?:the\s+)?"
    r"(?:email\s+(?:number\s+)?(?P<num>\d+|" + _ORDINAL_ALT + r")"
    r"|(?P<num2>" + _ORDINAL_ALT + r")\s+email)"
    r"\s*[?.!]*$",
    re.IGNORECASE,
)

# "unstar email N" / "unstar the Nth email"
_UNSTAR_RE = re.compile(
    r"^\s*unstar\s+(?:the\s+)?"
    r"(?:email\s+(?:number\s+)?(?P<num>\d+|" + _ORDINAL_ALT + r")"
    r"|(?P<num2>" + _ORDINAL_ALT + r")\s+email)"
    r"\s*[?.!]*$",
    re.IGNORECASE,
)

# "mark email N as read" / "mark the Nth email as read"
_MARK_READ_RE = re.compile(
    r"^\s*mark\s+(?:the\s+)?"
    r"(?:email\s+(?:number\s+)?(?P<num>\d+|" + _ORDINAL_ALT + r")"
    r"|(?P<num2>" + _ORDINAL_ALT + r")\s+email)"
    r"\s+as\s+read\s*[?.!]*$",
    re.IGNORECASE,
)

# "mark email N as unread" / "mark the Nth email as unread"
_MARK_UNREAD_RE = re.compile(
    r"^\s*mark\s+(?:the\s+)?"
    r"(?:email\s+(?:number\s+)?(?P<num>\d+|" + _ORDINAL_ALT + r")"
    r"|(?P<num2>" + _ORDINAL_ALT + r")\s+email)"
    r"\s+as\s+unread\s*[?.!]*$",
    re.IGNORECASE,
)

# "triage my inbox" / "send my inbox to my phone"
_TRIAGE_RE = re.compile(
    r"^\s*(?:"
    r"triage\s+my\s+(?:inbox|emails?|mail)"
    r"|send\s+my\s+inbox\s+to\s+my\s+phone"
    r")\s*[?.!]*$",
    re.IGNORECASE,
)

# "clean up my subscriptions" / "clean up my email subscriptions"
_UNSUBSCRIBE_SCAN_RE = re.compile(
    r"^\s*clean\s+up\s+my\s+(?:email\s+)?subscriptions\s*[?.!]*$",
    re.IGNORECASE,
)


def _parse_index_token(token: str) -> int | None:
    """Map an ordinal word or digit token to a 1-based index. None on miss."""
    if not token:
        return None
    norm = token.lower().strip()
    if norm.isdigit():
        return int(norm)
    return _ORDINALS.get(norm)


def _as_utc(dt: datetime) -> datetime:
    """Normalize a possibly-naive datetime to UTC for safe comparison."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class EmailCommand(IJarvisCommand):

    def __init__(self) -> None:
        super().__init__()
        self._last_email_list: list[EmailMessage] = []
        self._storage = JarvisStorage("email")
        # Per-sender actuation records written by the unsubscribe scan and
        # read back in the unsubscribe_selected callback (24h TTL).
        self._unsubscribe_storage = JarvisStorage("email_unsubscribe")

    # -- Properties -------------------------------------------------------------

    @property
    def command_name(self) -> str:
        return "email"

    @property
    def description(self) -> str:
        return (
            "Manage email: list unread, read, search, send, reply, forward, archive, "
            "trash, star/unstar, mark read/unread, triage, or clean up subscriptions. "
            "Use for ALL email and inbox queries."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "email", "emails", "inbox", "mail", "gmail",
            "messages", "unread", "read", "send", "reply",
            "archive", "trash", "delete", "star", "forward", "triage",
            "unsubscribe", "subscriptions",
        ]

    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter(
                "action", "string", required=False,
                description="Action to perform",
                enum_values=_ALL_ACTIONS,
                default="list",
            ),
            JarvisParameter(
                "email_index", "int", required=False,
                description="1-indexed position from the last email list (for read/reply/archive/trash/star)",
            ),
            JarvisParameter(
                "max_results", "int", required=False,
                description="Maximum number of emails to return (default 5)",
            ),
            JarvisParameter(
                "query", "string", required=False,
                description="Search query (Gmail search syntax, for search action)",
            ),
            JarvisParameter(
                "to", "string", required=False,
                description="Recipient email address (for send action)",
            ),
            JarvisParameter(
                "subject", "string", required=False,
                description="Email subject line (for send action)",
            ),
            JarvisParameter(
                "body", "string", required=False,
                description="Email body text (for send/reply actions)",
            ),
        ]

    @property
    def associated_service(self) -> str:
        provider = get_email_provider()
        return "IMAP Email" if provider == "imap" else "Gmail"

    @property
    def rules(self) -> List[str]:
        return [
            "Default action is 'list' (unread inbox emails)",
            "Use 'read' with email_index to read a specific email from the last list",
            "Use 'search' with a query for specific emails by sender/topic/keyword",
            "Use 'send' with to + body to compose a new email",
            "Use 'reply' with email_index + body to reply to an email",
            "Use 'forward' with email_index + to to forward an email (body is an optional note)",
            "'archive' removes from inbox but keeps the email",
            "'delete' means 'trash' — moves to trash",
            "Use 'mark_read'/'mark_unread' with email_index to toggle read state",
            "'unstar' removes the star from an email",
            "Use 'triage' to send an interactive list of unread emails to the user's phone",
            "Use 'unsubscribe_scan' to find subscription senders the user never reads and send an unsubscribe picker to their phone",
            "Email indices are 1-based from the most recent list/search results",
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "NEVER send an email without explicit user intent — when in doubt, use 'list' or 'read'",
        ]

    # -- Secrets & Auth ---------------------------------------------------------

    def _get_client_id(self) -> str:
        return self._storage.get_secret("GMAIL_CLIENT_ID") or _DEFAULT_CLIENT_ID

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        base = [
            JarvisSecret(
                "EMAIL_PROVIDER",
                "Email provider",
                "user", "string", required=False, is_sensitive=False,
                friendly_name="Email Provider",
                enum_values=["gmail", "yahoo", "outlook", "proton", "fastmail", "imap"],
                presets={
                    "yahoo": {
                        "IMAP_HOST": "imap.mail.yahoo.com", "IMAP_PORT": "993",
                        "IMAP_USE_SSL": "true", "SMTP_HOST": "smtp.mail.yahoo.com", "SMTP_PORT": "465",
                    },
                    "outlook": {
                        "IMAP_HOST": "outlook.office365.com", "IMAP_PORT": "993",
                        "IMAP_USE_SSL": "true", "SMTP_HOST": "smtp.office365.com", "SMTP_PORT": "587",
                    },
                    "fastmail": {
                        "IMAP_HOST": "imap.fastmail.com", "IMAP_PORT": "993",
                        "IMAP_USE_SSL": "true", "SMTP_HOST": "smtp.fastmail.com", "SMTP_PORT": "465",
                    },
                    "proton": {
                        "IMAP_HOST": "127.0.0.1", "IMAP_PORT": "1143",
                        "IMAP_USE_SSL": "false", "SMTP_HOST": "127.0.0.1", "SMTP_PORT": "1025",
                    },
                },
            ),
        ]
        provider = get_email_provider()
        if provider != "gmail":
            base.extend([
                JarvisSecret(
                    "IMAP_HOST", "IMAP server hostname",
                    "user", "string", required=False, is_sensitive=False,
                    friendly_name="IMAP Host",
                ),
                JarvisSecret(
                    "IMAP_PORT", "IMAP server port (993 for SSL, 1143 for STARTTLS)",
                    "user", "string", required=False, is_sensitive=False,
                    friendly_name="IMAP Port",
                ),
                JarvisSecret(
                    "IMAP_USERNAME", "IMAP/SMTP login username (full email address)",
                    "user", "string", is_sensitive=False,
                    friendly_name="IMAP Username",
                ),
                JarvisSecret(
                    "IMAP_PASSWORD", "IMAP/SMTP login password",
                    "user", "string", is_sensitive=True,
                    friendly_name="IMAP Password",
                ),
                JarvisSecret(
                    "SMTP_HOST", "SMTP server hostname",
                    "user", "string", required=False, is_sensitive=False,
                    friendly_name="SMTP Host",
                ),
                JarvisSecret(
                    "SMTP_PORT", "SMTP server port",
                    "user", "string", required=False, is_sensitive=False,
                    friendly_name="SMTP Port",
                ),
                JarvisSecret(
                    "IMAP_USE_SSL", "Use SSL instead of STARTTLS",
                    "user", "string", required=False, is_sensitive=False,
                    friendly_name="Use SSL",
                ),
            ])
        else:
            base.append(
                JarvisSecret(
                    "GMAIL_CLIENT_ID",
                    "Google OAuth client ID for Gmail (optional — a default is provided)",
                    "integration", "string", required=False, is_sensitive=False,
                    friendly_name="Client ID (optional)",
                ),
            )
        return base

    @property
    def all_possible_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret(
                "EMAIL_PROVIDER",
                "Email provider",
                "user", "string", required=False, is_sensitive=False,
                friendly_name="Email Provider",
                enum_values=["gmail", "yahoo", "outlook", "proton", "fastmail", "imap"],
                presets={
                    "yahoo": {
                        "IMAP_HOST": "imap.mail.yahoo.com", "IMAP_PORT": "993",
                        "IMAP_USE_SSL": "true", "SMTP_HOST": "smtp.mail.yahoo.com", "SMTP_PORT": "465",
                    },
                    "outlook": {
                        "IMAP_HOST": "outlook.office365.com", "IMAP_PORT": "993",
                        "IMAP_USE_SSL": "true", "SMTP_HOST": "smtp.office365.com", "SMTP_PORT": "587",
                    },
                    "fastmail": {
                        "IMAP_HOST": "imap.fastmail.com", "IMAP_PORT": "993",
                        "IMAP_USE_SSL": "true", "SMTP_HOST": "smtp.fastmail.com", "SMTP_PORT": "465",
                    },
                    "proton": {
                        "IMAP_HOST": "127.0.0.1", "IMAP_PORT": "1143",
                        "IMAP_USE_SSL": "false", "SMTP_HOST": "127.0.0.1", "SMTP_PORT": "1025",
                    },
                },
            ),
            # Gmail secrets (client ID is shared, tokens are per-user)
            JarvisSecret(
                "GMAIL_CLIENT_ID",
                "Google OAuth client ID for Gmail (optional — a default is provided)",
                "integration", "string", required=False, is_sensitive=False,
                friendly_name="Client ID (optional)",
            ),
            JarvisSecret(
                "GMAIL_ACCESS_TOKEN", "Gmail OAuth access token",
                "user", "string", friendly_name="Access Token",
            ),
            JarvisSecret(
                "GMAIL_REFRESH_TOKEN", "Gmail OAuth refresh token",
                "user", "string", friendly_name="Refresh Token",
            ),
            # IMAP settings (all per-user)
            JarvisSecret(
                "IMAP_HOST", "IMAP server hostname",
                "user", "string", required=False, is_sensitive=False,
                friendly_name="IMAP Host",
            ),
            JarvisSecret(
                "IMAP_PORT", "IMAP server port (1143 for STARTTLS, 993 for SSL)",
                "user", "string", required=False, is_sensitive=False,
                friendly_name="IMAP Port",
            ),
            JarvisSecret(
                "IMAP_USERNAME", "IMAP/SMTP login username (full email address)",
                "user", "string", required=False, is_sensitive=False,
                friendly_name="IMAP Username",
            ),
            JarvisSecret(
                "IMAP_PASSWORD", "IMAP/SMTP login password",
                "user", "string", required=False, is_sensitive=True,
                friendly_name="IMAP Password",
            ),
            JarvisSecret(
                "SMTP_HOST", "SMTP server hostname",
                "user", "string", required=False, is_sensitive=False,
                friendly_name="SMTP Host",
            ),
            JarvisSecret(
                "SMTP_PORT", "SMTP server port",
                "user", "string", required=False, is_sensitive=False,
                friendly_name="SMTP Port",
            ),
            JarvisSecret(
                "IMAP_USE_SSL", "Use SSL instead of STARTTLS for IMAP",
                "user", "string", required=False, is_sensitive=False,
                friendly_name="Use SSL",
            ),
            JarvisSecret(
                "IMAP_ARCHIVE_FOLDER", "IMAP folder name for archive (default: Archive)",
                "user", "string", required=False, is_sensitive=False,
                friendly_name="Archive Folder",
            ),
            JarvisSecret(
                "IMAP_TRASH_FOLDER", "IMAP folder name for trash (default: Trash)",
                "user", "string", required=False, is_sensitive=False,
                friendly_name="Trash Folder",
            ),
        ]

    @property
    def authentication(self) -> AuthenticationConfig | None:
        if get_email_provider() != "gmail":
            return None  # Non-gmail providers use username/password secrets, no OAuth
        client_id = self._get_client_id()
        if not client_id:
            return None
        return AuthenticationConfig(
            type="oauth",
            provider="google_gmail",
            friendly_name="Gmail",
            client_id=client_id,
            keys=["access_token", "refresh_token"],
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            exchange_url="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/gmail.modify"],
            supports_pkce=True,
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
            requires_background_refresh=True,
            refresh_token_secret_key="GMAIL_REFRESH_TOKEN",
            native_redirect_uri="com.googleusercontent.apps.683175564329-24fi9h6hck48hfrbjhb24vf12680e5ec:/oauthredirect",
        )

    def store_auth_values(self, values: dict[str, str]) -> None:
        if "access_token" in values:
            self._storage.set_secret("GMAIL_ACCESS_TOKEN", values["access_token"])
        if "refresh_token" in values:
            self._storage.set_secret("GMAIL_REFRESH_TOKEN", values["refresh_token"])
        try:
            from services.command_auth_service import clear_auth_flag
            clear_auth_flag("google_gmail")
        except ImportError:
            pass

    # -- Post-process -----------------------------------------------------------

    def post_process_tool_call(self, args: Dict[str, Any], voice_command: str) -> Dict[str, Any]:
        # Default action
        if not args.get("action"):
            args["action"] = "list"

        # "delete" -> "trash"
        if args.get("action") == "delete":
            args["action"] = "trash"

        # Extract ordinal from voice if email_index not already set
        if not args.get("email_index"):
            idx = self._extract_index_from_voice(voice_command)
            if idx is not None:
                args["email_index"] = idx

        return args

    @staticmethod
    def _extract_index_from_voice(voice: str) -> int | None:
        """Extract email index from voice command text."""
        lower = voice.lower()

        # Check ordinal words
        for word, idx in _ORDINALS.items():
            if word in lower:
                return idx

        # Check "email N" or "number N" patterns
        match = re.search(r'(?:email|number|#)\s*(\d+)', lower)
        if match:
            return int(match.group(1))

        return None

    # -- Examples ---------------------------------------------------------------

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample("Check my email", {"action": "list"}, is_primary=True),
            CommandExample("Read email 3", {"action": "read", "email_index": 3}),
            CommandExample("Search my email for receipts", {"action": "search", "query": "receipts"}),
            CommandExample(
                "Send an email to john@example.com saying I'll be late",
                {"action": "send", "to": "john@example.com", "subject": "Running late", "body": "I'll be late"},
            ),
            CommandExample("Reply to the first email saying thanks", {"action": "reply", "email_index": 1, "body": "Thanks!"}),
            CommandExample("Archive email 2", {"action": "archive", "email_index": 2}),
            CommandExample("Delete the third email", {"action": "trash", "email_index": 3}),
            CommandExample("Star the first email", {"action": "star", "email_index": 1}),
            CommandExample(
                "Forward email 2 to john@example.com",
                {"action": "forward", "email_index": 2, "to": "john@example.com"},
            ),
            CommandExample("Mark email 1 as read", {"action": "mark_read", "email_index": 1}),
            CommandExample("Triage my inbox", {"action": "triage"}),
            CommandExample("Clean up my subscriptions", {"action": "unsubscribe_scan"}),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        examples: list[tuple[str, dict[str, Any], bool]] = [
            # List (default)
            ("Check my email", {"action": "list"}, True),
            ("Any new emails?", {"action": "list"}, False),
            ("What's in my inbox?", {"action": "list"}, False),
            ("Do I have any emails?", {"action": "list"}, False),
            ("Read my emails", {"action": "list"}, False),
            ("Show me my inbox", {"action": "list"}, False),
            ("Any new messages?", {"action": "list"}, False),
            ("Check my Gmail", {"action": "list"}, False),
            ("What emails do I have?", {"action": "list"}, False),
            ("Do I have mail?", {"action": "list"}, False),
            # Read
            ("Read email 3", {"action": "read", "email_index": 3}, False),
            ("Read the first email", {"action": "read", "email_index": 1}, False),
            ("What does the second email say?", {"action": "read", "email_index": 2}, False),
            ("Open email number 5", {"action": "read", "email_index": 5}, False),
            ("Show me the third one", {"action": "read", "email_index": 3}, False),
            ("Read the last email", {"action": "read", "email_index": -1}, False),
            # Search
            ("Search my email for flight confirmation", {"action": "search", "query": "flight confirmation"}, False),
            ("Find emails from John", {"action": "search", "query": "from:John"}, False),
            ("Any emails about the meeting?", {"action": "search", "query": "meeting"}, False),
            ("Search for receipts", {"action": "search", "query": "receipts"}, False),
            ("Find that email about the dentist", {"action": "search", "query": "dentist"}, False),
            ("Look for emails from Amazon", {"action": "search", "query": "from:Amazon"}, False),
            ("Search my inbox for invoices", {"action": "search", "query": "invoices"}, False),
            # Send
            ("Send an email to john@example.com saying I'll be late", {"action": "send", "to": "john@example.com", "subject": "Running late", "body": "I'll be late"}, False),
            ("Email sarah@test.com about the project update", {"action": "send", "to": "sarah@test.com", "subject": "Project update", "body": "Here's the project update"}, False),
            ("Send a message to bob@company.com saying the report is ready", {"action": "send", "to": "bob@company.com", "subject": "Report ready", "body": "The report is ready"}, False),
            ("Compose an email to lisa@work.com", {"action": "send", "to": "lisa@work.com", "subject": "", "body": ""}, False),
            ("Write an email to team@company.com about tomorrow's standup", {"action": "send", "to": "team@company.com", "subject": "Tomorrow's standup", "body": "Regarding tomorrow's standup"}, False),
            # Reply
            ("Reply to the first email saying thanks", {"action": "reply", "email_index": 1, "body": "Thanks!"}, False),
            ("Respond to email 2 with I'll be there", {"action": "reply", "email_index": 2, "body": "I'll be there"}, False),
            ("Reply to the third email saying sounds good", {"action": "reply", "email_index": 3, "body": "Sounds good"}, False),
            ("Answer the second email with yes I can make it", {"action": "reply", "email_index": 2, "body": "Yes I can make it"}, False),
            ("Reply to that first one and say I got it", {"action": "reply", "email_index": 1, "body": "I got it"}, False),
            # Archive
            ("Archive email 3", {"action": "archive", "email_index": 3}, False),
            ("Archive the first email", {"action": "archive", "email_index": 1}, False),
            ("Move email 2 to archive", {"action": "archive", "email_index": 2}, False),
            # Trash
            ("Delete email 2", {"action": "trash", "email_index": 2}, False),
            ("Trash the first email", {"action": "trash", "email_index": 1}, False),
            ("Delete the third email", {"action": "trash", "email_index": 3}, False),
            ("Remove email 4", {"action": "trash", "email_index": 4}, False),
            # Star
            ("Star email 1", {"action": "star", "email_index": 1}, False),
            ("Star the second email", {"action": "star", "email_index": 2}, False),
            ("Mark the first email as starred", {"action": "star", "email_index": 1}, False),
            # Unstar
            ("Unstar email 1", {"action": "unstar", "email_index": 1}, False),
            ("Unstar the second email", {"action": "unstar", "email_index": 2}, False),
            # Mark read / unread
            ("Mark email 2 as read", {"action": "mark_read", "email_index": 2}, False),
            ("Mark the first email as read", {"action": "mark_read", "email_index": 1}, False),
            ("Mark email 3 as unread", {"action": "mark_unread", "email_index": 3}, False),
            ("Mark the second email as unread", {"action": "mark_unread", "email_index": 2}, False),
            # Forward
            ("Forward email 2 to john@example.com", {"action": "forward", "email_index": 2, "to": "john@example.com"}, False),
            ("Forward the first email to sarah@test.com", {"action": "forward", "email_index": 1, "to": "sarah@test.com"}, False),
            # Triage
            ("Triage my inbox", {"action": "triage"}, False),
            ("Send my inbox to my phone", {"action": "triage"}, False),
            # Subscription cleanup
            ("Clean up my subscriptions", {"action": "unsubscribe_scan"}, False),
            ("Clean up my email subscriptions", {"action": "unsubscribe_scan"}, False),
        ]
        return [
            CommandExample(voice, params, is_primary)
            for voice, params, is_primary in examples
        ]

    # ------------------------------------------------------------------
    # Fast-path patterns — bypass LLM for deterministic email actions.
    # Send/reply/search fall through because they need natural-language
    # extraction of recipient/subject/body/query the LLM handles better.
    # ------------------------------------------------------------------

    @property
    def fast_path_patterns(self) -> List[FastPathPattern]:
        return [
            FastPathPattern(
                id="email.list",
                description="Bypass LLM for 'check my email' / 'any new emails' / 'what's in my inbox'",
                example="check my email",
                regex=_LIST_RE.pattern,
                handler="_fp_list",
            ),
            FastPathPattern(
                id="email.read",
                description="Bypass LLM for 'read email N' / 'read the Nth email'",
                example="read email 3",
                regex=_READ_RE.pattern,
                handler="_fp_read",
            ),
            FastPathPattern(
                id="email.archive",
                description="Bypass LLM for 'archive email N' / 'archive the Nth email'",
                example="archive email 2",
                regex=_ARCHIVE_RE.pattern,
                handler="_fp_archive",
            ),
            FastPathPattern(
                id="email.archive_to",
                description="Bypass LLM for 'move email N to archive'",
                example="move email 2 to archive",
                regex=_ARCHIVE_TO_RE.pattern,
                handler="_fp_archive_to",
            ),
            FastPathPattern(
                id="email.trash",
                description="Bypass LLM for 'delete email N' / 'trash the Nth email'",
                example="delete email 3",
                regex=_TRASH_RE.pattern,
                handler="_fp_trash",
            ),
            FastPathPattern(
                id="email.star",
                description="Bypass LLM for 'star email N' / 'star the Nth email'",
                example="star email 1",
                regex=_STAR_RE.pattern,
                handler="_fp_star",
            ),
            FastPathPattern(
                id="email.unstar",
                description="Bypass LLM for 'unstar email N' / 'unstar the Nth email'",
                example="unstar email 2",
                regex=_UNSTAR_RE.pattern,
                handler="_fp_unstar",
            ),
            FastPathPattern(
                id="email.mark_read",
                description="Bypass LLM for 'mark email N as read'",
                example="mark email 2 as read",
                regex=_MARK_READ_RE.pattern,
                handler="_fp_mark_read",
            ),
            FastPathPattern(
                id="email.mark_unread",
                description="Bypass LLM for 'mark email N as unread'",
                example="mark email 2 as unread",
                regex=_MARK_UNREAD_RE.pattern,
                handler="_fp_mark_unread",
            ),
            FastPathPattern(
                id="email.triage",
                description="Bypass LLM for 'triage my inbox' / 'send my inbox to my phone'",
                example="triage my inbox",
                regex=_TRIAGE_RE.pattern,
                handler="_fp_triage",
            ),
            FastPathPattern(
                id="email.unsubscribe_scan",
                description="Bypass LLM for 'clean up my subscriptions' / 'clean up my email subscriptions'",
                example="clean up my subscriptions",
                regex=_UNSUBSCRIBE_SCAN_RE.pattern,
                handler="_fp_unsubscribe_scan",
            ),
        ]

    def _fp_list(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={"action": "list"})

    def _fp_read(self, match, voice_command: str) -> PreRouteResult | None:
        token = match.group("num") or match.group("num2") or match.group("num3")
        idx = _parse_index_token(token)
        if idx is None:
            return None
        return PreRouteResult(arguments={"action": "read", "email_index": idx})

    def _fp_archive(self, match, voice_command: str) -> PreRouteResult | None:
        token = match.group("num") or match.group("num2")
        idx = _parse_index_token(token)
        if idx is None:
            return None
        return PreRouteResult(arguments={"action": "archive", "email_index": idx})

    def _fp_archive_to(self, match, voice_command: str) -> PreRouteResult | None:
        idx = _parse_index_token(match.group("num"))
        if idx is None:
            return None
        return PreRouteResult(arguments={"action": "archive", "email_index": idx})

    def _fp_trash(self, match, voice_command: str) -> PreRouteResult | None:
        token = match.group("num") or match.group("num2")
        idx = _parse_index_token(token)
        if idx is None:
            return None
        return PreRouteResult(arguments={"action": "trash", "email_index": idx})

    def _fp_star(self, match, voice_command: str) -> PreRouteResult | None:
        token = match.group("num") or match.group("num2")
        idx = _parse_index_token(token)
        if idx is None:
            return None
        return PreRouteResult(arguments={"action": "star", "email_index": idx})

    def _fp_unstar(self, match, voice_command: str) -> PreRouteResult | None:
        token = match.group("num") or match.group("num2")
        idx = _parse_index_token(token)
        if idx is None:
            return None
        return PreRouteResult(arguments={"action": "unstar", "email_index": idx})

    def _fp_mark_read(self, match, voice_command: str) -> PreRouteResult | None:
        token = match.group("num") or match.group("num2")
        idx = _parse_index_token(token)
        if idx is None:
            return None
        return PreRouteResult(arguments={"action": "mark_read", "email_index": idx})

    def _fp_mark_unread(self, match, voice_command: str) -> PreRouteResult | None:
        token = match.group("num") or match.group("num2")
        idx = _parse_index_token(token)
        if idx is None:
            return None
        return PreRouteResult(arguments={"action": "mark_unread", "email_index": idx})

    def _fp_triage(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={"action": "triage"})

    def _fp_unsubscribe_scan(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={"action": "unsubscribe_scan"})

    # -- Action handler (for interactive send/reply confirm) --------------------

    def handle_action(self, action_name: str, context: dict[str, Any]) -> CommandResponse:
        """Handle button-tap actions from the mobile app (send confirm)."""
        if action_name == "send_click":
            draft: dict[str, Any] = context.get("draft", {})
            return self._execute_send(draft)

        # cancel_click handled by ABC default
        return super().handle_action(action_name, context)

    def _execute_send(self, draft: dict[str, Any]) -> CommandResponse:
        """Actually send or reply based on draft type."""
        try:
            service = create_email_service()
        except ValueError as e:
            return CommandResponse.error_response(error_details=str(e))

        try:
            draft_type = draft.get("type", "send")
            if draft_type == "reply":
                result = service.reply(
                    draft["message_id"], draft["thread_id"], draft["body"]
                )
            else:
                result = service.send(draft["to"], draft["subject"], draft["body"])

            return CommandResponse.final_response(
                context_data={
                    "sent": True,
                    "message_id": result.get("id", ""),
                    "message": "Email sent successfully.",
                }
            )
        except Exception as e:
            logger.error("Email send failed", error=str(e))
            return CommandResponse.error_response(error_details=str(e))

    # -- Main execution ---------------------------------------------------------

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        action: str = kwargs.get("action", "list")

        # Auth check — provider-aware
        provider = get_email_provider()
        if provider != "gmail":
            if not self._storage.get_secret("IMAP_USERNAME", scope="user") or not self._storage.get_secret("IMAP_PASSWORD", scope="user"):
                return CommandResponse.error_response(
                    error_details="Email not configured. Set IMAP username and password in settings.",
                    context_data={"error": "Not configured"},
                )
        else:
            access_token = self._storage.get_secret("GMAIL_ACCESS_TOKEN", scope="user")
            if not access_token:
                return CommandResponse.error_response(
                    error_details="Gmail not authenticated. Complete OAuth setup first.",
                    context_data={"error": "Not authenticated"},
                )

        try:
            if action == "list":
                return self._run_list(request_info, **kwargs)
            elif action == "read":
                return self._run_read(request_info, **kwargs)
            elif action == "search":
                return self._run_search(**kwargs)
            elif action == "send":
                return self._run_send(**kwargs)
            elif action == "reply":
                return self._run_reply(**kwargs)
            elif action == "forward":
                return self._run_forward(**kwargs)
            elif action == "archive":
                return self._run_archive(**kwargs)
            elif action == "trash":
                return self._run_trash(**kwargs)
            elif action == "star":
                return self._run_star(**kwargs)
            elif action == "unstar":
                return self._run_unstar(**kwargs)
            elif action == "mark_read":
                return self._run_mark_read(**kwargs)
            elif action == "mark_unread":
                return self._run_mark_unread(**kwargs)
            elif action == "triage":
                return self._run_triage(request_info, **kwargs)
            elif action == "unsubscribe_scan":
                return self._run_unsubscribe_scan(request_info, **kwargs)
            else:
                return CommandResponse.error_response(
                    error_details=f"Unknown email action: {action}"
                )
        except Exception as e:
            logger.error("email command failed", action=action, error=str(e))
            return CommandResponse.error_response(
                error_details=str(e),
                context_data={"error": str(e)},
            )

    # -- Action implementations -------------------------------------------------

    def _get_service(self):
        """Construct the email service for the configured provider."""
        return create_email_service()

    def _run_list(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        max_results: int = kwargs.get("max_results") or 5
        service = self._get_service()
        emails = service.search("is:unread in:inbox", max_results=max_results)
        self._last_email_list = emails

        formatted = [
            {
                "index": i + 1,
                "id": e.id,
                "sender": e.sender_name,
                "subject": e.subject,
                "snippet": e.snippet,
                "date": e.date.isoformat(),
            }
            for i, e in enumerate(emails)
        ]

        ctx: dict[str, Any] = {"emails": formatted, "total_results": len(emails)}
        # Pre-route callers have no LLM downstream — pre-compose a spoken
        # summary so the wrapper sees a `message` and doesn't fall through
        # to the LLM path.
        if request_info.is_pre_routed:
            if not emails:
                ctx["message"] = "You have no unread emails."
            else:
                top = ", ".join(
                    f"{e.sender_name}: {e.subject}".strip().rstrip(":")
                    for e in emails[:3]
                )
                rest = (
                    f", and {len(emails) - 3} more" if len(emails) > 3 else ""
                )
                ctx["message"] = (
                    f"You have {len(emails)} unread email{'s' if len(emails) != 1 else ''}. {top}{rest}."
                )
        return CommandResponse.follow_up_response(context_data=ctx)

    def _run_read(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        email_index: int | None = kwargs.get("email_index")
        if email_index is None:
            return CommandResponse.error_response(
                error_details="Please specify which email to read (e.g. 'read email 1')."
            )

        msg = self._resolve_index(email_index)
        if isinstance(msg, CommandResponse):
            return msg

        service = self._get_service()
        full_email = service.fetch_message(msg.id, max_body_chars=3000)
        if not full_email:
            return CommandResponse.error_response(
                error_details="Could not fetch the email. It may have been deleted."
            )

        ctx: dict[str, Any] = {
            "email": {
                "sender": full_email.sender_name,
                "sender_email": full_email.sender,
                "subject": full_email.subject,
                "date": full_email.date.isoformat(),
                "body": full_email.body,
            }
        }
        if request_info.is_pre_routed:
            # Truncate body to ~600 chars so TTS doesn't read a 5-minute
            # email aloud — the user can request "read more" or open it on
            # their phone if they need the full text.
            body = (full_email.body or "").strip()
            body_short = body[:600] + ("…" if len(body) > 600 else "")
            ctx["message"] = (
                f"Email from {full_email.sender_name}, subject: {full_email.subject}. "
                f"{body_short}"
            ).strip()
        return CommandResponse.follow_up_response(context_data=ctx)

    def _run_search(self, **kwargs: Any) -> CommandResponse:
        query: str | None = kwargs.get("query")
        if not query:
            return CommandResponse.error_response(
                error_details="Please specify a search query (e.g. 'search for meeting notes')."
            )
        max_results: int = kwargs.get("max_results") or 5
        service = self._get_service()
        emails = service.search(query, max_results=max_results)
        self._last_email_list = emails

        formatted = [
            {
                "index": i + 1,
                "id": e.id,
                "sender": e.sender_name,
                "subject": e.subject,
                "snippet": e.snippet,
                "date": e.date.isoformat(),
            }
            for i, e in enumerate(emails)
        ]

        return CommandResponse.follow_up_response(
            context_data={
                "emails": formatted,
                "total_results": len(emails),
                "query": query,
            }
        )

    def _run_send(self, **kwargs: Any) -> CommandResponse:
        to: str | None = kwargs.get("to")
        body: str | None = kwargs.get("body")
        subject: str = kwargs.get("subject") or ""

        if not to:
            return CommandResponse.error_response(
                error_details="Missing 'to' address. Who should I send the email to?"
            )

        # LLMs often put short messages in subject but leave body empty
        if not body and subject:
            body = subject
        if not body:
            return CommandResponse.error_response(
                error_details="Missing email body. What should the email say?"
            )
        draft = {"to": to, "subject": subject, "body": body, "type": "send"}

        resp = CommandResponse.follow_up_response(
            context_data={
                "command_name": "email",
                "draft": draft,
                "preview": f"To: {to}\nSubject: {subject}\n\n{body}",
                "message": f"I've drafted an email to {to}. Tap Send in the app to confirm.",
                "inbox_title": f"Confirm: {subject}" if subject else f"Email to {to}",
                "inbox_summary": f"I've drafted an email to {to}. Tap Send in the app to confirm.",
            }
        )
        resp.actions = [
            IJarvisButton("Cancel", "cancel_click", "destructive", None, "Cancelled."),
            IJarvisButton("Send", "send_click", "primary", "send", "Email sent!"),
        ]
        return resp

    def _run_reply(self, **kwargs: Any) -> CommandResponse:
        email_index: int | None = kwargs.get("email_index")
        body: str | None = kwargs.get("body")

        if email_index is None:
            return CommandResponse.error_response(
                error_details="Please specify which email to reply to (e.g. 'reply to email 1')."
            )
        if not body:
            return CommandResponse.error_response(
                error_details="Missing reply body. What should I say?"
            )

        msg = self._resolve_index(email_index)
        if isinstance(msg, CommandResponse):
            return msg

        # Fetch full message for reply-to header
        service = self._get_service()
        full_email = service.fetch_message(msg.id, max_body_chars=500)
        if not full_email:
            return CommandResponse.error_response(
                error_details="Could not fetch the original email."
            )

        reply_to = extract_email(full_email.sender)
        reply_subject = (
            full_email.subject
            if full_email.subject.lower().startswith("re:")
            else f"Re: {full_email.subject}"
        )

        draft = {
            "message_id": msg.id,
            "thread_id": msg.thread_id,
            "to": reply_to,
            "subject": reply_subject,
            "body": body,
            "type": "reply",
        }

        resp = CommandResponse.follow_up_response(
            context_data={
                "command_name": "email",
                "draft": draft,
                "preview": f"Reply to: {full_email.sender_name}\nSubject: {reply_subject}\n\n{body}",
                "message": f"I've drafted a reply to {full_email.sender_name}. Tap Send in the app to confirm.",
                "inbox_title": f"Confirm: {reply_subject}" if reply_subject else f"Reply to {full_email.sender_name}",
                "inbox_summary": f"I've drafted a reply to {full_email.sender_name}. Tap Send in the app to confirm.",
            }
        )
        resp.actions = [
            IJarvisButton("Send", "send_click", "primary", "send", "Reply sent!"),
            IJarvisButton("Cancel", "cancel_click", "destructive", None, "Cancelled."),
        ]
        return resp

    def _run_archive(self, **kwargs: Any) -> CommandResponse:
        email_index: int | None = kwargs.get("email_index")
        if email_index is None:
            return CommandResponse.error_response(
                error_details="Please specify which email to archive (e.g. 'archive email 1')."
            )

        msg = self._resolve_index(email_index)
        if isinstance(msg, CommandResponse):
            return msg

        service = self._get_service()
        success = service.archive(msg.id)
        if not success:
            return CommandResponse.error_response(error_details="Failed to archive the email.")

        self._remove_from_cache(msg.id)
        return CommandResponse.final_response(
            context_data={"archived": True, "subject": msg.subject, "message": f"Archived: {msg.subject}"}
        )

    def _run_trash(self, **kwargs: Any) -> CommandResponse:
        email_index: int | None = kwargs.get("email_index")
        if email_index is None:
            return CommandResponse.error_response(
                error_details="Please specify which email to delete (e.g. 'delete email 1')."
            )

        msg = self._resolve_index(email_index)
        if isinstance(msg, CommandResponse):
            return msg

        service = self._get_service()
        success = service.trash(msg.id)
        if not success:
            return CommandResponse.error_response(error_details="Failed to delete the email.")

        self._remove_from_cache(msg.id)
        return CommandResponse.final_response(
            context_data={"trashed": True, "subject": msg.subject, "message": f"Deleted: {msg.subject}"}
        )

    def _run_star(self, **kwargs: Any) -> CommandResponse:
        email_index: int | None = kwargs.get("email_index")
        if email_index is None:
            return CommandResponse.error_response(
                error_details="Please specify which email to star (e.g. 'star email 1')."
            )

        msg = self._resolve_index(email_index)
        if isinstance(msg, CommandResponse):
            return msg

        service = self._get_service()
        success = service.star(msg.id)
        if not success:
            return CommandResponse.error_response(error_details="Failed to star the email.")

        return CommandResponse.final_response(
            context_data={"starred": True, "subject": msg.subject, "message": f"Starred: {msg.subject}"}
        )

    def _run_unstar(self, **kwargs: Any) -> CommandResponse:
        email_index: int | None = kwargs.get("email_index")
        if email_index is None:
            return CommandResponse.error_response(
                error_details="Please specify which email to unstar (e.g. 'unstar email 1')."
            )

        msg = self._resolve_index(email_index)
        if isinstance(msg, CommandResponse):
            return msg

        service = self._get_service()
        success = service.unstar(msg.id)
        if not success:
            return CommandResponse.error_response(error_details="Failed to unstar the email.")

        return CommandResponse.final_response(
            context_data={"unstarred": True, "subject": msg.subject, "message": f"Unstarred: {msg.subject}"}
        )

    def _run_mark_read(self, **kwargs: Any) -> CommandResponse:
        email_index: int | None = kwargs.get("email_index")
        if email_index is None:
            return CommandResponse.error_response(
                error_details="Please specify which email to mark as read (e.g. 'mark email 1 as read')."
            )

        msg = self._resolve_index(email_index)
        if isinstance(msg, CommandResponse):
            return msg

        service = self._get_service()
        success = service.mark_read(msg.id)
        if not success:
            return CommandResponse.error_response(error_details="Failed to mark the email as read.")

        return CommandResponse.final_response(
            context_data={"marked_read": True, "subject": msg.subject, "message": f"Marked as read: {msg.subject}"}
        )

    def _run_mark_unread(self, **kwargs: Any) -> CommandResponse:
        email_index: int | None = kwargs.get("email_index")
        if email_index is None:
            return CommandResponse.error_response(
                error_details="Please specify which email to mark as unread (e.g. 'mark email 1 as unread')."
            )

        msg = self._resolve_index(email_index)
        if isinstance(msg, CommandResponse):
            return msg

        service = self._get_service()
        success = service.mark_unread(msg.id)
        if not success:
            return CommandResponse.error_response(error_details="Failed to mark the email as unread.")

        return CommandResponse.final_response(
            context_data={"marked_unread": True, "subject": msg.subject, "message": f"Marked as unread: {msg.subject}"}
        )

    def _run_forward(self, **kwargs: Any) -> CommandResponse:
        email_index: int | None = kwargs.get("email_index")
        to: str | None = kwargs.get("to")
        note: str = kwargs.get("body") or ""

        if email_index is None:
            return CommandResponse.error_response(
                error_details="Please specify which email to forward (e.g. 'forward email 1 to john@example.com')."
            )
        if not to:
            return CommandResponse.error_response(
                error_details="Missing 'to' address. Who should I forward the email to?"
            )

        msg = self._resolve_index(email_index)
        if isinstance(msg, CommandResponse):
            return msg

        # Fetch the full original so the forward quotes its body
        service = self._get_service()
        full_email = service.fetch_message(msg.id, max_body_chars=3000)
        if not full_email:
            return CommandResponse.error_response(
                error_details="Could not fetch the original email."
            )

        fwd_subject = (
            full_email.subject
            if full_email.subject.lower().startswith("fwd:")
            else f"Fwd: {full_email.subject}"
        )
        quoted = (
            "---------- Forwarded message ----------\n"
            f"From: {full_email.sender}\n"
            f"Subject: {full_email.subject}\n\n"
            f"{full_email.body}"
        )
        body = f"{note}\n\n{quoted}" if note else quoted

        draft = {"to": to, "subject": fwd_subject, "body": body, "type": "send"}

        resp = CommandResponse.follow_up_response(
            context_data={
                "command_name": "email",
                "draft": draft,
                "preview": f"To: {to}\nSubject: {fwd_subject}\n\n{body}",
                "message": f"I've drafted a forward to {to}. Tap Send in the app to confirm.",
                "inbox_title": f"Confirm: {fwd_subject}",
                "inbox_summary": f"I've drafted a forward to {to}. Tap Send in the app to confirm.",
            }
        )
        resp.actions = [
            IJarvisButton("Cancel", "cancel_click", "destructive", None, "Cancelled."),
            IJarvisButton("Send", "send_click", "primary", "send", "Email sent!"),
        ]
        return resp

    def _run_triage(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        service = self._get_service()
        emails = service.search("is:unread in:inbox", max_results=_TRIAGE_MAX_RESULTS)
        if not emails:
            return CommandResponse.final_response(
                context_data={"message": "No unread emails to triage.", "total_results": 0}
            )

        metadata, _context = build_triage_payload(emails)
        n = len(emails)
        tag = JarvisInbox(self.command_name).post(
            title=f"Inbox triage — {n} unread",
            summary=f"{n} unread {'email' if n == 1 else 'emails'} ready to review.",
            body=build_triage_body(emails),
            category=InteractiveList.CATEGORY,
            metadata=metadata,
            user_id=request_info.user_id,
            create_push_notification=True,
            target_type="user" if request_info.user_id else "household",
        )
        if tag != "ok":
            return self._inbox_post_failure(tag)

        return CommandResponse.final_response(
            context_data={
                "message": (
                    f"I've sent a triage list of {n} "
                    f"{'email' if n == 1 else 'emails'} to your phone."
                ),
                "total_results": n,
            }
        )

    def _run_unsubscribe_scan(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        """Find never-read subscription senders and post an unsubscribe picker.

        Candidates = senders with >= 3 aged-unread messages in the last 90
        days. One full fetch per candidate (cap 15) reads the List-Unsubscribe
        headers; senders with no unsubscribe data at all are dropped. Each
        surviving candidate's actuation data is persisted for 24h so the
        unsubscribe_selected callback can act on it without re-scanning.
        """
        service = self._get_service()
        provider = get_email_provider()

        if provider == "gmail":
            emails = service.search(_UNSUB_GMAIL_QUERY, max_results=_UNSUB_MAX_RESULTS)
        else:
            # IMAP can't express older_than or categories — pull the 90-day
            # unread window and apply the 7-day "aged" cut client-side.
            emails = service.search(_UNSUB_IMAP_QUERY, max_results=_UNSUB_MAX_RESULTS)
            cutoff = datetime.now(timezone.utc) - timedelta(days=_UNSUB_AGED_DAYS)
            emails = [e for e in emails if _as_utc(e.date) < cutoff]

        # Group by sender address; preserve first-seen order within a count.
        groups: dict[str, list[EmailMessage]] = {}
        for e in emails:
            addr = extract_email(e.sender).strip().lower()
            if addr:
                groups.setdefault(addr, []).append(e)

        candidates = [
            (addr, msgs) for addr, msgs in groups.items()
            if len(msgs) >= _UNSUB_MIN_MESSAGES
        ]
        candidates.sort(key=lambda kv: len(kv[1]), reverse=True)
        candidates = candidates[:_UNSUB_MAX_CANDIDATES]

        now = datetime.now(timezone.utc)
        entries: list[tuple[str, str, int]] = []  # (addr, name, count)
        for addr, msgs in candidates:
            full = service.fetch_message(msgs[0].id)
            if not full:
                continue
            if not (full.unsubscribe_url or full.unsubscribe_mailto):
                continue  # no unsubscribe data at all — nothing we could actuate
            name = (msgs[0].sender_name or "").strip()[:120] or addr[:120]
            count = len(msgs)
            self._unsubscribe_storage.save(
                addr,
                {
                    "url": full.unsubscribe_url,
                    "mailto": full.unsubscribe_mailto,
                    "one_click": full.unsubscribe_one_click,
                    "count": count,
                    "name": name,
                },
                expires_at=now + timedelta(hours=_UNSUB_TTL_HOURS),
            )
            entries.append((addr, name, count))

        if not entries:
            return CommandResponse.final_response(
                context_data={"message": _UNSUB_EMPTY_TEXT, "total_results": 0}
            )

        rows = [
            InteractiveRow(
                key=addr,
                label=name,
                caption=f"{count} unread in the last 90 days",
                control="checkbox",
                default_selected=False,
            )
            for addr, name, count in entries
        ]
        payload = InteractiveList(
            command_name=self.command_name,
            sections=[InteractiveSection(rows=rows)],
            actions=[
                InteractiveAction(
                    label="Unsubscribe {n}",
                    callback="unsubscribe_selected",
                    style="destructive",
                ),
            ],
            empty_text=_UNSUB_EMPTY_TEXT,
        )

        n = len(entries)
        tag = JarvisInbox(self.command_name).post(
            title=f"Subscription cleanup — {n} senders",
            summary=f"{n} {'sender' if n == 1 else 'senders'} you never read.",
            body="\n".join(f"- {name}: {count} unread" for _, name, count in entries),
            category=InteractiveList.CATEGORY,
            metadata=payload.to_dict(),
            user_id=request_info.user_id,
            create_push_notification=True,
            target_type="user" if request_info.user_id else "household",
        )
        if tag != "ok":
            return self._inbox_post_failure(tag)

        return CommandResponse.final_response(
            context_data={
                "message": (
                    f"I found {n} {'sender' if n == 1 else 'senders'} you never read. "
                    "Check your phone to pick which to unsubscribe."
                ),
                "total_results": n,
            }
        )

    def _inbox_post_failure(self, tag: str) -> CommandResponse:
        """Map a JarvisInbox post failure tag to a spoken error response.

        Discriminated tags (not bool) so each failure mode gets a distinct
        voice response — same pattern as the shopping-list export command.
        """
        if tag == "no_backend":
            message = "I can't send lists to your phone from this device."
            details = "no inbox backend registered (JarvisInbox returned 'no_backend')"
        elif tag == "no_cc_url":
            message = "I can't find the server to send the list to your phone."
            details = "service discovery returned no command-center URL"
        elif tag == "invalid":
            message = "Something went wrong building the list for your phone."
            details = "inbox backend rejected the payload as invalid"
        else:
            message = "I couldn't reach the server right now."
            details = f"JarvisInbox.post returned '{tag}'"

        logger.error("email inbox post failed", reason=tag)
        return CommandResponse.error_response(
            error_details=details,
            context_data={"message": message},
        )

    # ── Triage callbacks: mobile checked emails, apply the chosen verb ─────

    @callback("triage_mark_read")
    def triage_mark_read(self, data: dict, request_info: RequestInformation) -> CommandResponse:
        return self._apply_triage("mark_read", data)

    @callback("triage_archive")
    def triage_archive(self, data: dict, request_info: RequestInformation) -> CommandResponse:
        return self._apply_triage("archive", data)

    @callback("triage_star")
    def triage_star(self, data: dict, request_info: RequestInformation) -> CommandResponse:
        return self._apply_triage("star", data)

    def _apply_triage(self, verb: str, data: dict) -> CommandResponse:
        """Apply a service verb to every selected message id in a triage batch.

        Per-id failures don't abort the batch — they're counted and reported.
        Subjects for detail_lines come from the payload's echoed context
        (set at build time by build_triage_payload); missing entries fall
        back to the bare message id.
        """
        keys = [
            str(entry.get("key") or "").strip()
            for entry in (data.get("selected") or [])
            if isinstance(entry, dict)
        ]
        keys = [k for k in keys if k]
        if not keys:
            return CommandResponse.final_response(
                context_data={"message": "Nothing selected."}
            )

        context = data.get("context") if isinstance(data.get("context"), dict) else {}
        subjects = context.get("subjects") if isinstance(context.get("subjects"), dict) else {}

        try:
            service = self._get_service()
        except ValueError as e:
            return CommandResponse.error_response(
                error_details=str(e),
                context_data={"message": "Email isn't configured on this device."},
            )

        apply_verb = getattr(service, verb)
        detail_lines: list[str] = []
        failed = 0
        for key in keys:
            try:
                ok = bool(apply_verb(key))
            except Exception as e:
                logger.error("email triage verb failed", verb=verb, message_id=key, error=str(e))
                ok = False
            if ok:
                detail_lines.append(str(subjects.get(key) or key))
            else:
                failed += 1

        message = _TRIAGE_RESULT_TEMPLATES[verb].format(n=len(detail_lines))
        if failed:
            message += f" {failed} failed."

        return CommandResponse.final_response(
            context_data={
                "message": message,
                "detail_lines": detail_lines,
                "applied": len(detail_lines),
                "failed": failed,
            }
        )

    # ── Unsubscribe callback: mobile picked senders, actuate per record ────

    @callback("unsubscribe_selected")
    def unsubscribe_selected(self, data: dict, request_info: RequestInformation) -> CommandResponse:
        """Unsubscribe from each selected sender using its stored record.

        Row keys are sender addresses; the scan persisted per-sender
        actuation data (url/mailto/one_click) with a 24h TTL. A missing or
        expired record counts as failed — the list is a snapshot, re-running
        the scan gets fresh records. Per-sender failures don't abort the
        batch.
        """
        keys = [
            str(entry.get("key") or "").strip()
            for entry in (data.get("selected") or [])
            if isinstance(entry, dict)
        ]
        keys = [k for k in keys if k]
        if not keys:
            return CommandResponse.final_response(
                context_data={"message": "Nothing selected."}
            )

        try:
            service = self._get_service()
        except ValueError as e:
            return CommandResponse.error_response(
                error_details=str(e),
                context_data={"message": "Email isn't configured on this device."},
            )

        detail_lines: list[str] = []
        done = 0
        for key in keys:
            record = self._unsubscribe_storage.get(key)
            if not record:
                detail_lines.append(f"{key} — expired — run the scan again")
                continue
            name = str(record.get("name") or key)
            outcome = self._actuate_unsubscribe(service, key, record)
            if outcome == "done":
                done += 1
                detail_lines.append(f"{name} — done")
            elif outcome == "manual":
                detail_lines.append(f"{name} — needs manual visit")
            else:
                detail_lines.append(f"{name} — failed")

        return CommandResponse.final_response(
            context_data={
                "message": f"Unsubscribed from {done} of {len(keys)}.",
                "detail_lines": detail_lines,
                "unsubscribed": done,
            }
        )

    def _actuate_unsubscribe(self, service, sender: str, record: dict) -> str:
        """Actuate one unsubscribe record. Returns "done" | "manual" | "failed".

        Order of preference:
        1. RFC 8058 one-click — POST to the URL; 2xx is success. A failed
           POST falls through to mailto when one exists.
        2. mailto — an unsubscribe email through the user's own send path.
        3. URL only — report "needs a manual visit"; never GET it.
        """
        url = str(record.get("url") or "")
        mailto = str(record.get("mailto") or "")
        one_click = bool(record.get("one_click"))

        if one_click and url:
            try:
                resp = requests.post(
                    url, data={"List-Unsubscribe": "One-Click"}, timeout=10
                )
                if 200 <= resp.status_code < 300:
                    return "done"
                logger.warning(
                    "one-click unsubscribe rejected",
                    sender=sender,
                    status_code=resp.status_code,
                )
            except Exception as e:
                logger.warning(
                    "one-click unsubscribe failed", sender=sender, error=str(e)
                )
            # fall through to mailto when the one-click POST didn't succeed

        if mailto:
            try:
                service.send(mailto, "unsubscribe", "Please unsubscribe this address.")
                return "done"
            except Exception as e:
                logger.error(
                    "mailto unsubscribe failed", sender=sender, error=str(e)
                )
                return "failed"

        if url and not one_click:
            return "manual"
        return "failed"

    # ── Smart-reply callbacks: Send/Ignore buttons on draft inbox items ────

    @callback("send_draft_reply")
    def send_draft_reply(self, data: dict, request_info: RequestInformation) -> CommandResponse:
        """Send a smart-reply draft on its thread, then best-effort mark read.

        Input data (set by the smart-reply agent at post time):
        ``{message_id, thread_id, body}`` — body is the draft text shown on
        the InboxDetail screen; the tap IS the confirmation.
        """
        message_id = str(data.get("message_id") or "").strip()
        thread_id = str(data.get("thread_id") or "").strip()
        body = data.get("body") or ""
        if not message_id or not body:
            return CommandResponse.error_response(
                error_details="Draft payload is missing message_id or body.",
                context_data={"message": "I couldn't send that reply — the draft is incomplete."},
            )

        try:
            service = self._get_service()
        except ValueError as e:
            return CommandResponse.error_response(
                error_details=str(e),
                context_data={"message": "Email isn't configured on this device."},
            )

        try:
            service.reply(message_id, thread_id, body)
        except Exception as e:
            logger.error("smart reply send failed", message_id=message_id, error=str(e))
            return CommandResponse.error_response(
                error_details=str(e),
                context_data={"message": "I couldn't send the reply."},
            )

        # Best-effort: clear the unread flag on the original. A failure here
        # never undoes a successful send.
        try:
            service.mark_read(message_id)
        except Exception as e:
            logger.warning("smart reply mark_read failed", message_id=message_id, error=str(e))

        return CommandResponse.final_response(context_data={"message": "Reply sent."})

    @callback("dismiss_draft")
    def dismiss_draft(self, data: dict, request_info: RequestInformation) -> CommandResponse:
        """Dismiss a smart-reply draft. No side effects — the chip just checks off."""
        return CommandResponse.final_response(context_data={"message": "Dismissed."})

    # -- Helpers ----------------------------------------------------------------

    def _resolve_index(self, email_index: int) -> EmailMessage | CommandResponse:
        """Resolve a 1-based index to an EmailMessage from the cache.

        Returns the EmailMessage on success, or a CommandResponse error.
        """
        if not self._last_email_list:
            return CommandResponse.error_response(
                error_details="No email list available. Try listing or searching emails first."
            )

        # Handle "last" (-1)
        if email_index == -1:
            email_index = len(self._last_email_list)

        if email_index < 1 or email_index > len(self._last_email_list):
            return CommandResponse.error_response(
                error_details=(
                    f"Email index {email_index} is out of range. "
                    f"You have {len(self._last_email_list)} emails in the current list."
                )
            )

        return self._last_email_list[email_index - 1]

    def _remove_from_cache(self, message_id: str) -> None:
        """Remove a message from the cached email list."""
        self._last_email_list = [e for e in self._last_email_list if e.id != message_id]
