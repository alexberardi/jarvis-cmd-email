"""Daily digest as an interactive triage list — post, dedup, and Alert fallback."""

import importlib.util
import os
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace

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
    if "email_shared.email_service_factory" not in sys.modules:
        esf = types.ModuleType("email_shared.email_service_factory")
        esf.create_email_service = lambda: None
        esf.get_email_provider = lambda: "gmail"
        sys.modules["email_shared.email_service_factory"] = esf
    _load_real("email_shared.triage", "email_shared", "triage.py")


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


class TestDigestTriagePost:
    def test_digest_hour_posts_triage_payload(self, agent, inbox_backend):
        emails = [_make_email(1), _make_email(2), _make_email(3)]

        alerts = agent._check_digest(emails, 7, _DIGEST_NOW)

        assert alerts == []  # replaced the text Alert
        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["command_name"] == "email"
        assert call["title"] == "Daily email digest — 3 unread"
        assert call["category"] == "interactive_list"
        assert call["create_push_notification"] is True
        assert call["target_type"] == "household"
        assert call["user_id"] is None
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

        assert agent._check_digest(emails, 7, _DIGEST_NOW) == []
        assert agent._check_digest(emails, 7, _DIGEST_NOW) == []
        assert len(inbox_backend.calls) == 1

    def test_outside_digest_hour_no_post(self, agent, inbox_backend):
        emails = [_make_email(1)]
        noon = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

        assert agent._check_digest(emails, 7, noon) == []
        assert inbox_backend.calls == []

    def test_no_emails_no_post(self, agent, inbox_backend):
        assert agent._check_digest([], 7, _DIGEST_NOW) == []
        assert inbox_backend.calls == []


class TestDigestFallback:
    def test_post_failure_falls_back_to_text_alert(self, agent, inbox_backend):
        inbox_backend.tag = "http_error"
        emails = [
            _make_email(1, "Alice"),
            _make_email(2, "Alice"),
            _make_email(3, "Bob"),
        ]

        alerts = agent._check_digest(emails, 7, _DIGEST_NOW)

        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.title == "Daily Email Digest"
        assert alert.priority == 1
        assert "3 unread emails" in alert.summary
        assert "Alice (2)" in alert.summary
        assert "Bob (1)" in alert.summary

    def test_no_backend_falls_back_to_text_alert(self, agent):
        # No inbox backend registered at all — the facade returns "no_backend"
        alerts = agent._check_digest([_make_email(1)], 7, _DIGEST_NOW)

        assert len(alerts) == 1
        assert alerts[0].title == "Daily Email Digest"

    def test_fallback_still_consumes_daily_guard(self, agent, inbox_backend):
        inbox_backend.tag = "http_error"
        emails = [_make_email(1)]

        first = agent._check_digest(emails, 7, _DIGEST_NOW)
        second = agent._check_digest(emails, 7, _DIGEST_NOW)

        assert len(first) == 1
        assert second == []  # the fallback Alert already carried today's digest
