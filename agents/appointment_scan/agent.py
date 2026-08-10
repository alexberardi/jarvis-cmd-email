"""AppointmentScanAgent — turn appointment emails into tap-to-confirm calendar cards.

This is the GENERIC "an agent proposes another package's command" pilot. Runs
every 5 minutes. For each mailbox-configured user (mailbox secrets are
user-scoped and agents have no ambient user — see email_shared.user_resolution;
the optional EMAIL_AGENT_USER identity pins the agent to ONE household member
and targets its cards at them), with the SDK user ContextVar set to each user
in turn:

1. Search unread inbox mail from the last day; drop ids already handled this
   week (persistent per-user dedup) BEFORE any fetch/LLM so a re-scan never
   re-bothers the user.
2. Fetch each candidate and run ONE fail-closed LLM extraction (same JSON
   discipline as email_shared.reply_drafts: <think>/code-fence stripping,
   first-object regex, non-dict/exception → "no appointment"). Anything short
   of a concrete title + start ISO timestamp is treated as "no appointment".
3. On a real appointment, post a tap-to-confirm card via
   JarvisInbox.propose_action — proposing the calendar command's create_event
   action (target_command="add_event", action="create_event"). The tap routes
   to command-center's generic server-plane dispatcher
   (jarvis.proposable_action.execute), which validates the proposal against
   add_event's declared ProposableAction and runs the callback on the node.
   This agent does NOT own add_event — that's the whole point of the pilot.

The household-level fail-closed enable gate for proposals (`proposals.enabled`)
is the DISPATCHER's responsibility (command-center, server-side) — a card for a
disabled household is posted but refused on tap. We deliberately do NOT read
that gate here: the node can't authoritatively evaluate a server-side
household setting, and duplicating it would drift.
# TODO(pilot): optional node-side pre-gate — if/when a cheap local read of the
# household proposals toggle exists, short-circuit here to avoid posting cards
# that the dispatcher will only refuse. Until then the server gate is authoritative.

Connection failures (EmailConnectionError from search OR the per-candidate
fetch) are swallowed so the scheduler keeps probing every tick — a mailbox
outage must never read as "no appointments". Dedup is PERSISTENT: JarvisStorage
records keyed by "{user_id}:{message_id}" in the agent's own
"email_appointment_scan" namespace with a 7-day TTL, so proposed appointments
survive restarts and aren't re-proposed. run() never raises.
"""

import json
import re
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
    IJarvisAgent,
    IJarvisSecret,
    JarvisInbox,
    JarvisSecret,
    JarvisStorage,
    set_current_user_id,
)

from email_shared.email_message import EmailConnectionError, extract_email
from email_shared.email_service_factory import create_email_service, get_email_provider
from email_shared.reply_drafts import strip_llm_noise
from email_shared.user_resolution import (
    find_configured_user_ids,
    resolve_agent_user_ids,
)

# Own dedup namespace — independent of the shared reply-draft namespace so an
# appointment proposal and a smart-reply draft for the same message don't
# collide.
_storage = JarvisStorage("email_appointment_scan")

REFRESH_INTERVAL_SECONDS = 300  # 5 minutes
SEARCH_QUERY = "is:unread in:inbox newer_than:1d"
SEARCH_MAX_RESULTS = 20
FETCH_BODY_CHARS = 1000  # enough for the appointment details; keep LLM input small
MAX_PROPOSALS_PER_RUN = 3  # global cap across all users per run
MAX_USERS_PER_RUN = 5
DEDUP_TTL_DAYS = 7

# The calendar package's proposable command + action (its @callback name). The
# dispatcher validates that add_event actually declares create_event as a
# ProposableAction — this agent just names the intent.
TARGET_COMMAND = "add_event"
TARGET_ACTION = "create_event"

_EXTRACT_SYSTEM = (
    "You extract a single calendar appointment from an email, if one exists. "
    "An appointment is a specific dated event the recipient is expected to "
    "attend or that is scheduled for them: a meeting, reservation, medical or "
    "dental visit, interview, class, service window, or booking, with a "
    "concrete date AND time.\n"
    "- Newsletters, marketing, receipts, shipping updates with no delivery "
    "window, and vague 'let's find a time' mail have NO appointment — set "
    "has_appointment to false.\n"
    "- Resolve any relative date against the email's own Date/received context "
    "and output ABSOLUTE ISO 8601 timestamps (e.g. 2026-08-12T15:00:00). If "
    "you cannot determine a concrete start date and time, set has_appointment "
    "to false.\n"
    "- If no explicit end time is given, leave end as an empty string.\n"
    "- title is a short human label (e.g. 'Dentist cleaning', 'Table for 4 at "
    "Luigi's'). location is empty when unknown.\n"
    'Output ONLY JSON: {"has_appointment": true|false, "title": "...", '
    '"start": "ISO8601", "end": "ISO8601 or empty", "location": "..."} — '
    "no prose, no code fences, no explanation."
)


def _min_one_hour_end(start_iso: str, end_iso: str) -> str:
    """Guarantee at least a 1-hour event. The extractor often invents a short,
    odd end (e.g. a 47-minute duration) instead of leaving it empty, so honor an
    explicit end only when it's at least an hour after start; otherwise default
    to start + 1h (what calendar apps do). Returns an ISO string; if start can't
    be parsed, leaves the end as-is and lets add_event's own default backstop."""
    try:
        s = datetime.fromisoformat(start_iso)
    except (ValueError, TypeError):
        return end_iso
    e = None
    if end_iso:
        try:
            e = datetime.fromisoformat(end_iso)
        except (ValueError, TypeError):
            e = None
    if e is None or e < s + timedelta(hours=1):
        e = s + timedelta(hours=1)
    return e.isoformat()


class AppointmentScanAgent(IJarvisAgent):
    """Background agent that proposes calendar events extracted from unread mail."""

    @property
    def name(self) -> str:
        return "appointment_scan"

    @property
    def description(self) -> str:
        return (
            "Scans unread email for appointments and proposes them as "
            "tap-to-confirm calendar events"
        )

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(
            interval_seconds=REFRESH_INTERVAL_SECONDS,
            run_on_startup=False,
        )

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return [
            # Reuse the SAME identity secret the other email agents declare, so
            # the household configures "which member" once for all of them.
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

        Mirrors the smart_reply/email_alerts discovery check: mailbox secrets
        are user-scoped and discovery runs with no ambient user, so enumerate
        configured users node-side rather than relying on ContextVar reads.
        """
        if find_configured_user_ids():
            return []
        provider = get_email_provider()
        if provider != "gmail":
            missing: list[str] = []
            if not _storage.get_secret("IMAP_USERNAME"):
                missing.append("IMAP_USERNAME")
            if not _storage.get_secret("IMAP_PASSWORD"):
                missing.append("IMAP_PASSWORD")
            return missing
        if not _storage.get_secret("GMAIL_ACCESS_TOKEN"):
            return ["GMAIL_ACCESS_TOKEN"]
        return []

    @property
    def include_in_context(self) -> bool:
        return False

    async def run(self) -> None:
        """Scan unread mail and propose calendar events, per configured user."""
        try:
            # Pass the module-level find_configured_user_ids explicitly so the
            # EMAIL_AGENT_USER identity check sees the same lookup tests patch.
            user_ids = resolve_agent_user_ids(find_configured_user_ids)
            if not user_ids:
                logger.debug("Appointment scan: no users with a configured mailbox")
                return

            proposed = 0
            for uid in user_ids[:MAX_USERS_PER_RUN]:
                if proposed >= MAX_PROPOSALS_PER_RUN:
                    break  # per-run proposal cap is global across users
                # Set the SDK user ContextVar so create_email_service() (and
                # every user-scope secret read) resolves this user's mailbox.
                set_current_user_id(uid)
                try:
                    proposed += self._run_for_user(
                        uid, MAX_PROPOSALS_PER_RUN - proposed
                    )
                finally:
                    set_current_user_id(None)

            if proposed:
                logger.info("Appointment scan proposed calendar events", count=proposed)

        except Exception as e:
            logger.error("Appointment scan run failed", error=str(e))

    def _run_for_user(self, uid: int, budget: int) -> int:
        """Scan one user's mailbox and propose up to ``budget`` events.

        Caller has already set the SDK user ContextVar to ``uid``. Returns the
        number of proposals posted. Connection failures are swallowed (the
        scheduler keeps probing); nothing is marked seen unless a card actually
        posts, so transient failures retry next tick.
        """
        try:
            service = create_email_service()
        except ValueError:
            return 0

        try:
            emails = service.search(SEARCH_QUERY, max_results=SEARCH_MAX_RESULTS)
        except EmailConnectionError as e:
            # Mailbox unreachable — must never read as "no appointments".
            logger.warning("Appointment scan: search failed", error=e.description)
            return 0

        # 'Never suggest this again' blocklist for this user (fail-open on fetch).
        signals = self._fetch_suppression_signals(uid)
        blocked_sources = {s for s in signals.get("source_keys", []) if s}
        descriptors = signals.get("descriptors", [])

        # Screen BEFORE any fetch/LLM: drop already-seen mail AND mail whose
        # sender the user has hard-blocked (no fetch, no LLM spend on those).
        candidates = [
            e
            for e in emails
            if not self._already_seen(uid, e.id) and not self._is_blocked_source(e, blocked_sources)
        ]

        proposed = 0
        for email in candidates:
            if proposed >= budget:
                break
            try:
                full = service.fetch_message(email.id, max_body_chars=FETCH_BODY_CHARS)
            except EmailConnectionError as e:
                # Connection died mid-run — stop THIS user, leave the email
                # unmarked so it retries next tick.
                logger.warning("Appointment scan: fetch failed", error=e.description)
                break
            if not full:
                continue  # transient fetch miss — retry next run
            # descriptors are injected as negative examples so the LLM also skips
            # look-alikes of things the user blocked (not just exact senders).
            appt = self._extract_appointment(full, descriptors)
            if appt is None:
                # No appointment (or LLM unreachable). Do NOT mark seen —
                # extraction is fail-closed, so retry on a later tick.
                continue
            tag = self._propose(full, appt, uid)
            if tag == "ok":
                self._mark_seen(uid, full.id)
                proposed += 1
            else:
                # Transient post failure — leave unmarked so it retries.
                logger.warning(
                    "Appointment scan: proposal post failed",
                    reason=tag,
                    message_id=full.id,
                )
        return proposed

    # ── Fail-closed LLM extraction (mirrors reply_drafts.generate_draft_status) ─

    def _extract_appointment(
        self, email: Any, descriptors: list[str] | None = None
    ) -> dict[str, str] | None:
        """Extract a single appointment from one email. ``None`` = no appointment.

        Fail-closed: a missing LLM client, an empty/garbled response, a
        non-dict, has_appointment=false, or a result missing a concrete title
        or start all collapse to ``None`` — nothing is ever proposed on a
        shaky extraction. ``descriptors`` are the user's 'never suggest this'
        examples, injected so the LLM also skips look-alikes.
        """
        try:
            from services.node_llm_client import ask_llm
        except ImportError:
            logger.warning("Appointment scan: ask_llm unavailable (fail-closed)")
            return None

        prompt = (
            f"From: {email.sender}\n"
            f"Subject: {email.subject}\n\n"
            f"{email.body or email.snippet}"
        )

        # Anchor relative dates to a CORRECT "today" — the model otherwise
        # invents today's date (seen: it thought today was months off) and then
        # "corrects" an explicit date to match the stated weekday (8/11 -> 8/13).
        today = datetime.now().strftime("%A, %B %d, %Y")
        system = (
            _EXTRACT_SYSTEM
            + f"\n\nTODAY is {today}. Resolve any relative date against TODAY. If "
            "the email gives an explicit calendar date (e.g. 'August 11th'), use "
            "that exact date verbatim — do NOT shift it to match a stated weekday."
        )
        if descriptors:
            blocked = "\n".join(f"- {d}" for d in descriptors[:20])
            system += (
                "\n\nThe user has asked you to STOP proposing certain things. If "
                "this email is one of these — or clearly the same kind — set "
                f"has_appointment to false:\n{blocked}"
            )

        raw = ask_llm(prompt, system=system) or ""
        if not raw:
            logger.warning(
                "Appointment scan: empty LLM response (fail-closed)",
                message_id=email.id,
            )
            return None

        cleaned = strip_llm_noise(raw)
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)

        try:
            parsed = json.loads(cleaned)
            if not isinstance(parsed, dict):
                raise ValueError("expected a JSON object")
        except Exception as e:
            logger.warning(
                "Appointment scan: parse failed",
                error=str(e),
                raw=raw[:200],
                message_id=email.id,
            )
            return None

        if not parsed.get("has_appointment"):
            return None

        title = str(parsed.get("title") or "").strip()
        start = str(parsed.get("start") or "").strip()
        if not title or not start:
            # Need at least a label and a concrete start to build an event.
            return None
        return {
            "title": title,
            "start": start,
            # The model is unreliable at end times — enforce a sensible >= 1h
            # duration rather than trusting an invented short end.
            "end": _min_one_hour_end(start, str(parsed.get("end") or "").strip()),
            "location": str(parsed.get("location") or "").strip(),
        }

    # ── Proposal (the generic "agent proposes another command" call) ────────────

    def _propose(self, email: Any, appt: dict[str, str], uid: int) -> str:
        """Post a tap-to-confirm card proposing add_event.create_event. Returns the tag."""
        idempotency_key = f"appt:{email.id}"
        # 'Never suggest this' inputs: a deterministic source key (the sender's
        # address) for a hard block, and a semantic descriptor the extractor
        # injects as a negative example to also skip look-alikes.
        source = extract_email(email.sender) or (email.sender or "")
        descriptor = f'appointment emails like "{email.subject}" from {email.sender}'
        # Show the source email on the card so the user can verify the extracted
        # title/time/location against what the message actually said.
        excerpt = (getattr(email, "body", "") or getattr(email, "snippet", "") or "").strip()
        if len(excerpt) > 600:
            excerpt = excerpt[:600].rstrip() + "…"
        card_body = f"From: {email.sender}\n\n{excerpt}".rstrip()
        return JarvisInbox("appointment_scan").propose_action(
            target_command=TARGET_COMMAND,
            action=TARGET_ACTION,
            params={
                "title": appt["title"],
                "start": appt["start"],
                "end": appt["end"],
                "location": appt["location"],
                # Rides into add_event's params under its idempotency_param so a
                # double-tap / retry can't create the event twice.
                "idempotency_key": idempotency_key,
            },
            idempotency_key=idempotency_key,
            editable=["title", "start", "end", "location"],
            # Mobile renders start/end as date/time pickers (falls back to a text
            # box on older builds that don't know input_type=datetime).
            field_types={"start": "datetime", "end": "datetime"},
            title="Add to your calendar?",
            summary=email.subject,
            body=card_body,
            source=source,
            descriptor=descriptor,
            user_id=uid,
            create_push_notification=True,
        )

    # ── 'Never suggest this again' blocklist (fetched from command-center) ──────

    def _fetch_suppression_signals(self, uid: int) -> dict:
        """Fetch this user's blocklist signals for add_event. Fail-open: any
        error (including the node helper being unavailable) → empty signals, so
        a fetch miss never stops suggestions."""
        try:
            from services.proposal_client import get_suppression_signals
        except ImportError:
            return {"source_keys": [], "descriptors": []}
        try:
            return get_suppression_signals(TARGET_COMMAND, uid) or {
                "source_keys": [],
                "descriptors": [],
            }
        except Exception as e:  # noqa: BLE001 — fail-open
            logger.warning("Appointment scan: suppression fetch failed", error=str(e))
            return {"source_keys": [], "descriptors": []}

    def _is_blocked_source(self, email: Any, blocked_sources: set) -> bool:
        """True if the email's sender is on the user's hard blocklist — matched
        on the parsed address (and the raw From as a fallback)."""
        if not blocked_sources:
            return False
        addr = extract_email(email.sender) or ""
        return addr in blocked_sources or (email.sender or "") in blocked_sources

    # ── Persistent per-user dedup ("{uid}:{message_id}", 7-day TTL) ─────────────

    def _already_seen(self, uid: int, message_id: str) -> bool:
        return _storage.get(f"{uid}:{message_id}") is not None

    def _mark_seen(self, uid: int, message_id: str) -> None:
        now = datetime.now(timezone.utc)
        _storage.save(
            f"{uid}:{message_id}",
            {"proposed_at": now.isoformat()},
            expires_at=now + timedelta(days=DEDUP_TTL_DAYS),
        )

    def get_context_data(self) -> Dict[str, Any]:
        return {}
