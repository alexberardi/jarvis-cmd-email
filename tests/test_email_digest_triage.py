"""Daily digest as an interactive triage list — post, dedup, and Alert fallback.

Also covers the per-user run flow (mailbox secrets are user-scoped; the agent
iterates node-side configured users with the SDK user ContextVar set).
"""

import asyncio
import importlib.util
import os
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jarvis_command_sdk import set_inbox_backend
from jarvis_command_sdk.inbox import InboxBackend

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
_agent_mod = _load_real(
    "email_alerts_agent_under_test", "agents", "email_alerts", "agent.py"
)


def _make_email(idx: int, sender_name: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"id-{idx}",
        sender=f"{sender_name or f'Sender {idx}'} <sender{idx}@example.com>",
        sender_name=sender_name or f"Sender {idx}",
        subject=f"Subject {idx}",
        snippet=f"Snippet {idx}",
    )


class _FakeInboxBackend(InboxBackend):
    def __init__(self, tag: str = "ok") -> None:
        self.tag = tag
        self.calls: list[dict] = []

    def post_inbox_item(self, command_name, **kwargs) -> str:
        self.calls.append({"command_name": command_name, **kwargs})
        return self.tag


@pytest.fixture
def inbox_backend():
    backend = _FakeInboxBackend()
    set_inbox_backend(backend)
    yield backend
    set_inbox_backend(None)


@pytest.fixture
def agent():
    return _agent_mod.EmailAlertAgent()


_DIGEST_NOW = datetime(2026, 6, 10, 7, 30, tzinfo=timezone.utc)
_UID = 7


class TestDigestTriagePost:
    def test_digest_hour_posts_triage_payload(self, agent, inbox_backend):
        emails = [_make_email(1), _make_email(2), _make_email(3)]

        alerts = agent._check_digest(emails, 7, _DIGEST_NOW, _UID)

        assert alerts == []  # replaced the text Alert
        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["command_name"] == "email"
        assert call["title"] == "Daily email digest — 3 unread"
        assert call["category"] == "interactive_list"
        assert call["create_push_notification"] is True
        assert call["target_type"] == "user"
        assert call["user_id"] == _UID
        assert "3 unread emails" in call["summary"]
        assert call["body"] == (
            "- Sender 1: Subject 1\n- Sender 2: Subject 2\n- Sender 3: Subject 3"
        )

        # Same payload shape the command's triage action ships
        metadata = call["metadata"]
        assert metadata["type"] == "interactive_list"
        assert metadata["command_name"] == "email"
        assert metadata["context"]["subjects"] == {
            "id-1": "Subject 1", "id-2": "Subject 2", "id-3": "Subject 3",
        }
        rows = metadata["sections"][0]["rows"]
        assert [r["key"] for r in rows] == ["id-1", "id-2", "id-3"]
        assert all(r["control"] == "checkbox" for r in rows)
        assert [a["callback"] for a in metadata["actions"]] == [
            "triage_mark_read", "triage_archive", "triage_star",
        ]

    def test_once_per_day_guard(self, agent, inbox_backend):
        emails = [_make_email(1)]

        assert agent._check_digest(emails, 7, _DIGEST_NOW, _UID) == []
        assert agent._check_digest(emails, 7, _DIGEST_NOW, _UID) == []
        assert len(inbox_backend.calls) == 1

    def test_daily_guard_is_per_user(self, agent, inbox_backend):
        emails = [_make_email(1)]

        assert agent._check_digest(emails, 7, _DIGEST_NOW, 1) == []
        assert agent._check_digest(emails, 7, _DIGEST_NOW, 2) == []  # other user still posts
        assert agent._check_digest(emails, 7, _DIGEST_NOW, 1) == []  # uid 1 already done today

        assert [c["user_id"] for c in inbox_backend.calls] == [1, 2]

    def test_outside_digest_hour_no_post(self, agent, inbox_backend):
        emails = [_make_email(1)]
        noon = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

        assert agent._check_digest(emails, 7, noon, _UID) == []
        assert inbox_backend.calls == []

    def test_no_emails_no_post(self, agent, inbox_backend):
        assert agent._check_digest([], 7, _DIGEST_NOW, _UID) == []
        assert inbox_backend.calls == []


class TestDigestFallback:
    def test_post_failure_falls_back_to_text_alert(self, agent, inbox_backend):
        inbox_backend.tag = "http_error"
        emails = [
            _make_email(1, "Alice"),
            _make_email(2, "Alice"),
            _make_email(3, "Bob"),
        ]

        alerts = agent._check_digest(emails, 7, _DIGEST_NOW, _UID)

        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.title == "Daily Email Digest"
        assert alert.priority == 1
        assert "3 unread emails" in alert.summary
        assert "Alice (2)" in alert.summary
        assert "Bob (1)" in alert.summary

    def test_no_backend_falls_back_to_text_alert(self, agent):
        # No inbox backend registered at all — the facade returns "no_backend"
        alerts = agent._check_digest([_make_email(1)], 7, _DIGEST_NOW, _UID)

        assert len(alerts) == 1
        assert alerts[0].title == "Daily Email Digest"

    def test_fallback_still_consumes_daily_guard(self, agent, inbox_backend):
        inbox_backend.tag = "http_error"
        emails = [_make_email(1)]

        first = agent._check_digest(emails, 7, _DIGEST_NOW, _UID)
        second = agent._check_digest(emails, 7, _DIGEST_NOW, _UID)

        assert len(first) == 1
        assert second == []  # the fallback Alert already carried today's digest


# ── Discovery context: validate_secrets via node-side user enumeration ───────


class TestValidateSecretsDiscovery:
    def test_passes_with_a_configured_user(self, agent, monkeypatch):
        # No storage backend, no ContextVar user — a configured user found
        # node-side is enough for the agent to surface at discovery.
        monkeypatch.setattr(_agent_mod, "find_configured_user_ids", lambda: [3])
        assert agent.validate_secrets() == []

    def test_fails_with_no_users_gmail_branch(self, agent, monkeypatch):
        monkeypatch.setattr(_agent_mod, "find_configured_user_ids", lambda: [])
        monkeypatch.setattr(_agent_mod, "get_email_provider", lambda: "gmail")
        assert agent.validate_secrets() == ["GMAIL_ACCESS_TOKEN"]

    @pytest.mark.parametrize("provider", ["proton", "yahoo", "outlook", "fastmail", "imap"])
    def test_fails_with_no_users_imap_branch(self, agent, monkeypatch, provider):
        # Every non-gmail provider (IMAP presets included) must report the
        # IMAP creds, never GMAIL_ACCESS_TOKEN.
        monkeypatch.setattr(_agent_mod, "find_configured_user_ids", lambda: [])
        monkeypatch.setattr(_agent_mod, "get_email_provider", lambda: provider)
        assert agent.validate_secrets() == ["IMAP_USERNAME", "IMAP_PASSWORD"]


# ── Per-user run flow + uid-prefixed alert dedup ─────────────────────────────


@pytest.fixture
def context_calls(monkeypatch):
    """Record every set_current_user_id call the agent makes."""
    calls: list[int | None] = []
    monkeypatch.setattr(_agent_mod, "set_current_user_id", calls.append)
    return calls


def _run(agent) -> None:
    asyncio.run(agent.run())


class TestPerUserRun:
    def test_zero_configured_users_is_noop(self, agent, inbox_backend, monkeypatch):
        monkeypatch.setattr(_agent_mod, "find_configured_user_ids", lambda: [])
        create = MagicMock()
        monkeypatch.setattr(_agent_mod, "create_email_service", create)

        _run(agent)

        create.assert_not_called()
        assert agent.get_alerts() == []
        assert inbox_backend.calls == []

    def test_context_var_set_then_reset_per_user(
        self, agent, inbox_backend, context_calls, monkeypatch
    ):
        monkeypatch.setattr(_agent_mod, "find_configured_user_ids", lambda: [4, 9])
        service = MagicMock()
        service.search.return_value = []  # no mail — deterministic no-alert run
        monkeypatch.setattr(_agent_mod, "create_email_service", lambda: service)

        _run(agent)

        assert context_calls == [4, None, 9, None]
        assert service.search.call_count == 2

    def test_context_var_reset_even_when_user_flow_raises(
        self, agent, inbox_backend, context_calls, monkeypatch
    ):
        monkeypatch.setattr(_agent_mod, "find_configured_user_ids", lambda: [4])

        def boom(uid, reply_budget):
            raise RuntimeError("mailbox exploded")

        monkeypatch.setattr(agent, "_run_for_user", boom)

        _run(agent)  # outer try swallows; must not raise

        assert context_calls == [4, None]
        assert agent.get_alerts() == []

    def test_users_capped_at_five_per_run(
        self, agent, inbox_backend, context_calls, monkeypatch
    ):
        monkeypatch.setattr(
            _agent_mod, "find_configured_user_ids", lambda: [1, 2, 3, 4, 5, 6, 7]
        )
        service = MagicMock()
        service.search.return_value = []
        monkeypatch.setattr(_agent_mod, "create_email_service", lambda: service)

        _run(agent)

        visited = [c for c in context_calls if c is not None]
        assert visited == [1, 2, 3, 4, 5]


class TestTriggerPredicates:
    """VIP/urgent are now bool predicates — posting (and the persistent shared
    dedup) lives in _post_reply_items / email_shared.reply_drafts; the full
    flow is covered in test_email_alert_reply_posts.py."""

    def test_vip_match_is_boolean(self, agent):
        email = _make_email(1)

        assert agent._check_vip(email, {"sender1@example.com"}) is True
        assert agent._check_vip(email, {"other@example.com"}) is False
        assert agent._check_vip(email, set()) is False  # no VIP list → no match

    def test_urgent_match_is_boolean(self, agent):
        email = SimpleNamespace(
            id="id-9",
            sender="Boss <boss@example.com>",
            sender_name="Boss",
            subject="URGENT: deadline today",
            snippet="please respond asap",
        )

        assert agent._check_urgent(email, {"urgent"}) is True
        assert agent._check_urgent(email, {"invoice"}) is False
