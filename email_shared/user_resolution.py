"""Resolve which users the background email agents run for.

ALL mailbox secrets (EMAIL_PROVIDER, IMAP_*, GMAIL_*) are USER-scoped, and
background agents run with no user in the SDK ContextVar — every
JarvisStorage user-scope read returns None in agent/discovery context. So
agents enumerate the user-scope secret rows node-side and check each
candidate user's credentials explicitly.

On top of that auto-discovery sits the optional EMAIL_AGENT_USER identity
(integration scope, value_type "user" — the mobile app renders a
household-member picker and stores the selected member's user id as a
string): when set, the agents run as and notify exactly that user, so a
multi-user household never gets notifications fanned to everyone or to the
wrong person. An EXPLICIT identity must never silently fall back to other
users — a misconfigured value means the agents idle (with a warning).
"""

from collections.abc import Callable

try:
    from jarvis_log_client import JarvisLogger
    logger = JarvisLogger(service="jarvis-node")
except ImportError:
    import logging
    logger = logging.getLogger("jarvis-cmd-email")

from jarvis_command_sdk import JarvisStorage

# Integration scope — readable in agent context (no ambient user required).
_storage = JarvisStorage("email")

AGENT_USER_KEY = "EMAIL_AGENT_USER"

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


def configured_agent_user_id() -> int | None:
    """The explicit EMAIL_AGENT_USER identity as an int, or None.

    None when the secret is unset/blank OR doesn't parse as a user id —
    callers use this only for TARGETING (e.g. the connection-outage notice);
    whether the agents run at all is resolve_agent_user_ids()'s call.
    """
    raw = (_storage.get_secret(AGENT_USER_KEY) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def resolve_agent_user_ids(
    configured: Callable[[], list[int]] | None = None,
) -> list[int]:
    """User ids the background agents should run for.

    EMAIL_AGENT_USER set → exactly that user, and ONLY when they have a
    configured mailbox: an explicit identity must never silently fall back
    to other users, so an unparseable value or one without a mailbox means
    [] (agents idle) plus a warning. Unset/blank → the existing auto
    behavior, every mailbox-configured user — zero-config single-user
    households are unchanged.

    ``configured`` overrides the mailbox lookup; the agents pass their own
    module-level ``find_configured_user_ids`` reference so tests that patch
    it on the agent module keep working.
    """
    lookup = configured if configured is not None else find_configured_user_ids
    raw = (_storage.get_secret(AGENT_USER_KEY) or "").strip()
    if not raw:
        return lookup()
    try:
        uid = int(raw)
    except ValueError:
        logger.warning(
            f"EMAIL_AGENT_USER is set to {raw!r} but that isn't a user id — agents idle"
        )
        return []
    if uid in lookup():
        return [uid]
    logger.warning(
        f"EMAIL_AGENT_USER is set to {uid} but that user has no configured "
        "mailbox — agents idle"
    )
    return []
