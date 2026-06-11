"""EmailAlertAgent — proactive email notifications for VIP senders, urgent messages, and daily digest.

Runs every 5 minutes. Iterates the mailbox-configured users (mailbox secrets
are user-scoped, and agents have no ambient user — see
email_shared.user_resolution) and, with the SDK user ContextVar set to each
user in turn, fetches recent unread emails and generates alerts based on
three behaviors:
- VIP senders (priority 3) — configurable email list
- Urgent keywords (priority 2) — subject/snippet keyword matching
- Daily digest (priority 1) — morning summary of unread count + top senders,
  posted per user, once per day each

Does not run on startup to let TokenRefreshAgent warm up Gmail tokens first.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

try:
    from jarvis_log_client import JarvisLogger
    logger = JarvisLogger(service="jarvis-node")
except ImportError:
    import logging
    logger = logging.getLogger("jarvis-cmd-email")

from jarvis_command_sdk import (
    AgentSchedule,
    Alert,
    IJarvisAgent,
    IJarvisSecret,
    InteractiveList,
    JarvisInbox,
    JarvisSecret,
    JarvisStorage,
    set_current_user_id,
)

from email_shared.email_message import extract_email
from email_shared.email_service_factory import create_email_service, get_email_provider
from email_shared.triage import build_triage_body, build_triage_payload
from email_shared.user_resolution import find_configured_user_ids

_storage = JarvisStorage("email_alerts")

REFRESH_INTERVAL_SECONDS = 300  # 5 minutes
ALERT_TTL_HOURS = 8
MAX_ALERTS_PER_RUN = 5
MAX_DEDUP_CACHE = 200
MAX_USERS_PER_RUN = 5

DEFAULT_URGENT_KEYWORDS: set[str] = {
    "urgent",
    "asap",
    "emergency",
    "action required",
    "immediate",
    "critical",
    "deadline",
}


class EmailAlertAgent(IJarvisAgent):
    """Background agent that monitors Gmail for important emails."""

    def __init__(self) -> None:
        self._alerts: List[Alert] = []
        self._alerted_email_ids: set[str] = set()  # "{uid}:{email_id}" entries
        # Per-user digest guard: uid → ISO date string, e.g. "2026-03-15"
        self._last_digest_date: dict[int, str] = {}
        self._vip_senders: set[str] = set()  # cached from secrets

    @property
    def name(self) -> str:
        return "email_alerts"

    @property
    def description(self) -> str:
        return "Monitors Gmail for VIP emails, urgent messages, and daily digest"

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(
            interval_seconds=REFRESH_INTERVAL_SECONDS,
            run_on_startup=False,
        )

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret(
                "EMAIL_ALERT_VIP_SENDERS",
                "Comma-separated VIP email addresses for high-priority alerts",
                "integration",
                "string",
                required=False,
                is_sensitive=False,
                friendly_name="VIP Senders",
            ),
            JarvisSecret(
                "EMAIL_ALERT_URGENT_KEYWORDS",
                "Comma-separated keywords that trigger urgent email alerts",
                "integration",
                "string",
                required=False,
                is_sensitive=False,
                friendly_name="Urgent Keywords",
            ),
            JarvisSecret(
                "EMAIL_ALERT_DIGEST_HOUR",
                "Hour (0-23) for daily email digest (default: 7)",
                "integration",
                "int",
                required=False,
                is_sensitive=False,
                friendly_name="Digest Hour",
            ),
        ]

    def validate_secrets(self) -> List[str]:
        """Agent requires at least one user with usable email credentials.

        Mailbox secrets are user-scoped and agent discovery runs with no
        ambient user in the SDK ContextVar, so enumerate configured users
        node-side instead of relying on ContextVar-resolved reads (which
        always return None in this context).
        """
        if find_configured_user_ids():
            return []
        # No configured user (or not on a node) — report the missing creds
        # for the ambient provider, matching email_service_factory's branch:
        # every non-gmail provider uses the IMAP/SMTP code path.
        provider = get_email_provider()
        if provider != "gmail":
            missing: list[str] = []
            if not _storage.get_secret("IMAP_USERNAME"):
                missing.append("IMAP_USERNAME")
            if not _storage.get_secret("IMAP_PASSWORD"):
                missing.append("IMAP_PASSWORD")
            return missing
        # Gmail
        if not _storage.get_secret("GMAIL_ACCESS_TOKEN"):
            return ["GMAIL_ACCESS_TOKEN"]
        return []

    @property
    def include_in_context(self) -> bool:
        return False

    async def run(self) -> None:
        """Fetch recent unread emails and generate alerts, per configured user."""
        try:
            user_ids = find_configured_user_ids()
            if not user_ids:
                logger.debug("Email alert agent: no users with a configured mailbox")
                self._alerts = []
                return

            all_alerts: List[Alert] = []
            for uid in user_ids[:MAX_USERS_PER_RUN]:
                # Set the SDK user ContextVar so create_email_service() (and
                # every other user-scope secret read) resolves this user's
                # mailbox credentials.
                set_current_user_id(uid)
                try:
                    all_alerts.extend(self._run_for_user(uid))
                finally:
                    set_current_user_id(None)

            self._alerts = self._apply_rate_limit(all_alerts)

            # Trim dedup cache
            if len(self._alerted_email_ids) > MAX_DEDUP_CACHE:
                # Keep the most recent entries (arbitrary trim)
                excess = len(self._alerted_email_ids) - MAX_DEDUP_CACHE
                to_remove = list(self._alerted_email_ids)[:excess]
                for item in to_remove:
                    self._alerted_email_ids.discard(item)

            if self._alerts:
                logger.info("Email agent generated alerts", count=len(self._alerts))

        except Exception as e:
            logger.error("Email alert agent run failed", error=str(e))
            self._alerts = []

    def _run_for_user(self, uid: int) -> List[Alert]:
        """VIP / urgent / digest checks for one user's mailbox.

        Caller has already set the SDK user ContextVar to ``uid``.
        """
        try:
            service = create_email_service()
        except ValueError:
            return []

        emails = service.search("is:unread in:inbox newer_than:1d", max_results=20)

        # Load config from secrets (integration scope — shared across users)
        vip_senders = self._load_vip_senders()
        urgent_keywords = self._load_urgent_keywords()
        digest_hour = self._load_digest_hour()

        now = datetime.now(timezone.utc)
        alerts: List[Alert] = []

        for email in emails:
            # VIP check first (higher priority)
            alerts.extend(self._check_vip(email, vip_senders, uid))

            # Urgent keyword check (skips already-alerted emails)
            alerts.extend(self._check_urgent(email, urgent_keywords, uid))

        # Daily digest (per user, once per day each)
        alerts.extend(self._check_digest(emails, digest_hour, now, uid))
        return alerts

    def _load_vip_senders(self) -> set[str]:
        """Load VIP sender list from secrets, falling back to cached value."""
        raw = _storage.get_secret("EMAIL_ALERT_VIP_SENDERS")
        if raw:
            self._vip_senders = {
                addr.strip().lower() for addr in raw.split(",") if addr.strip()
            }
        return self._vip_senders

    def _load_urgent_keywords(self) -> set[str]:
        """Load urgent keywords from secrets or use defaults."""
        raw = _storage.get_secret("EMAIL_ALERT_URGENT_KEYWORDS")
        if raw:
            return {kw.strip().lower() for kw in raw.split(",") if kw.strip()}
        return DEFAULT_URGENT_KEYWORDS

    def _load_digest_hour(self) -> int:
        """Load digest hour from secrets or default to 7."""
        raw = _storage.get_secret("EMAIL_ALERT_DIGEST_HOUR")
        if raw:
            try:
                hour = int(raw)
                if 0 <= hour <= 23:
                    return hour
            except ValueError:
                pass
        return 7

    def _check_vip(self, email: Any, vip_senders: set[str], uid: int) -> List[Alert]:
        """Check if email is from a VIP sender. Returns 0 or 1 alerts."""
        if not vip_senders:
            return []

        if f"{uid}:{email.id}" in self._alerted_email_ids:
            return []

        sender_email = extract_email(email.sender).lower()
        if sender_email not in vip_senders:
            return []

        now = datetime.now(timezone.utc)
        self._alerted_email_ids.add(f"{uid}:{email.id}")

        return [Alert(
            source_agent=self.name,
            title=f"Email from {email.sender_name}",
            summary=f"{email.sender_name}: {email.subject}",
            created_at=now,
            expires_at=now + timedelta(hours=ALERT_TTL_HOURS),
            priority=3,
        )]

    def _check_urgent(self, email: Any, keywords: set[str], uid: int) -> List[Alert]:
        """Check if email subject/snippet contains urgent keywords. Returns 0 or 1 alerts."""
        if f"{uid}:{email.id}" in self._alerted_email_ids:
            return []

        text = f"{email.subject} {email.snippet}".lower()
        matched = any(kw in text for kw in keywords)

        if not matched:
            return []

        now = datetime.now(timezone.utc)
        self._alerted_email_ids.add(f"{uid}:{email.id}")

        return [Alert(
            source_agent=self.name,
            title=f"Urgent: {email.subject}",
            summary=f"From {email.sender_name}: {email.subject}",
            created_at=now,
            expires_at=now + timedelta(hours=ALERT_TTL_HOURS),
            priority=2,
        )]

    def _check_digest(
        self, emails: list[Any], digest_hour: int, now: datetime, uid: int
    ) -> List[Alert]:
        """Post the daily digest as an interactive triage list during the morning window.

        The digest is the same triage payload the email command's 'triage'
        action builds (shared via email_shared.triage), posted to the
        mailbox owner (once per day per user). When the inbox post fails,
        falls back to the legacy text Alert so the digest never silently
        disappears.
        """
        if not emails:
            return []

        # Only trigger during the configured hour
        if now.hour != digest_hour:
            return []

        today = now.strftime("%Y-%m-%d")
        if self._last_digest_date.get(uid) == today:
            return []

        self._last_digest_date[uid] = today

        metadata, _context = build_triage_payload(emails)
        tag = JarvisInbox("email").post(
            title=f"Daily email digest — {len(emails)} unread",
            summary=self._build_digest_summary(emails),
            body=build_triage_body(emails),
            category=InteractiveList.CATEGORY,
            metadata=metadata,
            user_id=uid,
            create_push_notification=True,
            target_type="user",
        )
        if tag == "ok":
            logger.info("Daily email digest posted as triage list", unread=len(emails))
            return []

        # Post failed — fall back to the legacy text digest Alert.
        logger.warning(
            "Daily digest inbox post failed — falling back to text alert", reason=tag
        )
        return [Alert(
            source_agent=self.name,
            title="Daily Email Digest",
            summary=self._build_digest_summary(emails),
            created_at=now,
            expires_at=now + timedelta(hours=ALERT_TTL_HOURS),
            priority=1,
        )]

    @staticmethod
    def _build_digest_summary(emails: list[Any]) -> str:
        """Unread count + top-3 senders line shared by the inbox post and the fallback Alert."""
        sender_counts: Counter[str] = Counter()
        for email in emails:
            sender_counts[email.sender_name] += 1

        top_senders = sender_counts.most_common(3)
        top_str = ", ".join(f"{name} ({count})" for name, count in top_senders)
        return f"{len(emails)} unread emails. Top senders: {top_str}"

    def _apply_rate_limit(self, alerts: List[Alert]) -> List[Alert]:
        """Cap alerts per run, keeping highest priority first."""
        if len(alerts) <= MAX_ALERTS_PER_RUN:
            return alerts
        # Sort by priority descending, take top N
        alerts.sort(key=lambda a: a.priority, reverse=True)
        return alerts[:MAX_ALERTS_PER_RUN]

    def get_context_data(self) -> Dict[str, Any]:
        return {}

    def get_alerts(self) -> List[Alert]:
        return list(self._alerts)
