"""Layered reply-worthiness rubric — built-in rules + the user's instructions.

The drafting decision is judged by the LLM against BUILT-IN rules with the
user's free-text instructions applied ON TOP — never keyword-contains. Urgent
KEYWORDS still decide notification delivery deterministically (email_alerts);
this rubric decides DRAFTING only.

Three pieces:

1. ``BASELINE_RULES`` — the built-in rubric as system-prompt text. Also
   embedded in the stage-2 draft prompt (email_shared.reply_drafts) so
   should_reply decisions follow the same rules.
2. ``resolve_user_address`` / ``is_direct_recipient`` — deterministic
   "addressed to me directly?" screen using the To header. Conservative:
   unknown address or missing To header defers to the LLM (never false-drop).
3. ``select_reply_worthy`` — the SINGLE stage-1 LLM call (news-agent
   fail-closed style: import-guarded ask_llm, strict judge, JSON array of
   indices, think-tag/code-fence stripping, out-of-range dropped, any failure
   returns []).
"""

import json
import re
from typing import Any

try:
    from jarvis_log_client import JarvisLogger
    logger = JarvisLogger(service="jarvis-node")
except ImportError:
    import logging
    logger = logging.getLogger("jarvis-cmd-email")

from jarvis_command_sdk import JarvisStorage

# Built-in reply-worthiness rubric, written as system-prompt text. The user's
# free-text instructions apply ON TOP of these — they can narrow further or
# call out specific senders/topics, but the baseline always holds.
BASELINE_RULES = (
    "An email is reply-worthy ONLY if ALL of these hold:\n"
    "1. It is from an actual human — automated, transactional, marketing, "
    "and no-reply senders are never reply-worthy.\n"
    "2. It is addressed to the user DIRECTLY — the user's address is in the "
    "To header. Merely being CC'd does not count, and a mailing-list blast "
    "does not count.\n"
    "3. It asks something of the user or genuinely warrants a personal "
    "response.\n"
    "4. Receipts, order/shipping confirmations, calendar invites, "
    "newsletters, and social notifications are NEVER reply-worthy.\n"
    "5. When in doubt, it is NOT reply-worthy — a missed draft is cheap; a "
    "wrong draft is expensive."
)

_SNIPPET_CHARS = 300


def resolve_user_address() -> str | None:
    """The user's own mailbox address, or None when it isn't stored.

    Reads the user-scoped IMAP_USERNAME secret (the full email address for
    every IMAP-path provider). Agents call this inside their per-uid
    ContextVar block, so user scope resolves to the right mailbox. Gmail
    accounts have no stored address — returns None and the LLM judges
    directness from the To/Cc lines alone.
    """
    try:
        address = JarvisStorage("email").get_secret("IMAP_USERNAME", scope="user")
    except Exception as e:  # never break a pipeline over a secret read
        logger.warning("Reply rubric: user address lookup failed", error=str(e))
        return None
    address = (address or "").strip()
    return address or None


def is_direct_recipient(email: Any, user_address: str | None) -> bool:
    """True when the user's address appears in the To header (case-insensitive).

    Conservative on missing data: an unknown user address or an empty To
    header (some providers omit it on fetch) returns True — defer to the LLM
    rather than false-drop. Only a known address that is absent from a
    populated To header returns False (CC-only / list blast).
    """
    if not user_address:
        return True
    to = str(getattr(email, "to", "") or "").strip()
    if not to:
        return True
    return user_address.lower() in to.lower()


def select_reply_worthy(
    emails: list[Any],
    user_instructions: str,
    user_address: str | None,
) -> list[Any]:
    """Single LLM call judging which emails deserve a drafted reply.

    System prompt = strict-judge framing + BASELINE_RULES + the user's
    instructions applied ON TOP. Each candidate line carries From / To / Cc /
    Subject / snippet so the judge can verify directness; the user's own
    address is included when known.

    Fail-CLOSED: ask_llm unavailable, empty/garbage output, or no parseable
    indices all return [] — a missed draft is cheap, a wrong draft is
    expensive. Out-of-range and non-numeric indices are dropped.
    """
    if not emails:
        return []

    try:
        from services.node_llm_client import ask_llm
    except ImportError:
        logger.warning("Reply rubric: ask_llm unavailable; skipping (fail-closed)")
        return []

    from .reply_drafts import strip_llm_noise

    email_lines = []
    for i, e in enumerate(emails, start=1):
        email_lines.append(
            f"{i}. From: {e.sender}\n"
            f"   To: {str(getattr(e, 'to', '') or '').strip()}\n"
            f"   Cc: {str(getattr(e, 'cc', '') or '').strip()}\n"
            f"   Subject: {e.subject}\n"
            f"   {(e.snippet or '').strip()[:_SNIPPET_CHARS]}"
        )

    system = (
        "You are a strict email filter. Your only job is to judge which "
        "emails deserve a drafted reply. Treat the rules as HARD "
        "constraints:\n"
        "- Match only emails that CLEARLY satisfy every rule below. "
        "Tangential or 'kind of related' emails do NOT match.\n"
        "- When in doubt, SKIP the email. The cost of a false negative "
        "(missing one match) is much lower than a false positive (drafting "
        "a reply the user never wanted).\n\n"
        "Built-in rules (ALWAYS apply):\n"
        f"{BASELINE_RULES}\n\n"
        "The user's additional instructions (apply ON TOP of the rules "
        "above):\n"
        f"{user_instructions}\n\n"
        "Do NOT rewrite, summarize, or compose anything. Output ONLY a JSON "
        "array of the matching email numbers."
    )

    address_line = (
        f"The user's own email address is {user_address}.\n\n" if user_address else ""
    )
    prompt = (
        f"{address_line}"
        f"Emails ({len(emails)} total):\n\n"
        + "\n\n".join(email_lines)
        + "\n\nReturn the numbers of emails that deserve a drafted reply, as "
        'a JSON array. Example: [1, 4, 7]. If nothing qualifies, return: []. '
        "Output ONLY the array — no prose, no code fences, no explanation."
    )

    raw = ask_llm(prompt, system=system) or ""
    if not raw:
        logger.warning(
            "Reply rubric: empty LLM response; skipping (fail-closed)",
            email_count=len(emails),
        )
        return []

    cleaned = strip_llm_noise(raw)
    match = re.search(r"\[[^\[\]]*\]", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)

    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, list):
            raise ValueError("expected a JSON array of indices")
    except Exception as e:
        logger.warning(
            "Reply rubric: parse failed; skipping (fail-closed)",
            error=str(e),
            raw=raw[:200],
        )
        return []

    matched: list[Any] = []
    for idx in parsed:
        try:
            i = int(idx)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= len(emails):
            matched.append(emails[i - 1])

    logger.info(
        "Reply rubric applied",
        input_emails=len(emails),
        matched=len(matched),
    )
    return matched
