"""Factory for constructing the configured email service backend.

Reads ``EMAIL_PROVIDER`` secret to decide between Gmail (REST API) and
IMAP/SMTP. Used by both ``email_command.py`` and ``email_alert_agent.py``
so provider selection is centralized.
"""

from jarvis_command_sdk import JarvisStorage

from .google_gmail_service import GoogleGmailService
from .imap_email_service import ImapEmailService

_storage = JarvisStorage("email")


def get_email_provider() -> str:
    """Return the configured email provider name (lowercase)."""
    return (_storage.get_secret("EMAIL_PROVIDER", scope="user") or "gmail").lower()


def create_email_service() -> GoogleGmailService | ImapEmailService:
    """Construct the email service matching ``EMAIL_PROVIDER`` secret.

    Returns:
        A ``GoogleGmailService`` (default) or ``ImapEmailService`` instance.

    Raises:
        ValueError: If required secrets are missing for the chosen provider.
    """
    provider = get_email_provider()

    # All non-gmail providers use the IMAP/SMTP code path
    if provider != "gmail":
        username = _storage.get_secret("IMAP_USERNAME", scope="user")
        password = _storage.get_secret("IMAP_PASSWORD", scope="user")
        if not username or not password:
            raise ValueError("IMAP_USERNAME and IMAP_PASSWORD secrets are required for IMAP provider")

        return ImapEmailService(
            imap_host=_storage.get_secret("IMAP_HOST", scope="user") or "localhost",
            imap_port=int(_storage.get_secret("IMAP_PORT", scope="user") or "1143"),
            smtp_host=_storage.get_secret("SMTP_HOST", scope="user") or "localhost",
            smtp_port=int(_storage.get_secret("SMTP_PORT", scope="user") or "1025"),
            username=username,
            password=password,
            use_ssl=(_storage.get_secret("IMAP_USE_SSL", scope="user") or "false").lower() == "true",
            archive_folder=_storage.get_secret("IMAP_ARCHIVE_FOLDER", scope="user") or "Archive",
            trash_folder=_storage.get_secret("IMAP_TRASH_FOLDER", scope="user") or "Trash",
        )

    # Default: Gmail (tokens are per-user, client_id is shared)
    access_token = _storage.get_secret("GMAIL_ACCESS_TOKEN", scope="user")
    refresh_token = _storage.get_secret("GMAIL_REFRESH_TOKEN", scope="user")
    client_id = _storage.get_secret("GMAIL_CLIENT_ID")

    return GoogleGmailService(
        access_token=access_token or "",
        refresh_token=refresh_token or "",
        client_id=client_id or "",
    )
