"""Shared reply-draft machinery for the smart_reply and email_alerts agents.

Three pieces live here so the two posting paths can't drift:

1. Draft generation — ``generate_draft_status()`` is the raw stage-2 LLM call
   (fail-closed JSON parse incl. think-tag/code-fence stripping) returning a
   discriminated status; ``generate_reply_draft()`` is the fetch+draft wrapper
   that collapses every non-ok outcome to ``None`` for callers where the draft
   is enrichment rather than a gate (the email_alerts VIP/urgent path).

2. The inbox post builder — ``post_reply_item()`` renders the shared
   From/Subject/snippet body and, ONLY when a draft exists, attaches the
   Send/Ignore buttons plus ``metadata.editable_text`` (the draft lives in
   the mobile editor, not the body). The alerts path posts draft-less items
   when the LLM is down (a deterministic trigger must never be lost); the
   smart_reply filter path never posts without a draft.

3. Persistent dedup — keys are ``"{uid}:{message_id}"`` in the JarvisStorage
   namespace ``"email_smart_reply"`` (kept from the original smart_reply agent
   so pre-refactor records still apply), 7-day TTL. SHARED so the filter path
   and the alert path can never double-post the same message. Records carry a
   ``posted`` flag distinguishing real posts from smart_reply declines
   (should_reply=false / parse_fail): ``already_posted()`` (the email_alerts
   gate) only suppresses on real posts — a decline must never swallow a
   VIP/urgent notification — while ``already_handled()`` (the smart_reply
   gate) suppresses on either so declined mail isn't re-judged every tick.
   Legacy records without the flag count as posted.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from jarvis_log_client import JarvisLogger
    logger = JarvisLogger(service="jarvis-node")
except ImportError:
    import logging
    logger = logging.getLogger("jarvis-cmd-email")

from jarvis_command_sdk import JarvisInbox, JarvisStorage

from .reply_rubric import BASELINE_RULES

# Dedup records live under this namespace. Kept as "email_smart_reply" (the
# original smart_reply agent's namespace) so existing records still dedup.
_storage = JarvisStorage("email_smart_reply")

DRAFT_BODY_CHARS = 3000
DEDUP_TTL_DAYS = 7
CATEGORY = "smart_reply"  # unknown to mobile → InboxDetail fallback (body + elements)

# The stage-2 prompt embeds the same BASELINE_RULES the stage-1 judge uses so
# should_reply decisions follow one rubric — the two stages can't drift.
_DRAFT_SYSTEM = (
    "You write brief reply drafts on behalf of the user. Read the email and "
    "draft a short reply the user could send as-is:\n"
    "- Plain text only. At most 150 words. No signature, no subject line, "
    "no markdown.\n"
    "- Be direct and polite; match the sender's tone.\n"
    "- If the email needs no response (newsletters, receipts, FYI-only "
    "mail), set should_reply to false.\n"
    "- Automated, transactional, marketing, or no-reply emails NEVER get a "
    'reply — set should_reply to false: {"should_reply": false, "draft": ""}.\n'
    "Apply this rubric — set should_reply to false unless the email passes "
    "ALL of it:\n"
    f"{BASELINE_RULES}\n"
    'Output ONLY JSON: {"should_reply": true|false, "draft": "..."} — '
    "no prose, no code fences, no explanation."
)


def strip_llm_noise(raw: str) -> str:
    """Strip <think> blocks and markdown code fences from an LLM response."""
    cleaned = re.sub(
        r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE
    ).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned


# ── Draft generation (stage-2 LLM call) ──────────────────────────────────────


def generate_draft_status(email: Any) -> tuple[str, str]:
    """Draft a reply to one (already fetched) email. Returns ``(status, draft)``.

    Statuses:
    - ``"ok"`` — draft is usable
    - ``"no_llm"`` — LLM unreachable/empty (transient; caller decides retry)
    - ``"parse_fail"`` — malformed output (don't retry forever)
    - ``"no_reply"`` — model says the email needs no response
    """
    try:
        from services.node_llm_client import ask_llm
    except ImportError:
        logger.warning("Reply draft: ask_llm unavailable (fail-closed)")
        return "no_llm", ""

    prompt = (
        f"From: {email.sender}\n"
        f"Subject: {email.subject}\n\n"
        f"{email.body or email.snippet}"
    )

    raw = ask_llm(prompt, system=_DRAFT_SYSTEM) or ""
    if not raw:
        logger.warning(
            "Reply draft: empty LLM response (fail-closed)",
            message_id=email.id,
        )
        return "no_llm", ""

    cleaned = strip_llm_noise(raw)
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
            "Reply draft: parse failed",
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


def generate_reply_draft(service: Any, email: Any) -> str | None:
    """Fetch the full message and draft a reply. ``None`` means no draft.

    Collapses every non-ok outcome — fetch failure, ask_llm unavailable,
    parse failure, should_reply=false — to ``None``, and never raises. For
    callers where the draft is enrichment, not a gate (email_alerts posts
    the item either way); the smart_reply filter path uses
    ``generate_draft_status()`` directly because it needs the transient/
    permanent distinction for its dedup-marking.
    """
    try:
        full = service.fetch_message(email.id, max_body_chars=DRAFT_BODY_CHARS)
        if not full:
            return None
        status, draft = generate_draft_status(full)
    except Exception as e:
        logger.warning(
            "Reply draft generation failed", error=str(e), message_id=email.id
        )
        return None
    return draft if status == "ok" else None


# ── Inbox post ────────────────────────────────────────────────────────────────


def post_reply_item(
    uid: int,
    email: Any,
    draft: str | None,
    *,
    title: str,
    reason: str,
) -> str:
    """Post one reply-surface item to the user's inbox. Returns the post tag.

    Body is From/Subject/snippet; when ``draft`` is not None the item also
    carries the Send/Ignore buttons plus ``metadata.editable_text`` — the
    draft lives ONLY in the mobile editor (seeded from ``initial``), not in
    the body. On tap, mobile substitutes the live editor text for
    ``data["body"]`` on elements whose data contains the ``data_key`` (Send),
    leaving the rest (Ignore) untouched. A draft-less item is informational —
    the user still gets the push + inbox entry even when the LLM couldn't
    produce a draft.

    Back-compat: older app builds without editable_text support will show NO
    draft text in the body but still have a Send chip carrying the original
    draft — acceptable for v0.1.x single-household; the body intentionally
    keeps From/Subject/snippet so the item is still informative.
    """
    body = (
        f"From: {email.sender}\n"
        f"Subject: {email.subject}\n\n"
        f"{email.snippet}"
    )
    elements: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None
    if draft is not None:
        metadata = {
            "editable_text": {
                "label": "Draft reply",
                "initial": draft,
                "data_key": "body",
            },
        }
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
    tag = JarvisInbox("email").post(
        title=title,
        summary=email.subject,
        body=body,
        category=CATEGORY,
        metadata=metadata,
        interactive_elements=elements,
        user_id=uid,
        create_push_notification=True,
        target_type="user",
    )
    if tag == "ok":
        logger.info(
            "Reply item posted",
            reason=reason,
            message_id=email.id,
            has_draft=draft is not None,
        )
    return tag


# ── Persistent dedup (keys are per-user: "{uid}:{message_id}") ───────────────


def already_posted(uid: int, message_id: str) -> bool:
    """True when this message was actually POSTED (by either agent) for this user.

    A smart_reply decline record (``posted: False``) does NOT count — the
    email_alerts VIP/urgent path must still be able to post. Legacy records
    without the ``posted`` flag default to True so they keep deduping.
    """
    record = _storage.get(f"{uid}:{message_id}")
    return record is not None and record.get("posted", True)


def already_handled(uid: int, message_id: str) -> bool:
    """True when this message was posted OR declined for this user.

    The smart_reply gate: declined mail isn't re-judged every tick (no
    repeated LLM burn), but it stays eligible for the email_alerts path.
    """
    return _storage.get(f"{uid}:{message_id}") is not None


def mark_posted(uid: int, message_id: str) -> None:
    """Record a successful post so neither agent re-posts within the TTL.

    Overwrites any earlier decline record — the message is now truly posted.
    """
    now = datetime.now(timezone.utc)
    _storage.save(
        f"{uid}:{message_id}",
        {"drafted_at": now.isoformat(), "posted": True},
        expires_at=now + timedelta(days=DEDUP_TTL_DAYS),
    )


def mark_declined(uid: int, message_id: str) -> None:
    """Record a smart_reply decline (should_reply=false / parse_fail).

    Suppresses re-judging via ``already_handled()`` but NOT the email_alerts
    VIP/urgent gate (``already_posted()``) — a decline is a "no draft
    warranted" verdict, never a delivery suppression. Same 7-day TTL.
    """
    now = datetime.now(timezone.utc)
    _storage.save(
        f"{uid}:{message_id}",
        {"declined_at": now.isoformat(), "posted": False},
        expires_at=now + timedelta(days=DEDUP_TTL_DAYS),
    )
