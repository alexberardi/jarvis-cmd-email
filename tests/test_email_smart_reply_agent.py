"""Smart-reply agent — fail-closed filter, draft parsing, persistent dedup, inbox post."""

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
    _load_real("email_shared.connection_health", "email_shared", "connection_health.py")
    _load_real("email_shared.reply_rubric", "email_shared", "reply_rubric.py")
    _load_real("email_shared.reply_drafts", "email_shared", "reply_drafts.py")


_install_email_shared()
_agent_mod = _load_real(
    "smart_reply_agent_under_test", "agents", "smart_reply", "agent.py"
)

# The exact class the agent module bound at load time — other test files
# reload email_shared.email_message, so raise THIS one or the agent's
# except clause won't match.
EmailConnectionError = sys.modules["email_shared.email_message"].EmailConnectionError


def _make_email(idx: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"id-{idx}",
        thread_id=f"thread-{idx}",
        sender=f"Sender {idx} <sender{idx}@example.com>",
        sender_name=f"Sender {idx}",
        subject=f"Subject {idx}",
        snippet=f"Snippet {idx}",
        body=f"Body {idx}",
    )


def _install_fake_node_llm_client(monkeypatch, ask_llm_impl) -> None:
    """Make `from services.node_llm_client import ask_llm` resolve to our fake."""
    services_mod = sys.modules.get("services") or types.ModuleType("services")
    monkeypatch.setitem(sys.modules, "services", services_mod)
    node_mod = types.ModuleType("services.node_llm_client")
    node_mod.ask_llm = ask_llm_impl  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.node_llm_client", node_mod)
    services_mod.node_llm_client = node_mod  # type: ignore[attr-defined]


def _staged_ask_llm(filter_response, draft_responses: list):
    """Fake ask_llm dispatching on the system prompt: filter vs draft stage."""
    drafts = list(draft_responses)

    def fake_ask(prompt: str, *, system=None, **kw):
        if system and "strict email filter" in system:
            return filter_response
        return drafts.pop(0) if drafts else None

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
    backend.secrets[("GMAIL_ACCESS_TOKEN", "integration")] = "tok"
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
    monkeypatch.setattr(_agent_mod, "find_configured_user_ids", lambda: [1])


@pytest.fixture
def context_calls(monkeypatch):
    """Record every set_current_user_id call the agent makes."""
    calls: list[int | None] = []
    monkeypatch.setattr(_agent_mod, "set_current_user_id", calls.append)
    return calls


@pytest.fixture
def agent():
    return _agent_mod.SmartReplyAgent()


def _wire_service(monkeypatch, emails: list[SimpleNamespace]) -> MagicMock:
    service = MagicMock()
    service.search.return_value = emails
    service.fetch_message.side_effect = lambda mid, max_body_chars=1000: next(
        (e for e in emails if e.id == mid), None
    )
    monkeypatch.setattr(_agent_mod, "create_email_service", lambda: service)
    return service


def _run(agent) -> None:
    asyncio.run(agent.run())


_OK_DRAFT = '{"should_reply": true, "draft": "Sounds good — see you then."}'


# ── Declaration ──────────────────────────────────────────────────────────────


class TestDeclaration:
    def test_schedule(self, agent):
        schedule = agent.schedule
        assert schedule.interval_seconds == 300
        assert schedule.run_on_startup is False

    def test_filter_secret_declared(self, agent):
        secrets = {s.key: s for s in agent.required_secrets}
        secret = secrets["EMAIL_NOTIFICATION_FILTER"]
        assert secret.scope == "integration"
        assert secret.required is False
        assert secret.is_sensitive is False

    def test_not_in_context(self, agent):
        assert agent.include_in_context is False
        assert agent.get_context_data() == {}
        assert agent.get_alerts() == []


# ── Stage 1 (LLM call 1): rubric judge wiring + deterministic CC screen ──────
# The fail-closed matrix for select_reply_worthy itself lives in
# tests/test_email_reply_rubric.py — these tests cover the agent pipeline.


class TestStage1RubricJudge:
    def test_system_prompt_carries_rubric_and_user_instructions(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_make_email(1)])
        captured: dict[str, Any] = {}

        def fake_ask(prompt: str, *, system=None, **kw):
            if system and "strict email filter" in system:
                captured["system"] = system
                captured["prompt"] = prompt
                return "[]"
            return _OK_DRAFT

        _install_fake_node_llm_client(monkeypatch, fake_ask)

        _run(agent)

        # Built-in rubric + the user's instructions applied ON TOP of it.
        assert "reply-worthy ONLY if ALL" in captured["system"]
        assert "apply ON TOP" in captured["system"]
        assert "clients and invoices" in captured["system"]
        # Candidate lines carry To/Cc so the judge can verify directness.
        assert "To:" in captured["prompt"]
        assert "Cc:" in captured["prompt"]
        assert "1. From: Sender 1 <sender1@example.com>" in captured["prompt"]


class TestDirectRecipientScreen:
    """When the user's own address is known, CC-only candidates never reach the LLM."""

    @staticmethod
    def _capture_filter(monkeypatch, response: str) -> dict:
        captured: dict[str, Any] = {}

        def fake_ask(prompt: str, *, system=None, **kw):
            if system and "strict email filter" in system:
                captured["prompt"] = prompt
                return response
            return _OK_DRAFT

        _install_fake_node_llm_client(monkeypatch, fake_ask)
        return captured

    def test_cc_only_candidates_dropped_pre_llm_when_address_known(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        storage_backend.secrets[("IMAP_USERNAME", "user")] = "alex@example.com"
        cc_only = _make_email(1)
        cc_only.to = "other@example.com"
        cc_only.cc = "alex@example.com"
        direct = _make_email(2)
        direct.to = "Alex Berardi <ALEX@example.com>"  # case-insensitive match
        _wire_service(monkeypatch, [cc_only, direct])
        captured = self._capture_filter(monkeypatch, "[1]")  # index 1 is now id-2

        _run(agent)

        assert "Subject 1" not in captured["prompt"]
        assert "Subject 2" in captured["prompt"]
        assert len(inbox_backend.calls) == 1
        assert inbox_backend.calls[0]["title"] == "Reply ready — Sender 2"

    def test_drop_skipped_when_address_unknown(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        # No IMAP_USERNAME secret (gmail account) — the CC-only candidate
        # still reaches the LLM; directness is judged from the To/Cc lines.
        cc_only = _make_email(1)
        cc_only.to = "other@example.com"
        cc_only.cc = "alex@example.com"
        _wire_service(monkeypatch, [cc_only])
        captured = self._capture_filter(monkeypatch, "[]")

        _run(agent)

        assert "Subject 1" in captured["prompt"]

    def test_empty_to_header_never_false_drops(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        # Some providers omit To on fetch — an empty header must not drop.
        storage_backend.secrets[("IMAP_USERNAME", "user")] = "alex@example.com"
        no_to = _make_email(1)  # SimpleNamespace without to/cc attributes
        _wire_service(monkeypatch, [no_to])
        captured = self._capture_filter(monkeypatch, "[]")

        _run(agent)

        assert "Subject 1" in captured["prompt"]

    def test_all_cc_only_skips_llm_entirely(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        storage_backend.secrets[("IMAP_USERNAME", "user")] = "alex@example.com"
        cc_only = _make_email(1)
        cc_only.to = "other@example.com"
        cc_only.cc = "alex@example.com"
        _wire_service(monkeypatch, [cc_only])
        ask = MagicMock()
        _install_fake_node_llm_client(monkeypatch, ask)

        _run(agent)

        ask.assert_not_called()
        assert inbox_backend.calls == []


# ── Draft generation (LLM call 2): JSON parsing ──────────────────────────────


class TestGenerateDraft:
    def test_valid_draft(self, agent, monkeypatch):
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: _OK_DRAFT)
        status, draft = agent._generate_draft(_make_email(1))
        assert status == "ok"
        assert draft == "Sounds good — see you then."

    def test_should_reply_false(self, agent, monkeypatch):
        _install_fake_node_llm_client(
            monkeypatch, lambda *a, **kw: '{"should_reply": false, "draft": ""}'
        )
        assert agent._generate_draft(_make_email(1)) == ("no_reply", "")

    def test_code_fences_stripped(self, agent, monkeypatch):
        raw = '```json\n{"should_reply": true, "draft": "Yes."}\n```'
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: raw)
        assert agent._generate_draft(_make_email(1)) == ("ok", "Yes.")

    def test_think_tags_stripped(self, agent, monkeypatch):
        raw = '<think>reasoning...</think>{"should_reply": true, "draft": "Yes."}'
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: raw)
        assert agent._generate_draft(_make_email(1)) == ("ok", "Yes.")

    def test_garbage_is_parse_fail(self, agent, monkeypatch):
        _install_fake_node_llm_client(
            monkeypatch, lambda *a, **kw: "Sure! Here's a draft: hello"
        )
        assert agent._generate_draft(_make_email(1)) == ("parse_fail", "")

    def test_should_reply_true_without_draft_is_parse_fail(self, agent, monkeypatch):
        _install_fake_node_llm_client(
            monkeypatch, lambda *a, **kw: '{"should_reply": true, "draft": ""}'
        )
        assert agent._generate_draft(_make_email(1)) == ("parse_fail", "")

    def test_empty_response_is_no_llm(self, agent, monkeypatch):
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: None)
        assert agent._generate_draft(_make_email(1)) == ("no_llm", "")

    def test_llm_unavailable_is_no_llm(self, agent, monkeypatch):
        monkeypatch.setitem(sys.modules, "services.node_llm_client", None)
        assert agent._generate_draft(_make_email(1)) == ("no_llm", "")

    def test_prompt_carries_full_body(self, agent, monkeypatch):
        captured: dict[str, Any] = {}

        def fake_ask(prompt: str, *, system=None, **kw):
            captured["prompt"] = prompt
            captured["system"] = system
            return _OK_DRAFT

        _install_fake_node_llm_client(monkeypatch, fake_ask)
        agent._generate_draft(_make_email(1))

        assert "Body 1" in captured["prompt"]
        assert "should_reply" in captured["system"]
        assert "150 words" in captured["system"]

    def test_draft_system_prompt_declines_automated_mail(self, agent, monkeypatch):
        # Belt-and-braces alongside the deterministic screen: the stage-2
        # prompt itself tells the model automated/marketing mail gets
        # should_reply=false.
        captured: dict[str, Any] = {}

        def fake_ask(prompt: str, *, system=None, **kw):
            captured["system"] = system
            return _OK_DRAFT

        _install_fake_node_llm_client(monkeypatch, fake_ask)
        agent._generate_draft(_make_email(1))

        assert "no-reply" in captured["system"]
        assert "marketing" in captured["system"]


# ── run(): gates, cap, post shape, dedup interplay ───────────────────────────


class TestRunFlow:
    def test_off_when_filter_empty(self, agent, storage_backend, inbox_backend, monkeypatch):
        del storage_backend.secrets[("EMAIL_NOTIFICATION_FILTER", "integration")]
        create = MagicMock()
        monkeypatch.setattr(_agent_mod, "create_email_service", create)

        _run(agent)

        create.assert_not_called()
        assert inbox_backend.calls == []

    def test_off_when_filter_whitespace(self, agent, storage_backend, inbox_backend, monkeypatch):
        storage_backend.secrets[("EMAIL_NOTIFICATION_FILTER", "integration")] = "   "
        create = MagicMock()
        monkeypatch.setattr(_agent_mod, "create_email_service", create)

        _run(agent)
        create.assert_not_called()

    def test_zero_configured_users_is_noop(self, agent, storage_backend, inbox_backend, monkeypatch):
        monkeypatch.setattr(_agent_mod, "find_configured_user_ids", lambda: [])
        create = MagicMock()
        monkeypatch.setattr(_agent_mod, "create_email_service", create)

        _run(agent)
        create.assert_not_called()
        assert inbox_backend.calls == []

    def test_happy_path_posts_draft(self, agent, storage_backend, inbox_backend, monkeypatch):
        emails = [_make_email(1), _make_email(2)]
        service = _wire_service(monkeypatch, emails)
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm("[1]", [_OK_DRAFT]))

        _run(agent)

        service.search.assert_called_once_with(
            "is:unread in:inbox newer_than:1d", max_results=20
        )
        service.fetch_message.assert_called_once_with("id-1", max_body_chars=3000)
        assert len(inbox_backend.calls) == 1
        # Posted id is persistently marked drafted, keyed per-user
        assert storage_backend.get("email_smart_reply", "1:id-1") is not None
        assert storage_backend.get("email_smart_reply", "1:id-2") is None

    def test_post_shape(self, agent, storage_backend, inbox_backend, monkeypatch):
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm("[1]", [_OK_DRAFT]))

        _run(agent)

        call = inbox_backend.calls[0]
        assert call["command_name"] == "email"
        assert call["title"] == "Reply ready — Sender 1"
        assert call["summary"] == "Subject 1"
        # The draft lives ONLY in metadata.editable_text now — the body stays
        # From/Subject/snippet so the item is informative without the editor.
        assert call["body"] == (
            "From: Sender 1 <sender1@example.com>\n"
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

    def test_max_two_posts_per_run(self, agent, storage_backend, inbox_backend, monkeypatch):
        emails = [_make_email(i) for i in range(1, 5)]
        service = _wire_service(monkeypatch, emails)
        _install_fake_node_llm_client(
            monkeypatch, _staged_ask_llm("[1, 2, 3, 4]", [_OK_DRAFT] * 4)
        )

        _run(agent)

        assert len(inbox_backend.calls) == 2
        assert service.fetch_message.call_count == 2  # matches capped before fetch
        assert storage_backend.get("email_smart_reply", "1:id-1") is not None
        assert storage_backend.get("email_smart_reply", "1:id-2") is not None
        assert storage_backend.get("email_smart_reply", "1:id-3") is None

    def test_already_drafted_ids_dropped_before_filter(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        emails = [_make_email(1), _make_email(2)]
        _wire_service(monkeypatch, emails)
        agent._mark_drafted(1, "id-1")

        captured: dict[str, Any] = {}

        def fake_ask(prompt: str, *, system=None, **kw):
            if system and "strict email filter" in system:
                captured["prompt"] = prompt
                return "[1]"
            return _OK_DRAFT

        _install_fake_node_llm_client(monkeypatch, fake_ask)

        _run(agent)

        # id-1 never reached the filter; index 1 now refers to id-2
        assert "Subject 1" not in captured["prompt"]
        assert "Subject 2" in captured["prompt"]
        assert len(inbox_backend.calls) == 1
        assert inbox_backend.calls[0]["title"] == "Reply ready — Sender 2"

    def test_all_drafted_skips_llm_entirely(self, agent, storage_backend, inbox_backend, monkeypatch):
        emails = [_make_email(1)]
        _wire_service(monkeypatch, emails)
        agent._mark_drafted(1, "id-1")
        ask = MagicMock()
        _install_fake_node_llm_client(monkeypatch, ask)

        _run(agent)

        ask.assert_not_called()
        assert inbox_backend.calls == []

    def test_draft_parse_fail_marks_declined_no_post(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm("[1]", ["garbage"]))

        _run(agent)

        assert inbox_backend.calls == []
        # Declined, not posted — suppresses re-judging but never delivery.
        record = storage_backend.get("email_smart_reply", "1:id-1")
        assert record is not None
        assert record["posted"] is False

    def test_should_reply_false_marks_declined_no_post(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(
            monkeypatch,
            _staged_ask_llm("[1]", ['{"should_reply": false, "draft": ""}']),
        )

        _run(agent)

        assert inbox_backend.calls == []
        record = storage_backend.get("email_smart_reply", "1:id-1")
        assert record is not None
        assert record["posted"] is False

    def test_declined_email_not_rejudged_next_run(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(
            monkeypatch,
            _staged_ask_llm("[1]", ['{"should_reply": false, "draft": ""}']),
        )
        _run(agent)
        assert storage_backend.get("email_smart_reply", "1:id-1")["posted"] is False

        # Next tick: the declined id never reaches the LLM again.
        ask = MagicMock()
        _install_fake_node_llm_client(monkeypatch, ask)
        _run(agent)

        ask.assert_not_called()
        assert inbox_backend.calls == []

    def test_llm_unreachable_at_draft_stage_does_not_mark(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm("[1]", [None]))

        _run(agent)

        assert inbox_backend.calls == []
        # Transient — must retry next run
        assert storage_backend.get("email_smart_reply", "1:id-1") is None

    def test_post_failure_does_not_mark_drafted(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        inbox_backend.tag = "http_error"
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm("[1]", [_OK_DRAFT]))

        _run(agent)

        assert len(inbox_backend.calls) == 1
        assert storage_backend.get("email_smart_reply", "1:id-1") is None

    def test_filter_fail_closed_no_posts(self, agent, storage_backend, inbox_backend, monkeypatch):
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm(None, []))

        _run(agent)
        assert inbox_backend.calls == []

    def test_service_construction_failure_swallowed(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        def boom():
            raise ValueError("not configured")

        monkeypatch.setattr(_agent_mod, "create_email_service", boom)
        _run(agent)  # must not raise
        assert inbox_backend.calls == []


# ── Per-match fetch failures: guarded, fed to health, user-scoped ────────────


class TestFetchConnectionFailure:
    """fetch_message sits after a successful search — its EmailConnectionError
    must be swallowed (scheduler keeps probing), feed the per-tick health
    aggregation (fetch-only outages accumulate to the notice threshold), stop
    only THIS user, and leave the email unmarked so it retries next tick."""

    def test_fetch_failure_reported_once_and_remaining_users_still_run(
        self, agent, storage_backend, inbox_backend, context_calls, monkeypatch
    ):
        monkeypatch.setattr(_agent_mod, "find_configured_user_ids", lambda: [1, 2])
        service = MagicMock()
        service.search.return_value = [_make_email(1)]
        service.fetch_message.side_effect = EmailConnectionError(
            "Couldn't connect to the email server at localhost:1143"
        )
        monkeypatch.setattr(_agent_mod, "create_email_service", lambda: service)
        _install_fake_node_llm_client(
            monkeypatch, _staged_ask_llm("[1]", [_OK_DRAFT] * 2)
        )

        _run(agent)  # must not raise (asyncio.run re-raises any escape)

        # Both users were processed — a fetch failure stops only ITS user.
        assert context_calls == [1, None, 2, None]
        assert service.search.call_count == 2
        # The tick recorded exactly ONE failure (per-tick aggregation), even
        # though both users' fetches died.
        record = storage_backend.get("email_health", "consecutive_failures")
        assert record["count"] == 1
        # Nothing posted, and the email is NOT marked drafted/declined — it
        # retries next tick.
        assert inbox_backend.calls == []
        assert storage_backend.get("email_smart_reply", "1:id-1") is None
        assert storage_backend.get("email_smart_reply", "2:id-1") is None

    def test_persistent_fetch_outage_accumulates_to_notice(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        # Search keeps working but fetch is down — three such ticks must
        # surface the outage notice exactly like a search outage.
        service = MagicMock()
        service.search.return_value = [_make_email(1)]
        service.fetch_message.side_effect = EmailConnectionError(
            "Couldn't connect to the email server at localhost:1143"
        )
        monkeypatch.setattr(_agent_mod, "create_email_service", lambda: service)
        _install_fake_node_llm_client(
            monkeypatch, _staged_ask_llm("[1]", [_OK_DRAFT] * 3)
        )

        _run(agent)
        _run(agent)
        assert inbox_backend.calls == []

        _run(agent)
        assert len(inbox_backend.calls) == 1
        assert inbox_backend.calls[0]["title"] == "Email connection problem"


# ── Deterministic bulk-sender screen (pre-LLM) ───────────────────────────────


def _bulk_email(idx: int) -> SimpleNamespace:
    e = _make_email(idx)
    e.sender = f"Shop {idx} <noreply@shop{idx}.example.com>"
    return e


class TestBulkSenderScreen:
    def test_all_bulk_candidates_never_reach_llm(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        # Every candidate is a bulk sender — the LLM is never consulted and
        # nothing is posted (the field-incident regression guard).
        _wire_service(monkeypatch, [_bulk_email(1), _bulk_email(2)])
        ask = MagicMock()
        _install_fake_node_llm_client(monkeypatch, ask)

        _run(agent)

        ask.assert_not_called()
        assert inbox_backend.calls == []

    def test_unsubscribe_signal_screens_human_looking_sender(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        # marketplace-messages@amazon.com style: human-ish local part, but the
        # List-Unsubscribe header marks it bulk.
        e = _make_email(1)
        e.sender = "Amazon <marketplace-messages@amazon.com>"
        e.unsubscribe_url = "https://amazon.com/unsub"
        _wire_service(monkeypatch, [e])
        ask = MagicMock()
        _install_fake_node_llm_client(monkeypatch, ask)

        _run(agent)

        ask.assert_not_called()
        assert inbox_backend.calls == []

    def test_mixed_candidates_only_human_reaches_filter(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        emails = [_bulk_email(1), _make_email(2)]
        _wire_service(monkeypatch, emails)

        captured: dict[str, Any] = {}

        def fake_ask(prompt: str, *, system=None, **kw):
            if system and "strict email filter" in system:
                captured["prompt"] = prompt
                return "[1]"  # index 1 is now the human email (id-2)
            return _OK_DRAFT

        _install_fake_node_llm_client(monkeypatch, fake_ask)

        _run(agent)

        assert "Subject 1" not in captured["prompt"]
        assert "Subject 2" in captured["prompt"]
        assert len(inbox_backend.calls) == 1
        assert inbox_backend.calls[0]["title"] == "Reply ready — Sender 2"

    def test_screened_bulk_not_marked_drafted(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        # Screening is deterministic and free — no need to burn a dedup
        # record; the screen drops them again next run.
        _wire_service(monkeypatch, [_bulk_email(1)])
        ask = MagicMock()
        _install_fake_node_llm_client(monkeypatch, ask)

        _run(agent)

        assert storage_backend.get("email_smart_reply", "1:id-1") is None

    def test_filter_system_prompt_excludes_automated_senders(
        self, agent, monkeypatch
    ):
        captured: dict[str, Any] = {}

        def fake_ask(prompt: str, *, system=None, **kw):
            captured["system"] = system
            return "[]"

        _install_fake_node_llm_client(monkeypatch, fake_ask)
        _agent_mod.select_reply_worthy([_make_email(1)], "clients", None)

        assert "NEVER reply-worthy" in captured["system"]
        assert "no-reply" in captured["system"]


# ── Persistent dedup ─────────────────────────────────────────────────────────


class TestPersistentDedup:
    def test_mark_and_check(self, agent, storage_backend):
        assert agent._already_drafted(1, "id-1") is False
        agent._mark_drafted(1, "id-1")
        assert agent._already_drafted(1, "id-1") is True

    def test_dedup_is_per_user(self, agent, storage_backend):
        agent._mark_drafted(1, "id-1")
        assert agent._already_drafted(1, "id-1") is True
        # Same message id for another user is NOT drafted
        assert agent._already_drafted(2, "id-1") is False

    def test_record_shape_and_ttl(self, agent, storage_backend):
        before = datetime.now(timezone.utc)
        agent._mark_drafted(1, "id-1")
        after = datetime.now(timezone.utc)

        record = storage_backend.records[("email_smart_reply", "1:id-1")]
        drafted_at = datetime.fromisoformat(record["drafted_at"])
        assert before <= drafted_at <= after
        assert record["posted"] is True  # a real post, not a decline

        expires_at = storage_backend.expires[("email_smart_reply", "1:id-1")]
        assert before + timedelta(days=7) <= expires_at <= after + timedelta(days=7)

    def test_survives_restart(self, agent, storage_backend):
        agent._mark_drafted(1, "id-1")
        fresh_agent = _agent_mod.SmartReplyAgent()
        assert fresh_agent._already_drafted(1, "id-1") is True

    def test_expired_record_means_not_drafted(self, agent, storage_backend):
        key = ("email_smart_reply", "1:id-1")
        storage_backend.records[key] = {"drafted_at": "2026-01-01T00:00:00+00:00"}
        storage_backend.expires[key] = datetime.now(timezone.utc) - timedelta(days=1)
        assert agent._already_drafted(1, "id-1") is False

    def test_no_backend_never_drafted(self, agent):
        # Facade returns None without a backend — agent treats as not drafted
        assert agent._already_drafted(1, "id-1") is False
        agent._mark_drafted(1, "id-1")  # no-op, must not raise


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


# ── Per-user execution: ContextVar lifecycle, user-targeted posts, caps ──────


class TestPerUserRun:
    def test_context_var_set_then_reset(
        self, agent, storage_backend, inbox_backend, context_calls, monkeypatch
    ):
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm("[1]", [_OK_DRAFT]))

        _run(agent)

        assert context_calls == [1, None]

    def test_context_var_reset_even_when_user_flow_raises(
        self, agent, storage_backend, inbox_backend, context_calls, monkeypatch
    ):
        def boom(uid, filter_text, budget):
            raise RuntimeError("mailbox exploded")

        monkeypatch.setattr(agent, "_run_for_user", boom)

        _run(agent)  # outer try swallows; must not raise

        assert context_calls == [1, None]

    def test_posts_are_user_targeted_per_user(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        monkeypatch.setattr(_agent_mod, "find_configured_user_ids", lambda: [1, 2])
        _wire_service(monkeypatch, [_make_email(1)])
        # Each user's filter pass matches the single email; one draft each.
        _install_fake_node_llm_client(
            monkeypatch, _staged_ask_llm("[1]", [_OK_DRAFT] * 2)
        )

        _run(agent)

        assert [(c["user_id"], c["target_type"]) for c in inbox_backend.calls] == [
            (1, "user"),
            (2, "user"),
        ]
        # Dedup keys are uid-prefixed — one per user for the same message id
        assert storage_backend.get("email_smart_reply", "1:id-1") is not None
        assert storage_backend.get("email_smart_reply", "2:id-1") is not None

    def test_draft_cap_is_global_across_users(
        self, agent, storage_backend, inbox_backend, context_calls, monkeypatch
    ):
        monkeypatch.setattr(_agent_mod, "find_configured_user_ids", lambda: [1, 2])
        _wire_service(monkeypatch, [_make_email(i) for i in range(1, 5)])
        _install_fake_node_llm_client(
            monkeypatch, _staged_ask_llm("[1, 2, 3, 4]", [_OK_DRAFT] * 4)
        )

        _run(agent)

        # User 1 exhausts MAX_DRAFTS_PER_RUN; user 2 is never entered.
        assert len(inbox_backend.calls) == 2
        assert all(c["user_id"] == 1 for c in inbox_backend.calls)
        assert context_calls == [1, None]

    def test_users_capped_at_five_per_run(
        self, agent, storage_backend, inbox_backend, context_calls, monkeypatch
    ):
        monkeypatch.setattr(
            _agent_mod, "find_configured_user_ids", lambda: [1, 2, 3, 4, 5, 6, 7]
        )
        _wire_service(monkeypatch, [])  # no mail — every user is a quick no-op

        _run(agent)

        visited = [c for c in context_calls if c is not None]
        assert visited == [1, 2, 3, 4, 5]
