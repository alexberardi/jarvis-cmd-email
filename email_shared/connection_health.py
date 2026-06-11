"""Persistent email-connection health tracking shared by BOTH background agents.

Field incident: the Proton Bridge died for a WEEK silently — search() returned
[] so the agents saw zero candidates and nothing surfaced anywhere. Now the
services raise EmailConnectionError (email_shared.email_message) and the agents
report failures here. After 3 CONSECUTIVE failures the user gets ONE inbox+push
notice per day.

The counter and the notice record live in JarvisStorage (namespace
"email_health") so they survive restarts AND act as a cross-agent lock: the
email_alerts and smart_reply agents share one counter, and whichever crosses
the threshold first writes the notice record — the other sees it and stays
quiet. ``record_success()`` clears both so a later, separate outage re-alerts.
"""

from datetime import datetime, timedelta, timezone

try:
    from jarvis_log_client import JarvisLogger
    logger = JarvisLogger(service="jarvis-node")
except ImportError:
    import logging
    logger = logging.getLogger("jarvis-cmd-email")

from jarvis_command_sdk import JarvisInbox, JarvisStorage

_storage = JarvisStorage("email_health")

FAILURE_THRESHOLD = 3  # consecutive failures before the user is told
NOTICE_TTL_HOURS = 24  # at most one notice per day (TTL-expired record re-arms)

_COUNTER_KEY = "consecutive_failures"
_NOTICE_KEY = "outage_notice"

NOTICE_TITLE = "Email connection problem"


def record_failure(error_text: str) -> bool:
    """Record one connection failure. True exactly when a notice should post.

    Returns True only when the persistent consecutive-failure count has
    reached FAILURE_THRESHOLD and no live notice record exists; on True the
    notice record is written with a 24h TTL — so across restarts and across
    BOTH agents, at most one True per day per outage.
    """
    record = _storage.get(_COUNTER_KEY) or {}
    try:
        count = int(record.get("count") or 0) + 1
    except (TypeError, ValueError):
        count = 1
    now = datetime.now(timezone.utc)
    _storage.save(
        _COUNTER_KEY,
        {
            "count": count,
            "last_error": error_text,
            "last_failure_at": now.isoformat(),
        },
    )
    if count < FAILURE_THRESHOLD:
        return False
    if _storage.get(_NOTICE_KEY) is not None:
        return False  # already told the user within the last 24h
    _storage.save(
        _NOTICE_KEY,
        {"posted_at": now.isoformat(), "error": error_text},
        expires_at=now + timedelta(hours=NOTICE_TTL_HOURS),
    )
    return True


def record_success() -> None:
    """Clear the failure counter AND the notice — a later outage re-alerts."""
    _storage.delete(_COUNTER_KEY)
    _storage.delete(_NOTICE_KEY)


def report_connection_failure(description: str, user_id: int | None = None) -> bool:
    """record_failure + post the once-per-day outage notice when it crosses.

    Shared by both agents so the notice copy can't drift. Never raises —
    callers sit inside agent run() loops that must keep probing every tick.
    Returns True when a notice was posted this call.

    When ``user_id`` is set (the explicit EMAIL_AGENT_USER identity), the
    notice posts user-targeted — the rest of the household never hears about
    a mailbox that isn't theirs. Otherwise it stays household-wide as before.
    """
    try:
        if not record_failure(description):
            return False
        tag = JarvisInbox("email").post(
            title=NOTICE_TITLE,
            summary=description,
            body=(
                f"I can't reach the email server ({description}). "
                "Email alerts, smart replies and digests are paused until it recovers."
            ),
            category="general",
            user_id=user_id,
            create_push_notification=True,
            target_type="user" if user_id is not None else "household",
        )
        if tag != "ok":
            # The user never saw anything — don't burn the once-per-day
            # notice on a failed post. The record still served as the
            # cross-agent lock during the attempt; delete it so the next
            # failure tick retries the post.
            _storage.delete(_NOTICE_KEY)
            logger.error(
                "Email outage notice post failed — will retry next failure tick",
                reason=tag,
            )
            return False
        logger.warning(
            "Email connection outage notice posted",
            description=description,
            tag=tag,
        )
        return True
    except Exception as e:  # never break the agent tick over health bookkeeping
        logger.error("Email connection health reporting failed", error=str(e))
        return False
