"""SmartReplyAgent — LLM-drafted replies for important unread email.

Runs every 5 minutes. The whole feature is OFF until the user sets the
EMAIL_NOTIFICATION_FILTER secret (their free-text importance rule). Each run
iterates the mailbox-configured users (mailbox secrets are user-scoped, and
agents have no ambient user — see email_shared.user_resolution) and, with the
SDK user ContextVar set to each user in turn:

1. Search unread inbox mail from the last day, drop already-drafted ids.
2. LLM call 1 — strict indices filter (fail-closed, same discipline as the
   news alerts agent): which emails match the user's rule.
3. LLM call 2 per match — write a brief draft reply, or decline
   (should_reply=false) for mail that needs no response.
4. Post the draft to the inbox (InboxDetail surface) with Send/Ignore
   buttons dispatching to the email command's send_draft_reply /
   dismiss_draft callbacks. Nothing is ever auto-sent — the tap IS the
   confirmation, with the draft on screen.

Dedup is PERSISTENT: JarvisStorage records keyed by "{user_id}:{message_id}"
with a 7-day TTL, so drafts survive restarts. (The alerts agent's in-memory
dedup re-alerts after a restart; a duplicate draft push would be far worse.)
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

from email_shared.email_service_factory import create_email_service, get_email_provider
from email_shared.user_resolution import find_configured_user_ids

# Records (drafted-id dedup) live under this namespace; secret reads are
# global by (key, scope) so the same facade serves both.
_storage = JarvisStorage("email_smart_reply")

REFRESH_INTERVAL_SECONDS = 300  # 5 minutes
SEARCH_QUERY = "is:unread in:inbox newer_than:1d"
SEARCH_MAX_RESULTS = 20
MAX_DRAFTS_PER_RUN = 2  # global cap across all users per run
MAX_USERS_PER_RUN = 5
DRAFT_BODY_CHARS = 3000
DEDUP_TTL_DAYS = 7
CATEGORY = "smart_reply"  # unknown to mobile → InboxDetail fallback (body + elements)

_DRAFT_SYSTEM = (
    "You write brief reply drafts on behalf of the user. Read the email and "
    "draft a short reply the user could send as-is:\n"
    "- Plain text only. At most 150 words. No signature, no subject line, "
    "no markdown.\n"
    "- Be direct and polite; match the sender's tone.\n"
    "- If the email needs no response (newsletters, receipts, FYI-only "
    "mail), set should_reply to false.\n"
    'Output ONLY JSON: {"should_reply": true|false, "draft": "..."} — '
    "no prose, no code fences, no explanation."
)


def _strip_llm_noise(raw: str) -> str:
    """Strip <think> blocks and markdown code fences from an LLM response."""
    cleaned = re.sub(
        r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE
    ).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned


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
                "Free-text rule for which emails deserve a drafted reply "
                "(e.g. 'clients, invoices, anything from my kid's school'). "
                "Leave blank to disable smart replies.",
                "integration",
                "string",
                required=False,
                is_sensitive=False,
                friendly_name="Smart Reply Filter",
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
            user_ids = find_configured_user_ids()
            if not user_ids:
                logger.debug("Smart reply agent: no users with a configured mailbox")
                return

            filter_text = self._read_filter()
            if not filter_text:
                return  # feature is OFF until the user writes a rule

            posted = 0
            for uid in user_ids[:MAX_USERS_PER_RUN]:
                if posted >= MAX_DRAFTS_PER_RUN:
                    break  # the per-run draft cap is global across users
                # Set the SDK user ContextVar so create_email_service() (and
                # every other user-scope secret read) resolves this user's
                # mailbox credentials.
                set_current_user_id(uid)
                try:
                    posted += self._run_for_user(
                        uid, filter_text, MAX_DRAFTS_PER_RUN - posted
                    )
                finally:
                    set_current_user_id(None)

            if posted:
                logger.info("Smart reply agent posted drafts", count=posted)

        except Exception as e:
            logger.error("Smart reply agent run failed", error=str(e))

    def _run_for_user(self, uid: int, filter_text: str, budget: int) -> int:
        """Run the draft flow for one user's mailbox. Returns drafts posted.

        Caller has already set the SDK user ContextVar to ``uid``.
        """
        try:
            service = create_email_service()
        except ValueError:
            return 0

        emails = service.search(SEARCH_QUERY, max_results=SEARCH_MAX_RESULTS)
        candidates = [e for e in emails if not self._already_drafted(uid, e.id)]
        if not candidates:
            return 0

        matched = self._filter_emails(filter_text, candidates)[:budget]

        posted = 0
        for email in matched:
            if posted >= budget:
                break
            full = service.fetch_message(email.id, max_body_chars=DRAFT_BODY_CHARS)
            if not full:
                continue  # transient fetch failure — retry next run
            status, draft = self._generate_draft(full)
            if status == "no_llm":
                # LLM unreachable — fail closed for the rest of this run,
                # retry on the next one.
                break
            if status != "ok":
                # Parse fail / should_reply false — mark drafted so we
                # don't burn LLM calls retrying the same email forever.
                self._mark_drafted(uid, full.id)
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
        return posted

    # ── Filter (LLM call 1) ────────────────────────────────────────────────

    def _read_filter(self) -> str:
        """Return the user's EMAIL_NOTIFICATION_FILTER value (empty string if unset)."""
        return (_storage.get_secret("EMAIL_NOTIFICATION_FILTER") or "").strip()

    def _filter_emails(self, filter_text: str, emails: List[Any]) -> List[Any]:
        """Ask the LLM which emails match the user's rule. Returns subset.

        Single LLM call returning only matching email numbers — keeps the
        decision deterministic to verify and prevents the model from
        smuggling in non-matching emails through composition.

        Fail-CLOSED contract: when a filter is set and we can't reliably
        determine matches (LLM unreachable, malformed output, no parseable
        indices), we return [] rather than drafting replies to mail the user
        didn't ask about. The next run will retry.
        """
        try:
            from services.node_llm_client import ask_llm
        except ImportError:
            logger.warning("Smart reply filter: ask_llm unavailable; skipping run (fail-closed)")
            return []

        if not emails:
            return []

        email_lines = []
        for i, e in enumerate(emails, start=1):
            email_lines.append(
                f"{i}. From: {e.sender}\n"
                f"   Subject: {e.subject}\n"
                f"   {(e.snippet or '').strip()[:300]}"
            )

        system = (
            "You are a strict email filter. Your only job is to identify "
            "which emails match the user's rule. Treat the rule as a HARD "
            "constraint:\n"
            "- Match only emails that CLEARLY and DIRECTLY satisfy the rule.\n"
            "- Tangential, adjacent, or 'kind of related' emails do NOT match.\n"
            "- When in doubt, SKIP the email. The cost of a false negative "
            "(missing one match) is much lower than a false positive "
            "(drafting a reply to an email the user explicitly asked not to "
            "be bothered about).\n"
            "- Do NOT rewrite, summarize, or compose anything. Output ONLY a "
            "JSON array of the matching email numbers."
        )

        prompt = (
            f'The user\'s rule:\n"""\n{filter_text}\n"""\n\n'
            f"Emails ({len(emails)} total):\n\n"
            + "\n\n".join(email_lines) +
            "\n\nReturn the numbers of emails that match the rule, as a JSON "
            'array. Example: [1, 4, 7]. If nothing matches, return: []. '
            "Output ONLY the array — no prose, no code fences, no explanation."
        )

        raw = ask_llm(prompt, system=system) or ""
        if not raw:
            logger.warning(
                "Smart reply filter: empty LLM response; skipping run (fail-closed)",
                email_count=len(emails),
            )
            return []

        cleaned = _strip_llm_noise(raw)
        # Find the first JSON array
        match = re.search(r"\[[^\[\]]*\]", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)

        try:
            parsed = json.loads(cleaned)
            if not isinstance(parsed, list):
                raise ValueError("expected a JSON array of indices")
        except Exception as e:
            logger.warning(
                "Smart reply filter: parse failed; skipping run (fail-closed)",
                error=str(e),
                raw=raw[:200],
            )
            return []

        matched: List[Any] = []
        for idx in parsed:
            try:
                i = int(idx)
            except (TypeError, ValueError):
                continue
            if 1 <= i <= len(emails):
                matched.append(emails[i - 1])

        logger.info(
            "Smart reply filter applied",
            input_emails=len(emails),
            matched=len(matched),
        )
        return matched

    # ── Draft generation (LLM call 2) ──────────────────────────────────────

    def _generate_draft(self, email: Any) -> tuple[str, str]:
        """Draft a reply to one email. Returns ``(status, draft)``.

        Statuses:
        - ``"ok"`` — draft is usable
        - ``"no_llm"`` — LLM unreachable/empty (transient; do NOT mark drafted)
        - ``"parse_fail"`` — malformed output (mark drafted, don't retry forever)
        - ``"no_reply"`` — model says the email needs no response (mark drafted)
        """
        try:
            from services.node_llm_client import ask_llm
        except ImportError:
            logger.warning("Smart reply draft: ask_llm unavailable (fail-closed)")
            return "no_llm", ""

        prompt = (
            f"From: {email.sender}\n"
            f"Subject: {email.subject}\n\n"
            f"{email.body or email.snippet}"
        )

        raw = ask_llm(prompt, system=_DRAFT_SYSTEM) or ""
        if not raw:
            logger.warning(
                "Smart reply draft: empty LLM response (fail-closed)",
                message_id=email.id,
            )
            return "no_llm", ""

        cleaned = _strip_llm_noise(raw)
        # Find the first JSON object
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)

        try:
            parsed = json.loads(cleaned)
            if not isinstance(parsed, dict):
                raise ValueError("expected a JSON object")
        except Exception as e:
            logger.warning(
                "Smart reply draft: parse failed",
                error=str(e),
                raw=raw[:200],
                message_id=email.id,
            )
            return "parse_fail", ""

        if not parsed.get("should_reply"):
            return "no_reply", ""

        draft = str(parsed.get("draft") or "").strip()
        if not draft:
            return "parse_fail", ""
        return "ok", draft

    # ── Inbox post ─────────────────────────────────────────────────────────

    def _post_draft(self, email: Any, draft: str, uid: int) -> str:
        """Post one draft to the user's inbox with Send/Ignore buttons. Returns the post tag."""
        body = (
            f"From: {email.sender}\n"
            f"Subject: {email.subject}\n\n"
            f"{email.snippet}\n\n"
            "— Draft reply —\n\n"
            f"{draft}"
        )
        elements = [
            {
                "id": f"send-{email.id}",
                "label": "Send reply",
                "kind": "send",
                "command": "email",
                "callback": "send_draft_reply",
                "data": {
                    "message_id": email.id,
                    "thread_id": email.thread_id,
                    "body": draft,
                },
                "navigation_type": "stack",
            },
            {
                # No navigation_type ⇒ new_notification fire-and-forget;
                # the chip just checks off.
                "id": f"ignore-{email.id}",
                "label": "Ignore",
                "command": "email",
                "callback": "dismiss_draft",
                "data": {"message_id": email.id},
            },
        ]
        return JarvisInbox("email").post(
            title=f"Reply ready — {email.sender_name}",
            summary=email.subject,
            body=body,
            category=CATEGORY,
            interactive_elements=elements,
            user_id=uid,
            create_push_notification=True,
            target_type="user",
        )

    # ── Persistent dedup (keys are per-user: "{uid}:{message_id}") ─────────

    def _already_drafted(self, uid: int, message_id: str) -> bool:
        return _storage.get(f"{uid}:{message_id}") is not None

    def _mark_drafted(self, uid: int, message_id: str) -> None:
        now = datetime.now(timezone.utc)
        _storage.save(
            f"{uid}:{message_id}",
            {"drafted_at": now.isoformat()},
            expires_at=now + timedelta(days=DEDUP_TTL_DAYS),
        )

    def get_context_data(self) -> Dict[str, Any]:
        return {}
