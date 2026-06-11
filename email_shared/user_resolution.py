"""Resolve which users have a usable mailbox configuration.

ALL mailbox secrets (EMAIL_PROVIDER, IMAP_*, GMAIL_*) are USER-scoped, and
background agents run with no user in the SDK ContextVar — every
JarvisStorage user-scope read returns None in agent/discovery context. So
agents enumerate the user-scope secret rows node-side and check each
candidate user's credentials explicitly.
"""

try:
    from jarvis_log_client import JarvisLogger
    logger = JarvisLogger(service="jarvis-node")
except ImportError:
    import logging
    logger = logging.getLogger("jarvis-cmd-email")

# Any of these rows existing for a user marks them as a mailbox candidate.
_CANDIDATE_KEYS = {"EMAIL_PROVIDER", "IMAP_USERNAME", "GMAIL_ACCESS_TOKEN"}


def find_configured_user_ids() -> list[int]:
    """User ids with a usable mailbox config. Agent context has no ambient
    user, so enumerate user-scope secret rows node-side."""
    try:
        from services.secret_service import get_all_secrets, get_secret_value
    except ImportError:
        return []  # not on a node (e.g. Pantry container test)

    try:
        candidates: set[int] = set()
        for row in get_all_secrets("user"):
            if row.key in _CANDIDATE_KEYS and row.user_id is not None:
                candidates.add(row.user_id)

        usable: list[int] = []
        for uid in candidates:
            provider = (
                get_secret_value("EMAIL_PROVIDER", "user", user_id=uid) or ""
            ).strip().lower() or "gmail"
            if provider != "gmail":
                # All non-gmail providers (proton/yahoo/outlook/fastmail/imap)
                # use the IMAP/SMTP code path — match email_service_factory.
                if get_secret_value(
                    "IMAP_USERNAME", "user", user_id=uid
                ) and get_secret_value("IMAP_PASSWORD", "user", user_id=uid):
                    usable.append(uid)
            elif get_secret_value("GMAIL_ACCESS_TOKEN", "user", user_id=uid):
                usable.append(uid)
        return sorted(usable)
    except Exception as e:  # never raise from agent discovery
        logger.warning("Email mailbox user resolution failed", error=str(e))
        return []
