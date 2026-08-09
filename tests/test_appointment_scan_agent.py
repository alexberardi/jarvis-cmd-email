"""Appointment-scan agent — fail-closed extraction, proposable-action card, dedup.

This agent is the "an agent proposes another package's command" pilot: it
scans unread mail, LLM-extracts a single appointment (fail-closed), and posts
a tap-to-confirm card that proposes the calendar command's create_event
action via JarvisInbox.propose_action. The card routes to command-center's
generic server-plane dispatcher (jarvis.proposable_action.execute) — see the
SDK's JarvisInbox.propose_action.
"""

import asyncio
import importlib.util
import json
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
    "appointment_scan_agent_under_test", "agents", "appointment_scan", "agent.py"
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


def _appointment_json(
    has: bool = True,
    title: str = "Dentist cleaning",
    start: str = "2026-08-12T15:00:00",
    end: str = "2026-08-12T16:00:00",
    location: str = "123 Main St",
) -> str:
    return json.dumps(
        {
            "has_appointment": has,
            "title": title,
            "start": start,
            "end": end,
            "location": location,
        }
    )


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
    return _agent_mod.AppointmentScanAgent()


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


def _confirm_element(call: dict) -> dict:
    """The Confirm interactive element from a posted proposable-action card."""
    return call["metadata"]["interactive_elements"][0]


def _install_fake_proposal_client(monkeypatch, signals: dict) -> None:
    """Make `from services.proposal_client import get_suppression_signals` resolve."""
    services_mod = sys.modules.get("services") or types.ModuleType("services")
    monkeypatch.setitem(sys.modules, "services", services_mod)
    pc = types.ModuleType("services.proposal_client")
    pc.get_suppression_signals = lambda command, user_id: signals  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.proposal_client", pc)
    services_mod.proposal_client = pc  # type: ignore[attr-defined]


# ── Declaration ──────────────────────────────────────────────────────────────


class TestDeclaration:
    def test_name_and_description(self, agent):
        assert agent.name == "appointment_scan"
        assert isinstance(agent.description, str) and agent.description

    def test_schedule(self, agent):
        schedule = agent.schedule
        assert schedule.interval_seconds == 300
        assert schedule.run_on_startup is False

    def test_agent_user_secret_declared(self, agent):
        secrets = {s.key: s for s in agent.required_secrets}
        # Reuses the SAME EMAIL_AGENT_USER identity secret as the other agents.
        secret = secrets["EMAIL_AGENT_USER"]
        assert secret.scope == "integration"
        assert secret.value_type == "user"
        assert secret.required is False

    def test_not_in_context(self, agent):
        assert agent.include_in_context is False
        assert agent.get_context_data() == {}
        assert agent.get_alerts() == []


# ── Fail-closed LLM extraction ───────────────────────────────────────────────


class TestExtraction:
    def test_valid_appointment(self, agent, monkeypatch):
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: _appointment_json())
        appt = agent._extract_appointment(_make_email(1))
        assert appt == {
            "title": "Dentist cleaning",
            "start": "2026-08-12T15:00:00",
            "end": "2026-08-12T16:00:00",
            "location": "123 Main St",
        }

    def test_no_appointment_is_none(self, agent, monkeypatch):
        _install_fake_node_llm_client(
            monkeypatch, lambda *a, **kw: _appointment_json(has=False)
        )
        assert agent._extract_appointment(_make_email(1)) is None

    def test_missing_start_is_none(self, agent, monkeypatch):
        _install_fake_node_llm_client(
            monkeypatch, lambda *a, **kw: _appointment_json(start="")
        )
        assert agent._extract_appointment(_make_email(1)) is None

    def test_missing_title_is_none(self, agent, monkeypatch):
        _install_fake_node_llm_client(
            monkeypatch, lambda *a, **kw: _appointment_json(title="")
        )
        assert agent._extract_appointment(_make_email(1)) is None

    def test_code_fences_stripped(self, agent, monkeypatch):
        raw = "```json\n" + _appointment_json(end="", location="") + "\n```"
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: raw)
        appt = agent._extract_appointment(_make_email(1))
        assert appt is not None
        assert appt["title"] == "Dentist cleaning"
        assert appt["end"] == ""
        assert appt["location"] == ""

    def test_think_tags_stripped(self, agent, monkeypatch):
        raw = "<think>reasoning...</think>" + _appointment_json()
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: raw)
        assert agent._extract_appointment(_make_email(1)) is not None

    def test_garbage_is_none(self, agent, monkeypatch):
        _install_fake_node_llm_client(
            monkeypatch, lambda *a, **kw: "Sure! There's a meeting soon."
        )
        assert agent._extract_appointment(_make_email(1)) is None

    def test_empty_response_is_none(self, agent, monkeypatch):
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: None)
        assert agent._extract_appointment(_make_email(1)) is None

    def test_llm_unavailable_is_none(self, agent, monkeypatch):
        monkeypatch.setitem(sys.modules, "services.node_llm_client", None)
        assert agent._extract_appointment(_make_email(1)) is None

    def test_prompt_carries_full_body(self, agent, monkeypatch):
        captured: dict[str, Any] = {}

        def fake_ask(prompt: str, *, system=None, **kw):
            captured["prompt"] = prompt
            captured["system"] = system
            return _appointment_json()

        _install_fake_node_llm_client(monkeypatch, fake_ask)
        agent._extract_appointment(_make_email(1))

        assert "Body 1" in captured["prompt"]
        assert "has_appointment" in captured["system"]


# ── (a) run(): an appointment email posts a proposable-action card ───────────


class TestProposesAction:
    def test_appointment_posts_proposable_card(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: _appointment_json())

        _run(agent)

        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        # Posted by the agent's own inbox facade, generic proposal category.
        assert call["command_name"] == "appointment_scan"
        assert call["category"] == "proposable_action"
        assert call["title"] == "Add to your calendar?"
        assert call["summary"] == "Subject 1"
        assert call["user_id"] == 1
        assert call["target_type"] == "user"
        assert call["create_push_notification"] is True

        confirm = _confirm_element(call)
        assert confirm["command"] == "jarvis.proposable_action"
        assert confirm["callback"] == "execute"
        action = confirm["data"]["_action"]
        assert action["target_command"] == "add_event"
        assert action["target_callback"] == "create_event"
        # The extracted params ride at the top level so mobile field-edits merge.
        assert confirm["data"]["title"] == "Dentist cleaning"
        assert confirm["data"]["start"] == "2026-08-12T15:00:00"
        # Idempotency handle is stable per message.
        assert action["idempotency_key"] == "appt:id-1"

    def test_posted_id_is_marked_seen(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: _appointment_json())

        _run(agent)

        # Dedup lives in the agent's own namespace, keyed per-user.
        assert storage_backend.get("email_appointment_scan", "1:id-1") is not None

    def test_fetch_uses_bounded_body(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        service = _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: _appointment_json())

        _run(agent)

        service.search.assert_called_once_with(
            "is:unread in:inbox newer_than:1d", max_results=20
        )
        service.fetch_message.assert_called_once_with("id-1", max_body_chars=1000)

    def test_post_failure_does_not_mark_seen(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        inbox_backend.tag = "http_error"
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: _appointment_json())

        _run(agent)

        assert len(inbox_backend.calls) == 1
        # Transient post failure — retries next run.
        assert storage_backend.get("email_appointment_scan", "1:id-1") is None


# ── (b) run(): no appointment → nothing posted ──────────────────────────────


class TestNoAppointment:
    def test_no_appointment_posts_nothing(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(
            monkeypatch, lambda *a, **kw: _appointment_json(has=False)
        )

        _run(agent)

        assert inbox_backend.calls == []
        # Nothing extracted → nothing marked seen (retry next run in case the
        # LLM was transiently wrong is acceptable; extraction is fail-closed).
        assert storage_backend.get("email_appointment_scan", "1:id-1") is None

    def test_llm_down_posts_nothing(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: None)

        _run(agent)

        assert inbox_backend.calls == []


# ── (c) run(): dedup — a second scan over the same email doesn't repost ─────


class TestDedup:
    def test_second_run_over_same_email_does_not_repost(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: _appointment_json())

        _run(agent)
        assert len(inbox_backend.calls) == 1

        # Second tick, same unread email still present. It must NOT reach the
        # LLM again and must NOT post a second card.
        ask = MagicMock()
        _install_fake_node_llm_client(monkeypatch, ask)

        _run(agent)

        ask.assert_not_called()
        assert len(inbox_backend.calls) == 1

    def test_already_seen_id_skips_fetch_and_llm(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        service = _wire_service(monkeypatch, [_make_email(1)])
        agent._mark_seen(1, "id-1")
        ask = MagicMock()
        _install_fake_node_llm_client(monkeypatch, ask)

        _run(agent)

        ask.assert_not_called()
        service.fetch_message.assert_not_called()
        assert inbox_backend.calls == []

    def test_dedup_is_per_user(self, agent, storage_backend):
        agent._mark_seen(1, "id-1")
        assert agent._already_seen(1, "id-1") is True
        assert agent._already_seen(2, "id-1") is False

    def test_record_shape_and_ttl(self, agent, storage_backend):
        before = datetime.now(timezone.utc)
        agent._mark_seen(1, "id-1")
        after = datetime.now(timezone.utc)

        expires_at = storage_backend.expires[("email_appointment_scan", "1:id-1")]
        assert before + timedelta(days=7) <= expires_at <= after + timedelta(days=7)


# ── (d) run(): per-user ContextVar lifecycle ────────────────────────────────


class TestPerUserRun:
    def test_context_var_set_then_reset(
        self, agent, storage_backend, inbox_backend, context_calls, monkeypatch
    ):
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: _appointment_json())

        _run(agent)

        assert context_calls == [1, None]

    def test_context_var_reset_even_when_user_flow_raises(
        self, agent, storage_backend, inbox_backend, context_calls, monkeypatch
    ):
        def boom(uid, budget):
            raise RuntimeError("mailbox exploded")

        monkeypatch.setattr(agent, "_run_for_user", boom)

        _run(agent)  # outer try swallows; must not raise

        assert context_calls == [1, None]

    def test_posts_are_user_targeted_per_user(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        monkeypatch.setattr(_agent_mod, "find_configured_user_ids", lambda: [1, 2])
        _wire_service(monkeypatch, [_make_email(1)])
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: _appointment_json())

        _run(agent)

        assert [(c["user_id"], c["target_type"]) for c in inbox_backend.calls] == [
            (1, "user"),
            (2, "user"),
        ]
        assert storage_backend.get("email_appointment_scan", "1:id-1") is not None
        assert storage_backend.get("email_appointment_scan", "2:id-1") is not None


# ── run(): resilience — never raises, swallows service/connection errors ────


class TestResilience:
    def test_zero_configured_users_is_noop(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        monkeypatch.setattr(_agent_mod, "find_configured_user_ids", lambda: [])
        create = MagicMock()
        monkeypatch.setattr(_agent_mod, "create_email_service", create)

        _run(agent)

        create.assert_not_called()
        assert inbox_backend.calls == []

    def test_service_construction_failure_swallowed(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        def boom():
            raise ValueError("not configured")

        monkeypatch.setattr(_agent_mod, "create_email_service", boom)
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: _appointment_json())

        _run(agent)  # must not raise
        assert inbox_backend.calls == []

    def test_search_connection_error_swallowed(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        service = MagicMock()
        service.search.side_effect = EmailConnectionError(
            "Couldn't connect to the email server at localhost:1143"
        )
        monkeypatch.setattr(_agent_mod, "create_email_service", lambda: service)

        _run(agent)  # must not raise
        assert inbox_backend.calls == []

    def test_fetch_connection_error_swallowed(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        service = MagicMock()
        service.search.return_value = [_make_email(1)]
        service.fetch_message.side_effect = EmailConnectionError(
            "Couldn't connect to the email server at localhost:1143"
        )
        monkeypatch.setattr(_agent_mod, "create_email_service", lambda: service)
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: _appointment_json())

        _run(agent)  # must not raise
        assert inbox_backend.calls == []
        assert storage_backend.get("email_appointment_scan", "1:id-1") is None


# ── 'Never suggest this again' — blocklist consumption ───────────────────────


class TestSuppression:
    def test_blocked_sender_skipped_before_fetch_and_llm(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        emails = [_make_email(1)]  # sender1@example.com
        service = _wire_service(monkeypatch, emails)
        _install_fake_proposal_client(
            monkeypatch, {"source_keys": ["sender1@example.com"], "descriptors": []}
        )
        llm = {"called": False}

        def _ask(prompt, system=None):
            llm["called"] = True
            return _appointment_json()

        _install_fake_node_llm_client(monkeypatch, _ask)

        _run(agent)

        assert inbox_backend.calls == []          # no card for a blocked sender
        assert llm["called"] is False             # skipped before the LLM spend
        service.fetch_message.assert_not_called()  # and before the fetch

    def test_descriptors_injected_into_extraction_prompt(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_make_email(2)])
        _install_fake_proposal_client(
            monkeypatch, {"source_keys": [], "descriptors": ["CI/build-failure notifications"]}
        )
        seen = {}

        def _ask(prompt, system=None):
            seen["system"] = system
            return _appointment_json(has=False)

        _install_fake_node_llm_client(monkeypatch, _ask)

        _run(agent)

        assert "CI/build-failure notifications" in seen["system"]

    def test_card_carries_suppress_source_and_descriptor(
        self, agent, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_service(monkeypatch, [_make_email(3)])  # sender3@example.com, "Subject 3"
        _install_fake_proposal_client(monkeypatch, {"source_keys": [], "descriptors": []})
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: _appointment_json())

        _run(agent)

        els = inbox_backend.calls[0]["metadata"]["interactive_elements"]
        suppress = next(e for e in els if e["callback"] == "suppress")
        assert suppress["data"]["source"] == "sender3@example.com"
        assert "Subject 3" in suppress["data"]["descriptor"]
