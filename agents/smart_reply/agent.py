"""SmartReplyAgent — LLM-drafted replies for important unread email.

Runs every 5 minutes. The whole feature is OFF until the user sets the
EMAIL_NOTIFICATION_FILTER secret (their free-text importance rule). Each run
iterates the mailbox-configured users (mailbox secrets are user-scoped, and
agents have no ambient user — see email_shared.user_resolution; the optional
EMAIL_AGENT_USER identity pins the agent to ONE household member instead, and
targets the outage notice at them) and, with the SDK user ContextVar set to
each user in turn:

1. Search unread inbox mail from the last day, drop already-drafted ids and
   bulk/automated senders (deterministic screen — see email_shared.senders;
   they never reach the LLM and never get drafts). When the user's own
   address is known, candidates where the user is only CC'd are also dropped
   deterministically (email_shared.reply_rubric.is_direct_recipient).
2. LLM call 1 — email_shared.reply_rubric.select_reply_worthy: the built-in
   BASELINE_RULES rubric + the user's instructions applied ON TOP, judged in
   one fail-closed strict-indices call (same discipline as the news alerts
   agent).
3. LLM call 2 per match — write a brief draft reply, or decline
   (should_reply=false) for mail that needs no response.
4. Post the draft to the inbox (InboxDetail surface) with Send/Ignore
   buttons dispatching to the email command's send_draft_reply /
   dismiss_draft callbacks. Nothing is ever auto-sent — the tap IS the
   confirmation, with the draft on screen.

Connection failures (EmailConnectionError from search OR the per-match fetch)
are recorded in email_shared.connection_health — never read as "no
candidates". Health is aggregated PER TICK: each user's loop pass yields a
connection outcome, and after the loop the tracker is called at most once —
any outage reports a failure (a healthy mailbox must not clear a broken
mailbox's streak), otherwise any reachable mailbox records success. Three
consecutive failure TICKS post ONE cross-agent outage notice per day; the
exception never escapes run(), so the scheduler keeps probing every tick.

Dedup is PERSISTENT: JarvisStorage records keyed by "{user_id}:{message_id}"
with a 7-day TTL, so drafts survive restarts. The dedup, the draft LLM call,
and the inbox post builder live in email_shared.reply_drafts — SHARED with the
email_alerts agent's VIP/urgent path so the two can never double-post the same
message. Posts are marked via mark_posted; stage-2 declines (should_reply
false / parse_fail) via mark_declined — this agent skips anything already
HANDLED (posted or declined), while the email_alerts gate only honors real
posts, so a decline here can never suppress a VIP/urgent notification.
"""

from typing import Any, Dict, List

try:
    from jarvis_log_client import JarvisLogger
    logger = JarvisLogger(service="jarvis-node")
except ImportError:
    import logging
    logger = logging.getLogger("jarvis-cmd-email")

from jarvis_command_sdk import (
    AgentSchedule,
    IJarvisAgent,
    IJarvisSecret,
    JarvisSecret,
    JarvisStorage,
    set_current_user_id,
)

from email_shared.connection_health import record_success, report_connection_failure
from email_shared.email_message import EmailConnectionError
from email_shared.email_service_factory import create_email_service, get_email_provider
from email_shared.senders import is_bulk_sender
from email_shared.reply_drafts import (
    DRAFT_BODY_CHARS,
    already_handled,
    generate_draft_status,
    mark_declined,
    mark_posted,
    post_reply_item,
)
from email_shared.reply_rubric import (
    is_direct_recipient,
    resolve_user_address,
    select_reply_worthy,
)
from email_shared.user_resolution import (
    configured_agent_user_id,
    find_configured_user_ids,
    resolve_agent_user_ids,
)

# Secret reads are global by (key, scope); dedup records share the same
# namespace via email_shared.reply_drafts.
_storage = JarvisStorage("email_smart_reply")

REFRESH_INTERVAL_SECONDS = 300  # 5 minutes
SEARCH_QUERY = "is:unread in:inbox newer_than:1d"
SEARCH_MAX_RESULTS = 20
MAX_DRAFTS_PER_RUN = 2  # global cap across all users per run
MAX_USERS_PER_RUN = 5


class SmartReplyAgent(IJarvisAgent):
    """Background agent that drafts replies for filter-matched unread email."""

    @property
    def name(self) -> str:
        return "smart_reply"

    @property
    def description(self) -> str:
        return "Drafts replies for important unread emails matching the user's filter"

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
                "EMAIL_NOTIFICATION_FILTER",
                "Free-text instructions for which emails deserve a drafted "
                "reply (e.g. 'clients, invoices, anything from my kid's "
                "school'). Applied ON TOP of the built-in rules: real humans "
                "only, addressed to you directly, and the email actually "
                "warrants a response. Leave blank to disable smart replies.",
                "integration",
                "string",
                required=False,
                is_sensitive=False,
                friendly_name="Smart Reply Filter",
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
        """Filter unread email and post draft replies, per configured user."""
        try:
            # The module-level find_configured_user_ids is passed explicitly
            # so the EMAIL_AGENT_USER identity check sees the same lookup
            # tests patch on this module.
            user_ids = resolve_agent_user_ids(find_configured_user_ids)
            if not user_ids:
                logger.debug("Smart reply agent: no users with a configured mailbox")
                return

            filter_text = self._read_filter()
            if not filter_text:
                return  # feature is OFF until the user writes a rule

            posted = 0
            outage: str | None = None
            reachable = False
            for uid in user_ids[:MAX_USERS_PER_RUN]:
                if posted >= MAX_DRAFTS_PER_RUN:
                    break  # the per-run draft cap is global across users
                # Set the SDK user ContextVar so create_email_service() (and
                # every other user-scope secret read) resolves this user's
                # mailbox credentials.
                set_current_user_id(uid)
                try:
                    user_posted, failure, ok = self._run_for_user(
                        uid, filter_text, MAX_DRAFTS_PER_RUN - posted
                    )
                    posted += user_posted
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

            if posted:
                logger.info("Smart reply agent posted drafts", count=posted)

        except Exception as e:
            logger.error("Smart reply agent run failed", error=str(e))

    def _run_for_user(
        self, uid: int, filter_text: str, budget: int
    ) -> tuple[int, str | None, bool]:
        """Run the draft flow for one user's mailbox.

        Caller has already set the SDK user ContextVar to ``uid``. Returns
        ``(drafts_posted, connection_failure, reachable)`` — the connection
        outcome feeds run()'s per-tick health aggregation instead of hitting
        the tracker directly (one broken mailbox among N must not be masked
        or multiply-counted). ``connection_failure`` is the failure
        description (search OR per-match fetch), ``reachable`` is True only
        when the mailbox was actually probed successfully; both are falsy
        when no service could be constructed (neutral — nothing was probed).
        """
        try:
            service = create_email_service()
        except ValueError:
            return 0, None, False

        try:
            emails = service.search(SEARCH_QUERY, max_results=SEARCH_MAX_RESULTS)
        except EmailConnectionError as e:
            # Mailbox unreachable — must never read as "no candidates".
            # Swallowing here keeps the scheduler's 3-strike auto-disable
            # from tripping so we keep probing every tick.
            return 0, e.description, False

        candidates = [e for e in emails if not self._already_drafted(uid, e.id)]

        # Deterministic bulk-sender screen BEFORE the stage-1 LLM filter. A
        # field incident saw a marketing email get a drafted reply — the LLM
        # filter alone is insufficient. Bulk senders never reach the LLM and
        # never get drafts.
        non_bulk = [e for e in candidates if not is_bulk_sender(e)]
        if len(non_bulk) < len(candidates):
            logger.info(
                "Smart reply: screened bulk senders before LLM filter",
                screened=len(candidates) - len(non_bulk),
                remaining=len(non_bulk),
            )
        candidates = non_bulk

        # Deterministic directness screen: when the user's own address is
        # known, mail where they're only CC'd (or part of a list blast) never
        # reaches the LLM. Unknown address defers entirely to the LLM judge.
        user_address = resolve_user_address()
        if user_address:
            direct = [e for e in candidates if is_direct_recipient(e, user_address)]
            if len(direct) < len(candidates):
                logger.info(
                    "Smart reply: dropped CC-only candidates before LLM judge",
                    dropped=len(candidates) - len(direct),
                    remaining=len(direct),
                )
            candidates = direct
        if not candidates:
            return 0, None, True

        # Stage 1 — single fail-closed LLM call judging the built-in rubric
        # plus the user's instructions (email_shared.reply_rubric).
        matched = select_reply_worthy(candidates, filter_text, user_address)[:budget]

        posted = 0
        for email in matched:
            if posted >= budget:
                break
            try:
                full = service.fetch_message(
                    email.id, max_body_chars=DRAFT_BODY_CHARS
                )
            except EmailConnectionError as e:
                # Connection died mid-run (or a fetch-only outage: search
                # cached/cheap but fetch unreachable). Stop THIS user, leave
                # the email unmarked so it retries next tick, and surface
                # the failure as this user's outcome — persistent fetch-only
                # outages must accumulate to the notice threshold too.
                return posted, e.description, False
            if not full:
                continue  # transient fetch failure — retry next run
            status, draft = self._generate_draft(full)
            if status == "no_llm":
                # LLM unreachable — fail closed for the rest of this run,
                # retry on the next one.
                break
            if status != "ok":
                # Parse fail / should_reply false — mark DECLINED so we don't
                # burn LLM calls re-judging the same email forever, without
                # blocking the email_alerts VIP/urgent gate (already_posted
                # ignores declines — a decline must never suppress delivery).
                mark_declined(uid, full.id)
                continue
            tag = self._post_draft(full, draft, uid)
            if tag == "ok":
                self._mark_drafted(uid, full.id)
                posted += 1
            else:
                # Not marked drafted — transient post failures retry next run.
                logger.warning(
                    "Smart reply inbox post failed",
                    reason=tag,
                    message_id=full.id,
                )
        return posted, None, True

    # ── Filter (LLM call 1) — email_shared.reply_rubric.select_reply_worthy ─

    def _read_filter(self) -> str:
        """Return the user's EMAIL_NOTIFICATION_FILTER value (empty string if unset)."""
        return (_storage.get_secret("EMAIL_NOTIFICATION_FILTER") or "").strip()

    # ── Draft generation (LLM call 2) — shared with email_alerts ───────────

    def _generate_draft(self, email: Any) -> tuple[str, str]:
        """Draft a reply to one email. Returns ``(status, draft)``.

        Statuses (from email_shared.reply_drafts.generate_draft_status):
        - ``"ok"`` — draft is usable
        - ``"no_llm"`` — LLM unreachable/empty (transient; do NOT mark drafted)
        - ``"parse_fail"`` — malformed output (mark drafted, don't retry forever)
        - ``"no_reply"`` — model says the email needs no response (mark drafted)
        """
        return generate_draft_status(email)

    # ── Inbox post — shared builder with email_alerts ───────────────────────

    def _post_draft(self, email: Any, draft: str, uid: int) -> str:
        """Post one draft to the user's inbox with Send/Ignore buttons. Returns the post tag."""
        return post_reply_item(
            uid,
            email,
            draft,
            title=f"Reply ready — {email.sender_name}",
            reason="filter",
        )

    # ── Persistent dedup (shared namespace with email_alerts) ───────────────

    def _already_drafted(self, uid: int, message_id: str) -> bool:
        # Posted OR declined — declined mail isn't re-judged (no repeated
        # LLM burn) but stays eligible for the email_alerts VIP/urgent gate.
        return already_handled(uid, message_id)

    def _mark_drafted(self, uid: int, message_id: str) -> None:
        mark_posted(uid, message_id)

    def get_context_data(self) -> Dict[str, Any]:
        return {}
