"""EmailAlertAgent — proactive email notifications for VIP senders, urgent messages, and daily digest.

Runs every 5 minutes. Iterates the mailbox-configured users (mailbox secrets
are user-scoped, and agents have no ambient user — see
email_shared.user_resolution; the optional EMAIL_AGENT_USER identity pins
the agent to ONE household member instead, and targets the outage notice at
them) and, with the SDK user ContextVar set to each user in turn, fetches
recent unread emails and reacts to three triggers:

- VIP senders / urgent keywords — posted as drafted inbox+push reply items
  via the shared email_shared.reply_drafts surface (same one the smart_reply
  agent uses). KEY ASYMMETRY vs the smart_reply filter path: these triggers
  are deterministic, so the item posts even when no draft could be generated
  (LLM down, or the model says no reply is warranted) — the draft is
  enrichment, never a gate. Dedup is the PERSISTENT shared namespace, so the
  filter path and this path can never double-post the same message.
- Daily digest — morning interactive triage list per user, once per day each,
  with the legacy text-Alert fallback when the inbox post fails (get_alerts()
  still serves that fallback).

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

from email_shared.connection_health import record_success, report_connection_failure
from email_shared.email_message import EmailConnectionError, extract_email
from email_shared.email_service_factory import create_email_service, get_email_provider
from email_shared.reply_drafts import (
    already_posted,
    generate_reply_draft,
    mark_posted,
    post_reply_item,
)
from email_shared.reply_rubric import is_direct_recipient, resolve_user_address
from email_shared.senders import is_bulk_sender
from email_shared.triage import build_triage_body, build_triage_payload
from email_shared.user_resolution import (
    configured_agent_user_id,
    find_configured_user_ids,
    resolve_agent_user_ids,
)

_storage = JarvisStorage("email_alerts")

REFRESH_INTERVAL_SECONDS = 300  # 5 minutes
ALERT_TTL_HOURS = 8
MAX_ALERTS_PER_RUN = 5
MAX_REPLY_POSTS_PER_RUN = 3  # VIP + urgent combined, across all users per run
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
            JarvisSecret(
                "EMAIL_AGENT_USER",
                "The household member this agent runs as and notifies. "
                "Leave unset to run for every member with a configured mailbox.",
                "integration",
                "user",
                required=False,
                is_sensitive=False,
                friendly_name="Notify user",
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
        """Fetch recent unread emails and react to triggers, per configured user."""
        try:
            # The module-level find_configured_user_ids is passed explicitly
            # so the EMAIL_AGENT_USER identity check sees the same lookup
            # tests patch on this module.
            user_ids = resolve_agent_user_ids(find_configured_user_ids)
            if not user_ids:
                logger.debug("Email alert agent: no users with a configured mailbox")
                self._alerts = []
                return

            all_alerts: List[Alert] = []
            reply_budget = MAX_REPLY_POSTS_PER_RUN
            outage: str | None = None
            reachable = False
            for uid in user_ids[:MAX_USERS_PER_RUN]:
                # Set the SDK user ContextVar so create_email_service() (and
                # every other user-scope secret read) resolves this user's
                # mailbox credentials.
                set_current_user_id(uid)
                try:
                    alerts, posted, failure, ok = self._run_for_user(
                        uid, reply_budget
                    )
                    all_alerts.extend(alerts)
                    reply_budget -= posted
                    if failure is not None:
                        outage = failure
                    elif ok:
                        reachable = True
                finally:
                    set_current_user_id(None)

            # Per-TICK health aggregation — the tracker is called AT MOST
            # once per tick so N broken mailboxes are one failure (the
            # debounce threshold means ticks, not users) and a healthy
            # mailbox can never clear a broken mailbox's failure streak.
            # With an explicit EMAIL_AGENT_USER identity the outage notice
            # targets that user; otherwise it stays household-wide.
            if outage is not None:
                report_connection_failure(outage, user_id=configured_agent_user_id())
            elif reachable:
                record_success()

            self._alerts = self._apply_rate_limit(all_alerts)

            if self._alerts:
                logger.info("Email agent generated alerts", count=len(self._alerts))

        except Exception as e:
            logger.error("Email alert agent run failed", error=str(e))
            self._alerts = []

    def _run_for_user(
        self, uid: int, reply_budget: int
    ) -> tuple[List[Alert], int, str | None, bool]:
        """VIP / urgent / digest checks for one user's mailbox.

        Caller has already set the SDK user ContextVar to ``uid``. Returns
        ``(digest_alerts, reply_posts_made, connection_failure, reachable)``
        — VIP/urgent matches post inbox items directly (never Alerts); only
        the digest fallback still emits Alerts. The connection outcome feeds
        run()'s per-tick health aggregation instead of hitting the tracker
        directly (one broken mailbox among N must not be masked or
        multiply-counted): ``connection_failure`` is the failure description,
        ``reachable`` is True only when the mailbox was actually probed
        successfully; both are falsy when no service could be constructed
        (neutral — nothing was probed).
        """
        try:
            service = create_email_service()
        except ValueError:
            return [], 0, None, False

        try:
            emails = service.search("is:unread in:inbox newer_than:1d", max_results=20)
        except EmailConnectionError as e:
            # Mailbox unreachable — must never read as "no unread mail".
            # Swallowing here keeps the scheduler's 3-strike auto-disable
            # from tripping so we keep probing every tick.
            return [], 0, e.description, False

        # Load config from secrets (integration scope — shared across users)
        vip_senders = self._load_vip_senders()
        urgent_keywords = self._load_urgent_keywords()
        digest_hour = self._load_digest_hour()

        now = datetime.now(timezone.utc)

        # VIP first, then urgent — posted as drafted reply items.
        posted = self._post_reply_items(
            uid, service, emails, vip_senders, urgent_keywords, reply_budget
        )

        # Daily digest (per user, once per day each)
        return self._check_digest(emails, digest_hour, now, uid), posted, None, True

    def _post_reply_items(
        self,
        uid: int,
        service: Any,
        emails: list[Any],
        vip_senders: set[str],
        urgent_keywords: set[str],
        budget: int,
    ) -> int:
        """Post VIP/urgent matches as drafted inbox+push items. Returns posts made.

        VIP matches are processed first, then urgent; an email matching both
        is posted once (as VIP). KEY ASYMMETRY vs the smart_reply filter
        path: these are deterministic triggers, so the item posts even when
        ``generate_reply_draft`` produced nothing (LLM down / no reply
        warranted) — the draft is enrichment, never a gate. Urgent matches
        only get the draft attempt when the sender is not bulk
        (email_shared.senders) AND the user is a direct recipient
        (email_shared.reply_rubric) — otherwise they post with draft=None
        forced: never draft a reply to a no-reply/marketing sender or to
        mail where the user is merely CC'd. VIPs are user-listed and skip
        ALL screens (stage-2 should_reply still applies inside the draft
        call). Dedup (shared with smart_reply) is marked only on a
        successful post, so failed posts retry next tick.
        """
        vip = [e for e in emails if self._check_vip(e, vip_senders)]
        vip_ids = {e.id for e in vip}
        urgent = [
            e for e in emails
            if e.id not in vip_ids and self._check_urgent(e, urgent_keywords)
        ]

        queue = [(e, f"Email from {e.sender_name}", "vip") for e in vip] + [
            (e, f"Urgent: {e.subject}", "urgent") for e in urgent
        ]

        # Caller holds the per-uid ContextVar, so user scope resolves here.
        user_address = resolve_user_address()

        posted = 0
        for email, title, reason in queue:
            if posted >= budget:
                break
            if already_posted(uid, email.id):
                continue
            # VIP senders are explicitly user-listed — they skip ALL screens.
            # Urgent-keyword matches still post (an automated fraud alert
            # from noreply@bank must not be lost) but draft=None is forced —
            # skipping the draft LLM entirely — when the sender is bulk OR
            # the user isn't a direct recipient (only CC'd / list blast).
            if reason == "urgent" and (
                is_bulk_sender(email)
                or not is_direct_recipient(email, user_address)
            ):
                draft = None
            else:
                # Guarded: every failure mode (LLM down, fetch failure, no
                # reply warranted) yields draft=None — the post still happens.
                draft = generate_reply_draft(service, email)
            tag = post_reply_item(uid, email, draft, title=title, reason=reason)
            if tag == "ok":
                mark_posted(uid, email.id)
                posted += 1
            else:
                logger.warning(
                    "Email alert reply post failed — will retry next run",
                    reason=tag,
                    trigger=reason,
                    message_id=email.id,
                )
        return posted

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

    def _check_vip(self, email: Any, vip_senders: set[str]) -> bool:
        """True when the email's sender is on the VIP list."""
        if not vip_senders:
            return False
        return extract_email(email.sender).lower() in vip_senders

    def _check_urgent(self, email: Any, keywords: set[str]) -> bool:
        """True when the email subject/snippet contains an urgent keyword."""
        text = f"{email.subject} {email.snippet}".lower()
        return any(kw in text for kw in keywords)

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
