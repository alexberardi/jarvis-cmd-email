"""VIP/urgent alerts as drafted inbox+push reply items (email_shared.reply_drafts).

Covers the email_alerts side of the shared reply surface:
- VIP/urgent matches post user-targeted items with Send/Ignore draft elements.
- KEY ASYMMETRY vs the smart_reply filter path: the post happens even with
  draft=None (LLM down / should_reply=false) — a deterministic trigger must
  never be lost; the draft is enrichment.
- Persistent dedup is SHARED with smart_reply (namespace "email_smart_reply",
  keys "{uid}:{message_id}") so an email matching both the urgent keywords and
  the smart-reply filter posts exactly once across both agents.
- Posted vs declined: a smart_reply DECLINE (should_reply=false / parse_fail)
  is recorded with posted=False — it suppresses re-judging (already_handled)
  but NEVER the VIP/urgent gate (already_posted), so a decline can't swallow
  a deterministic notification. Legacy records without the flag still dedup.
- Dedup marks only on tag "ok"; failed posts retry next tick.
- Cap of 3 reply posts per run across VIP + urgent (and across users).
- No Alert emission from vip/urgent — get_alerts() serves only the digest
  fallback now.
"""

import asyncio
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from jarvis_command_sdk import set_backend, set_inbox_backend
from jarvis_command_sdk.inbox import InboxBackend
from jarvis_command_sdk.storage import StorageBackend

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))


def _load_real(name: str, *parts: str):
    path = os.path.join(_ROOT, *parts)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _stub_log_client() -> None:
    """No-op logger so failure-path tests don't queue real log batches."""
    stub = types.ModuleType("jarvis_log_client")

    class _Logger:
        def __init__(self, **kwargs): ...
        def info(self, *args, **kwargs): ...
        def warning(self, *args, **kwargs): ...
        def error(self, *args, **kwargs): ...
        def debug(self, *args, **kwargs): ...

    stub.JarvisLogger = _Logger
    sys.modules["jarvis_log_client"] = stub


def _install_email_shared() -> None:
    _stub_log_client()
    if "email_shared" not in sys.modules:
        sys.modules["email_shared"] = types.ModuleType("email_shared")
    _load_real("email_shared.email_message", "email_shared", "email_message.py")
    _load_real("email_shared.senders", "email_shared", "senders.py")
    _load_real("email_shared.user_resolution", "email_shared", "user_resolution.py")
    if "email_shared.email_service_factory" not in sys.modules:
        esf = types.ModuleType("email_shared.email_service_factory")
        esf.create_email_service = lambda: None
        esf.get_email_provider = lambda: "gmail"
        sys.modules["email_shared.email_service_factory"] = esf
    _load_real("email_shared.triage", "email_shared", "triage.py")
    _load_real("email_shared.connection_health", "email_shared", "connection_health.py")
    _load_real("email_shared.reply_rubric", "email_shared", "reply_rubric.py")
    _load_real("email_shared.reply_drafts", "email_shared", "reply_drafts.py")


_install_email_shared()
_reply_drafts = sys.modules["email_shared.reply_drafts"]
_alerts_mod = _load_real(
    "email_alerts_agent_reply_posts", "agents", "email_alerts", "agent.py"
)
_smart_mod = _load_real(
    "smart_reply_agent_for_dedup", "agents", "smart_reply", "agent.py"
)


def _make_email(
    idx: int,
    sender_email: str | None = None,
    subject: str | None = None,
    snippet: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"id-{idx}",
        thread_id=f"thread-{idx}",
        sender=f"Sender {idx} <{sender_email or f'sender{idx}@example.com'}>",
        sender_name=f"Sender {idx}",
        subject=subject or f"Subject {idx}",
        snippet=snippet or f"Snippet {idx}",
        body=f"Body {idx}",
    )


def _vip_email(idx: int) -> SimpleNamespace:
    return _make_email(idx, sender_email="vip@example.com")


def _urgent_email(idx: int) -> SimpleNamespace:
    return _make_email(idx, subject=f"URGENT: thing {idx}")


def _install_fake_node_llm_client(monkeypatch, ask_llm_impl) -> None:
    """Make `from services.node_llm_client import ask_llm` resolve to our fake."""
    services_mod = sys.modules.get("services") or types.ModuleType("services")
    monkeypatch.setitem(sys.modules, "services", services_mod)
    node_mod = types.ModuleType("services.node_llm_client")
    node_mod.ask_llm = ask_llm_impl  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.node_llm_client", node_mod)
    services_mod.node_llm_client = node_mod  # type: ignore[attr-defined]


_OK_DRAFT = '{"should_reply": true, "draft": "Sounds good — see you then."}'


def _staged_ask_llm(filter_response, draft_response=_OK_DRAFT):
    """Fake ask_llm dispatching on the system prompt: filter vs draft stage."""

    def fake_ask(prompt: str, *, system=None, **kw):
        if system and "strict email filter" in system:
            return filter_response
        return draft_response

    return fake_ask


class _FakeStorageBackend(StorageBackend):
    """In-memory records (honoring expires_at) + secrets keyed by (key, scope)."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict] = {}
        self.expires: dict[tuple[str, str], datetime | None] = {}
        self.secrets: dict[tuple[str, str], str] = {}

    def save(self, command_name, data_key, data, expires_at=None) -> None:
        self.records[(command_name, data_key)] = data
        self.expires[(command_name, data_key)] = expires_at

    def get(self, command_name, data_key):
        key = (command_name, data_key)
        if key not in self.records:
            return None
        exp = self.expires.get(key)
        if exp is not None and exp <= datetime.now(timezone.utc):
            return None
        return self.records[key]

    def get_all(self, command_name):
        return [v for (c, _), v in self.records.items() if c == command_name]

    def delete(self, command_name, data_key) -> bool:
        return self.records.pop((command_name, data_key), None) is not None

    def delete_all(self, command_name) -> int:
        keys = [k for k in self.records if k[0] == command_name]
        for k in keys:
            del self.records[k]
        return len(keys)

    def get_secret(self, key, scope, user_id=None):
        return self.secrets.get((key, scope))

    def set_secret(self, key, value, scope, value_type="string", user_id=None) -> None:
        self.secrets[(key, scope)] = value

    def delete_secret(self, key, scope, user_id=None) -> None:
        self.secrets.pop((key, scope), None)


class _FakeInboxBackend(InboxBackend):
    def __init__(self, tag: str = "ok") -> None:
        self.tag = tag
        self.calls: list[dict] = []

    def post_inbox_item(self, command_name, **kwargs) -> str:
        self.calls.append({"command_name": command_name, **kwargs})
        return self.tag


@pytest.fixture
def storage_backend():
    backend = _FakeStorageBackend()
    backend.secrets[("EMAIL_ALERT_VIP_SENDERS", "integration")] = "vip@example.com"
    backend.secrets[("EMAIL_ALERT_URGENT_KEYWORDS", "integration")] = "urgent,asap"
    # Keep the daily digest quiet regardless of when the tests run.
    quiet_hour = (datetime.now(timezone.utc).hour + 2) % 24
    backend.secrets[("EMAIL_ALERT_DIGEST_HOUR", "integration")] = str(quiet_hour)
    backend.secrets[("EMAIL_NOTIFICATION_FILTER", "integration")] = "clients and invoices"
    set_backend(backend)
    yield backend
    set_backend(None)


@pytest.fixture
def inbox_backend():
    backend = _FakeInboxBackend()
    set_inbox_backend(backend)
    yield backend
    set_inbox_backend(None)


@pytest.fixture(autouse=True)
def one_configured_user(monkeypatch):
    """Default: one mailbox-configured user (uid 1). Tests override as needed."""
    monkeypatch.setattr(_alerts_mod, "find_configured_user_ids", lambda: [1])
    monkeypatch.setattr(_smart_mod, "find_configured_user_ids", lambda: [1])


@pytest.fixture
def agent():
    return _alerts_mod.EmailAlertAgent()


def _wire_service(monkeypatch, emails: list[SimpleNamespace]) -> MagicMock:
    service = MagicMock()
    service.search.return_value = emails
    service.fetch_message.side_effect = lambda mid, max_body_chars=1000: next(
        (e for e in emails if e.id == mid), None
    )
    monkeypatch.setattr(_alerts_mod, "create_email_service", lambda: service)
    monkeypatch.setattr(_smart_mod, "create_email_service", lambda: service)
    return service


def _run(agent) -> None:
    asyncio.run(agent.run())


# ── Post shape: VIP and urgent items ─────────────────────────────────────────


class TestReplyPostShape:
    def test_vip_posts_user_targeted_item_with_draft_elements(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_vip_email(1)])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm(None))

        _run(agent)

        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["command_name"] == "email"
        assert call["title"] == "Email from Sender 1"
        assert call["summary"] == "Subject 1"
        # The draft lives ONLY in metadata.editable_text now — the body stays
        # From/Subject/snippet so the item is informative without the editor.
        assert call["body"] == (
            "From: Sender 1 <vip@example.com>\n"
            "Subject: Subject 1\n\n"
            "Snippet 1"
        )
        assert "— Draft reply —" not in call["body"]
        assert call["category"] == "smart_reply"
        assert call["user_id"] == 1
        assert call["target_type"] == "user"
        assert call["create_push_notification"] is True

        assert call["metadata"]["editable_text"] == {
            "label": "Draft reply",
            "initial": "Sounds good — see you then.",
            "data_key": "body",
        }
        elements = call["metadata"]["interactive_elements"]
        assert elements == [
            {
                "id": "send-id-1",
                "label": "Send reply",
                "kind": "send",
                "command": "email",
                "callback": "send_draft_reply",
                "data": {
                    "message_id": "id-1",
                    "thread_id": "thread-1",
                    "body": "Sounds good — see you then.",
                },
                "navigation_type": "stack",
            },
            {
                "id": "ignore-id-1",
                "label": "Ignore",
                "command": "email",
                "callback": "dismiss_draft",
                "data": {"message_id": "id-1"},
            },
        ]

        # Persistent shared dedup marked on the ok post
        assert storage_backend.get("email_smart_reply", "1:id-1") is not None

    def test_urgent_posts_with_urgent_title(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_urgent_email(1)])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm(None))

        _run(agent)

        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["title"] == "Urgent: URGENT: thing 1"
        assert call["user_id"] == 1
        assert call["target_type"] == "user"
        assert call["category"] == "smart_reply"
        # Draft is carried in metadata.editable_text, never the body.
        assert "— Draft reply —" not in call["body"]
        assert call["metadata"]["editable_text"]["initial"] == (
            "Sounds good — see you then."
        )

    def test_email_matching_vip_and_urgent_posts_once_as_vip(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        both = _make_email(1, sender_email="vip@example.com", subject="urgent: now")
        _wire_service(monkeypatch, [both])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm(None))

        _run(agent)

        assert len(inbox_backend.calls) == 1
        assert inbox_backend.calls[0]["title"] == "Email from Sender 1"


# ── KEY ASYMMETRY: deterministic triggers post even without a draft ──────────


class TestPostsWithoutDraft:
    def test_llm_down_still_posts_without_send_element(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_vip_email(1)])
        monkeypatch.setitem(sys.modules, "services.node_llm_client", None)

        _run(agent)

        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["title"] == "Email from Sender 1"
        assert call["body"] == (
            "From: Sender 1 <vip@example.com>\nSubject: Subject 1\n\nSnippet 1"
        )
        assert "— Draft reply —" not in call["body"]
        assert call["metadata"] is None  # no Send/Ignore elements without a draft
        # The trigger is consumed — dedup marked on the successful post
        assert storage_backend.get("email_smart_reply", "1:id-1") is not None

    def test_should_reply_false_posts_without_draft(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_vip_email(1)])
        _install_fake_node_llm_client(
            monkeypatch,
            _staged_ask_llm(None, '{"should_reply": false, "draft": ""}'),
        )

        _run(agent)

        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert "— Draft reply —" not in call["body"]
        assert call["metadata"] is None

    def test_fetch_failure_still_posts_without_draft(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        service = _wire_service(monkeypatch, [_vip_email(1)])
        service.fetch_message.side_effect = RuntimeError("imap hiccup")
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm(None))

        _run(agent)

        assert len(inbox_backend.calls) == 1
        assert "— Draft reply —" not in inbox_backend.calls[0]["body"]


# ── Bulk senders: urgent posts draft-less, VIPs are never screened ───────────


class TestBulkSenders:
    def test_urgent_bulk_sender_posts_without_send_element_or_draft_llm(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        # An automated fraud alert from noreply@bank must not be lost — it
        # still posts — but draft=None is forced and the draft LLM is never
        # called (replying to a no-reply sender is pointless by definition).
        email = _make_email(
            1, sender_email="noreply@bank.example.com", subject="Urgent: fraud alert"
        )
        service = _wire_service(monkeypatch, [email])
        ask = MagicMock()
        _install_fake_node_llm_client(monkeypatch, ask)

        _run(agent)

        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["title"] == "Urgent: Urgent: fraud alert"
        assert call["metadata"] is None  # no Send/Ignore, no editable_text
        assert "— Draft reply —" not in call["body"]
        ask.assert_not_called()
        service.fetch_message.assert_not_called()
        # The trigger is consumed — dedup marked on the successful post.
        assert storage_backend.get("email_smart_reply", "1:id-1") is not None

    def test_urgent_with_unsubscribe_signal_posts_without_draft(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        email = _make_email(1, subject="URGENT: final notice")
        email.unsubscribe_url = "https://promo.example.com/unsub"
        _wire_service(monkeypatch, [email])
        ask = MagicMock()
        _install_fake_node_llm_client(monkeypatch, ask)

        _run(agent)

        assert len(inbox_backend.calls) == 1
        assert inbox_backend.calls[0]["metadata"] is None
        ask.assert_not_called()

    def test_vip_bulk_sender_never_screened_gets_full_treatment(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        # VIP senders are explicitly user-listed: a noreply VIP still gets
        # the draft attempt and the full Send/Ignore + editable_text surface.
        storage_backend.secrets[("EMAIL_ALERT_VIP_SENDERS", "integration")] = (
            "noreply@bank.example.com"
        )
        email = _make_email(1, sender_email="noreply@bank.example.com")
        _wire_service(monkeypatch, [email])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm(None))

        _run(agent)

        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["title"] == "Email from Sender 1"
        assert call["metadata"]["editable_text"] == {
            "label": "Draft reply",
            "initial": "Sounds good — see you then.",
            "data_key": "body",
        }
        assert any(
            el["callback"] == "send_draft_reply"
            for el in call["metadata"]["interactive_elements"]
        )


# ── Directness gate: urgent drafting requires the user in To ─────────────────


class TestDirectRecipientGate:
    def test_urgent_cc_only_posts_without_send_element_or_draft_llm(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        # Urgent KEYWORDS still decide delivery (the post happens), but
        # drafting gates on directness: the user is only CC'd, so draft=None
        # is forced and the draft LLM is never consulted.
        storage_backend.secrets[("IMAP_USERNAME", "user")] = "alex@example.com"
        email = _urgent_email(1)
        email.to = "other@example.com"
        email.cc = "alex@example.com"
        service = _wire_service(monkeypatch, [email])
        ask = MagicMock()
        _install_fake_node_llm_client(monkeypatch, ask)

        _run(agent)

        assert len(inbox_backend.calls) == 1  # delivery unchanged
        call = inbox_backend.calls[0]
        assert call["title"] == "Urgent: URGENT: thing 1"
        assert call["metadata"] is None  # no Send element, no editable_text
        ask.assert_not_called()
        service.fetch_message.assert_not_called()
        # The trigger is consumed — dedup marked on the successful post.
        assert storage_backend.get("email_smart_reply", "1:id-1") is not None

    def test_urgent_direct_recipient_still_gets_draft(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        storage_backend.secrets[("IMAP_USERNAME", "user")] = "alex@example.com"
        email = _urgent_email(1)
        email.to = "Alex Berardi <ALEX@example.com>"  # case-insensitive match
        _wire_service(monkeypatch, [email])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm(None))

        _run(agent)

        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["metadata"]["editable_text"]["initial"] == (
            "Sounds good — see you then."
        )

    def test_urgent_unknown_address_defers_and_drafts(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        # No stored address (gmail account) — the deterministic screen defers
        # and the stage-2 judgment decides, exactly as before.
        email = _urgent_email(1)
        email.to = "other@example.com"
        email.cc = "alex@example.com"
        _wire_service(monkeypatch, [email])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm(None))

        _run(agent)

        assert len(inbox_backend.calls) == 1
        assert inbox_backend.calls[0]["metadata"] is not None

    def test_vip_cc_only_bypasses_directness_screen(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        # VIP senders are explicitly user-chosen — they skip ALL screens
        # (bulk AND directness); stage-2 should_reply still applies inside
        # the draft call as today.
        storage_backend.secrets[("IMAP_USERNAME", "user")] = "alex@example.com"
        email = _vip_email(1)
        email.to = "other@example.com"
        email.cc = "alex@example.com"
        _wire_service(monkeypatch, [email])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm(None))

        _run(agent)

        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["title"] == "Email from Sender 1"
        assert call["metadata"]["editable_text"]["initial"] == (
            "Sounds good — see you then."
        )
        assert any(
            el["callback"] == "send_draft_reply"
            for el in call["metadata"]["interactive_elements"]
        )

    def test_vip_cc_only_and_bulk_still_gets_draft_attempt(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        # Belt and braces: a VIP that would fail BOTH screens (bulk local
        # part + CC-only) still gets the full draft treatment.
        storage_backend.secrets[("EMAIL_ALERT_VIP_SENDERS", "integration")] = (
            "noreply@bank.example.com"
        )
        storage_backend.secrets[("IMAP_USERNAME", "user")] = "alex@example.com"
        email = _make_email(1, sender_email="noreply@bank.example.com")
        email.to = "other@example.com"
        email.cc = "alex@example.com"
        _wire_service(monkeypatch, [email])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm(None))

        _run(agent)

        assert len(inbox_backend.calls) == 1
        assert inbox_backend.calls[0]["metadata"] is not None


# ── Dedup: shared, persistent, marked only on ok ─────────────────────────────


class TestSharedDedup:
    def test_already_posted_email_is_skipped(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        service = _wire_service(monkeypatch, [_vip_email(1)])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm(None))
        _reply_drafts.mark_posted(1, "id-1")

        _run(agent)

        assert inbox_backend.calls == []
        service.fetch_message.assert_not_called()

    def test_post_failure_does_not_mark_dedup_and_retries_next_run(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_vip_email(1)])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm(None))
        inbox_backend.tag = "http_error"

        _run(agent)

        assert len(inbox_backend.calls) == 1
        assert storage_backend.get("email_smart_reply", "1:id-1") is None

        # Next tick the post succeeds and the dedup marks.
        inbox_backend.tag = "ok"
        _run(agent)

        assert len(inbox_backend.calls) == 2
        assert storage_backend.get("email_smart_reply", "1:id-1") is not None

    def test_urgent_and_filter_match_posts_exactly_once_across_both_agents(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        # One email matching BOTH the urgent keywords and the smart-reply
        # filter. Alerts agent runs first; smart_reply must not double-post.
        _wire_service(monkeypatch, [_urgent_email(1)])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm("[1]"))

        _run(agent)
        assert len(inbox_backend.calls) == 1

        smart = _smart_mod.SmartReplyAgent()
        _run(smart)

        assert len(inbox_backend.calls) == 1  # no second post
        assert storage_backend.get("email_smart_reply", "1:id-1") is not None

    def test_filter_then_urgent_also_posts_exactly_once(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        # Reverse order: smart_reply drafts first, alerts agent must skip.
        _wire_service(monkeypatch, [_urgent_email(1)])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm("[1]"))

        smart = _smart_mod.SmartReplyAgent()
        _run(smart)
        assert len(inbox_backend.calls) == 1
        assert inbox_backend.calls[0]["title"] == "Reply ready — Sender 1"

        _run(agent)

        assert len(inbox_backend.calls) == 1  # alerts agent deduped


# ── Posted vs declined: a decline never suppresses a VIP/urgent post ─────────


class TestPostedVsDeclinedDedup:
    def test_declined_is_handled_but_not_posted(self, storage_backend):
        _reply_drafts.mark_declined(1, "id-1")

        assert _reply_drafts.already_handled(1, "id-1") is True
        assert _reply_drafts.already_posted(1, "id-1") is False
        record = storage_backend.records[("email_smart_reply", "1:id-1")]
        assert record["posted"] is False
        assert "declined_at" in record

    def test_declined_record_carries_seven_day_ttl(self, storage_backend):
        before = datetime.now(timezone.utc)
        _reply_drafts.mark_declined(1, "id-1")
        after = datetime.now(timezone.utc)

        expires = storage_backend.expires[("email_smart_reply", "1:id-1")]
        assert before + timedelta(days=7) <= expires <= after + timedelta(days=7)

    def test_legacy_record_without_flag_counts_as_posted(self, storage_backend):
        # Pre-flag records keep deduping BOTH gates — never re-post on upgrade.
        storage_backend.records[("email_smart_reply", "1:id-1")] = {
            "drafted_at": "2026-06-01T00:00:00+00:00"
        }
        storage_backend.expires[("email_smart_reply", "1:id-1")] = None

        assert _reply_drafts.already_posted(1, "id-1") is True
        assert _reply_drafts.already_handled(1, "id-1") is True

    def test_mark_posted_overwrites_declined(self, storage_backend):
        _reply_drafts.mark_declined(1, "id-1")
        _reply_drafts.mark_posted(1, "id-1")

        assert _reply_drafts.already_posted(1, "id-1") is True
        assert storage_backend.records[("email_smart_reply", "1:id-1")]["posted"] is True

    def test_smart_reply_decline_then_urgent_alert_posts_exactly_once(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        # THE regression: smart_reply judges the email and declines
        # (should_reply=false). That verdict must not suppress the
        # deterministic urgent notification — exactly one inbox post.
        _wire_service(monkeypatch, [_urgent_email(1)])
        _install_fake_node_llm_client(
            monkeypatch,
            _staged_ask_llm("[1]", '{"should_reply": false, "draft": ""}'),
        )

        smart = _smart_mod.SmartReplyAgent()
        _run(smart)
        assert inbox_backend.calls == []  # declined — nothing posted
        record = storage_backend.get("email_smart_reply", "1:id-1")
        assert record is not None
        assert record["posted"] is False

        _run(agent)  # the alerts agent must still deliver the urgent item

        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["title"] == "Urgent: URGENT: thing 1"
        assert call["metadata"] is None  # stage-2 declined — draft-less post
        # The successful post overwrites the decline record...
        assert storage_backend.get("email_smart_reply", "1:id-1")["posted"] is True

        # ...so neither agent ever posts it again.
        _run(agent)
        _run(smart)
        assert len(inbox_backend.calls) == 1


# ── Cap: 3 reply posts per run across VIP + urgent (and across users) ────────


class TestReplyPostCap:
    def test_cap_three_per_run_across_vip_and_urgent(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        emails = [_vip_email(1), _vip_email(2), _urgent_email(3), _urgent_email(4)]
        _wire_service(monkeypatch, emails)
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm(None))

        _run(agent)

        assert len(inbox_backend.calls) == 3
        # VIP first, then urgent — the second urgent email is over budget.
        assert [c["title"] for c in inbox_backend.calls] == [
            "Email from Sender 1",
            "Email from Sender 2",
            "Urgent: URGENT: thing 3",
        ]
        assert storage_backend.get("email_smart_reply", "1:id-4") is None

    def test_cap_is_global_across_users(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        monkeypatch.setattr(_alerts_mod, "find_configured_user_ids", lambda: [1, 2])
        _wire_service(monkeypatch, [_vip_email(1), _vip_email(2), _vip_email(3), _vip_email(4)])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm(None))

        _run(agent)

        # User 1 exhausts the budget of 3; user 2 posts nothing this run.
        assert len(inbox_backend.calls) == 3
        assert all(c["user_id"] == 1 for c in inbox_backend.calls)


# ── No Alert emission from vip/urgent ────────────────────────────────────────


class TestNoAlertEmission:
    def test_vip_and_urgent_emit_no_alerts(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_vip_email(1), _urgent_email(2)])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm(None))

        _run(agent)

        assert len(inbox_backend.calls) == 2  # both posted as inbox items
        assert agent.get_alerts() == []  # get_alerts() serves only the digest fallback

    def test_digest_fallback_alert_still_flows_through_run(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        # Digest hour == now and the inbox post fails — run() must surface the
        # legacy text Alert through get_alerts(), exactly as before.
        now_hour = datetime.now(timezone.utc).hour
        storage_backend.secrets[("EMAIL_ALERT_DIGEST_HOUR", "integration")] = str(now_hour)
        inbox_backend.tag = "http_error"
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm(None))

        _run(agent)

        alerts = agent.get_alerts()
        assert len(alerts) == 1
        assert alerts[0].title == "Daily Email Digest"
        assert alerts[0].priority == 1
