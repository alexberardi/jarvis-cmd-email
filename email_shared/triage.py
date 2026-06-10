"""Shared triage payload builder — interactive inbox triage for unread email.

Both the on-demand "triage my inbox" command action and the daily-digest path
in the alerts agent post the SAME interactive-list payload; building it in one
place keeps the two from drifting.

The payload's ``context`` carries ``{"subjects": {message_id: subject}}`` and
is echoed back verbatim in every triage callback, so the callbacks can report
per-message ``detail_lines`` without re-fetching each email.
"""

from typing import Any

from jarvis_command_sdk import (
    InteractiveAction,
    InteractiveList,
    InteractiveRow,
    InteractiveSection,
)

from .email_message import EmailMessage

# SDK builder caps — labels/captions are rejected (not truncated) when over.
_MAX_LABEL_CHARS = 120
_MAX_CAPTION_CHARS = 200
# Subjects echoed back in callback context — kept short to bound payload size.
_MAX_CONTEXT_SUBJECT_CHARS = 80

TRIAGE_EMPTY_TEXT = "Inbox zero — nothing to triage."


def build_triage_payload(emails: list[EmailMessage]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the interactive triage payload for a list of unread emails.

    Returns ``(metadata, context)``:
    - ``metadata`` — the InteractiveList wire dict; ship it as the inbox item's
      ``metadata`` with ``category=InteractiveList.CATEGORY``.
    - ``context`` — the ``{"subjects": {message_id: subject}}`` dict embedded
      in the payload, echoed verbatim in every triage callback.
    """
    context: dict[str, Any] = {
        "subjects": {e.id: (e.subject or "")[:_MAX_CONTEXT_SUBJECT_CHARS] for e in emails}
    }
    rows = [
        InteractiveRow(
            key=e.id,
            label=(e.sender_name or "").strip()[:_MAX_LABEL_CHARS] or "Unknown",
            caption=(e.subject or "")[:_MAX_CAPTION_CHARS],
            control="checkbox",
            default_selected=False,
        )
        for e in emails
    ]
    payload = InteractiveList(
        command_name="email",
        sections=[InteractiveSection(rows=rows)],
        actions=[
            InteractiveAction(label="Mark {n} read", callback="triage_mark_read", style="primary"),
            InteractiveAction(label="Archive {n}", callback="triage_archive", style="secondary"),
            InteractiveAction(label="Star {n}", callback="triage_star", style="secondary"),
        ],
        context=context,
        empty_text=TRIAGE_EMPTY_TEXT,
    )
    return payload.to_dict(), context


def build_triage_body(emails: list[EmailMessage]) -> str:
    """Plain-text fallback listing for clients without the rich renderer."""
    return "\n".join(f"- {e.sender_name}: {e.subject}" for e in emails)
