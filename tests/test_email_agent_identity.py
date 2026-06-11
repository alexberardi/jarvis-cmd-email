"""EMAIL_AGENT_USER — the explicit agent identity (value_type "user").

In a multi-user household the background agents must never fan notifications
to everyone or to the wrong person. EMAIL_AGENT_USER (integration scope,
value_type "user" — the mobile app stores the picked member's user id as a
string) pins both agents to ONE household member. Coverage:

- resolve_agent_user_ids matrix: unset/blank → auto behavior (every
  mailbox-configured user, zero-config single-user households unchanged);
  set + configured mailbox → exactly that uid; set + NO configured mailbox →
  [] plus a warning (an EXPLICIT identity must never silently fall back to
  other users); unparseable value → [] plus a warning.
- Both agents run only the identity user and their posts target that user;
  with an unconfigured identity the agents idle — they probe NO mailbox at
  all, not other users'.
- The connection-outage notice posts user-targeted when the identity is set,
  household-wide as before when it isn't.
- The secret is declared on both agents and jarvis_package.yaml is aligned.
"""

import asyncio
import importlib.util
import os
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

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
_ur_mod = sys.modules["email_shared.user_resolution"]
_alerts_mod = _load_real(
    "email_alerts_agent_for_identity", "agents", "email_alerts", "agent.py"
)
_smart_mod = _load_real(
    "smart_reply_agent_for_identity", "agents", "smart_reply", "agent.py"
)

EmailConnectionError = sys.modules["email_shared.email_message"].EmailConnectionError

_DESCRIPTION = "Couldn't connect to the Proton Mail Bridge at 127.0.0.1:1143"

_IDENTITY_DESCRIPTION = (
    "The household member this agent runs as and notifies. "
    "Leave unset to run for every member with a configured mailbox."
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
        self.expires.pop((command_name, data_key), None)
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


class _RecordingLogger:
    """Captures warning messages so tests can assert the identity warnings."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def info(self, *args, **kwargs): ...
    def error(self, *args, **kwargs): ...
    def debug(self, *args, **kwargs): ...

    def warning(self, msg, *args, **kwargs):
        self.warnings.append(msg)


@pytest.fixture
def storage_backend():
    backend = _FakeStorageBackend()
    # smart_reply only probes when its filter is set; keep the digest quiet.
    backend.secrets[("EMAIL_NOTIFICATION_FILTER", "integration")] = "clients"
    quiet_hour = (datetime.now(timezone.utc).hour + 2) % 24
    backend.secrets[("EMAIL_ALERT_DIGEST_HOUR", "integration")] = str(quiet_hour)
    set_backend(backend)
    yield backend
    set_backend(None)


@pytest.fixture
def inbox_backend():
    backend = _FakeInboxBackend()
    set_inbox_backend(backend)
    yield backend
    set_inbox_backend(None)


@pytest.fixture
def resolution_warnings(monkeypatch):
    recorder = _RecordingLogger()
    monkeypatch.setattr(_ur_mod, "logger", recorder)
    return recorder.warnings


def _set_identity(storage_backend, value: str) -> None:
    storage_backend.secrets[("EMAIL_AGENT_USER", "integration")] = value


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


_OK_DRAFT = '{"should_reply": true, "draft": "Sounds good — see you then."}'


def _run(agent) -> None:
    asyncio.run(agent.run())


def _context_calls(monkeypatch, module) -> list:
    calls: list[int | None] = []
    monkeypatch.setattr(module, "set_current_user_id", calls.append)
    return calls


# ── resolve_agent_user_ids matrix ────────────────────────────────────────────


class TestResolveAgentUserIds:
    def test_unset_returns_all_configured_users(self, storage_backend):
        assert _ur_mod.resolve_agent_user_ids(lambda: [1, 2]) == [1, 2]

    def test_blank_value_returns_all_configured_users(self, storage_backend):
        _set_identity(storage_backend, "   ")
        assert _ur_mod.resolve_agent_user_ids(lambda: [1, 2]) == [1, 2]

    def test_no_storage_backend_returns_all_configured_users(self):
        # Facade no-ops without a backend — identical to unset.
        assert _ur_mod.resolve_agent_user_ids(lambda: [1]) == [1]

    def test_set_and_configured_returns_only_that_uid(self, storage_backend):
        _set_identity(storage_backend, "2")
        assert _ur_mod.resolve_agent_user_ids(lambda: [1, 2, 3]) == [2]

    def test_value_is_stripped_before_parsing(self, storage_backend):
        _set_identity(storage_backend, " 2 ")
        assert _ur_mod.resolve_agent_user_ids(lambda: [2]) == [2]

    def test_set_without_configured_mailbox_idles_with_warning(
        self, storage_backend, resolution_warnings
    ):
        # An EXPLICIT identity must never silently fall back to other users.
        _set_identity(storage_backend, "7")
        assert _ur_mod.resolve_agent_user_ids(lambda: [1, 2]) == []
        assert any(
            "EMAIL_AGENT_USER is set to 7" in w and "no configured mailbox" in w
            for w in resolution_warnings
        )

    def test_unparseable_value_idles_with_warning(
        self, storage_backend, resolution_warnings
    ):
        _set_identity(storage_backend, "alex")
        assert _ur_mod.resolve_agent_user_ids(lambda: [1, 2]) == []
        assert any("EMAIL_AGENT_USER" in w for w in resolution_warnings)

    def test_default_lookup_is_find_configured_user_ids(
        self, storage_backend, monkeypatch
    ):
        monkeypatch.setattr(_ur_mod, "find_configured_user_ids", lambda: [3])
        _set_identity(storage_backend, "3")
        assert _ur_mod.resolve_agent_user_ids() == [3]


class TestConfiguredAgentUserId:
    def test_unset_is_none(self, storage_backend):
        assert _ur_mod.configured_agent_user_id() is None

    def test_set_parses_the_stored_string(self, storage_backend):
        _set_identity(storage_backend, "5")
        assert _ur_mod.configured_agent_user_id() == 5

    def test_unparseable_is_none(self, storage_backend):
        _set_identity(storage_backend, "alex")
        assert _ur_mod.configured_agent_user_id() is None


# ── Agents pinned to the identity user ───────────────────────────────────────


class TestAgentsPinnedToIdentity:
    def test_alerts_agent_runs_only_identity_user_and_posts_target_them(
        self, storage_backend, inbox_backend, monkeypatch
    ):
        _set_identity(storage_backend, "1")
        storage_backend.secrets[("EMAIL_ALERT_VIP_SENDERS", "integration")] = (
            "sender1@example.com"
        )
        monkeypatch.setattr(_alerts_mod, "find_configured_user_ids", lambda: [1, 2])
        calls = _context_calls(monkeypatch, _alerts_mod)
        constructed: list[MagicMock] = []

        def make_service():
            service = MagicMock()
            service.search.return_value = [_make_email(1)]
            constructed.append(service)
            return service

        monkeypatch.setattr(_alerts_mod, "create_email_service", make_service)

        _run(_alerts_mod.EmailAlertAgent())

        # Only uid 1's mailbox was probed — uid 2 never ran.
        assert calls == [1, None]
        assert len(constructed) == 1
        # And the VIP post targets the identity user.
        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["target_type"] == "user"
        assert call["user_id"] == 1

    def test_smart_reply_agent_runs_only_identity_user_and_posts_target_them(
        self, storage_backend, inbox_backend, monkeypatch
    ):
        _set_identity(storage_backend, "1")
        monkeypatch.setattr(_smart_mod, "find_configured_user_ids", lambda: [1, 2])
        calls = _context_calls(monkeypatch, _smart_mod)
        emails = [_make_email(1)]
        constructed: list[MagicMock] = []

        def make_service():
            service = MagicMock()
            service.search.return_value = emails
            service.fetch_message.side_effect = (
                lambda mid, max_body_chars=1000: next(
                    (e for e in emails if e.id == mid), None
                )
            )
            constructed.append(service)
            return service

        monkeypatch.setattr(_smart_mod, "create_email_service", make_service)
        _install_fake_node_llm_client(monkeypatch, _staged_ask_llm("[1]", [_OK_DRAFT]))

        _run(_smart_mod.SmartReplyAgent())

        assert calls == [1, None]
        assert len(constructed) == 1
        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["target_type"] == "user"
        assert call["user_id"] == 1

    def test_alerts_agent_idles_when_identity_has_no_mailbox(
        self, storage_backend, inbox_backend, resolution_warnings, monkeypatch
    ):
        # Identity user 7 has no configured mailbox — the agent must probe
        # NOBODY (not fall back to users 1/2) and warn.
        _set_identity(storage_backend, "7")
        monkeypatch.setattr(_alerts_mod, "find_configured_user_ids", lambda: [1, 2])
        calls = _context_calls(monkeypatch, _alerts_mod)
        service_factory = MagicMock()
        monkeypatch.setattr(_alerts_mod, "create_email_service", service_factory)

        agent = _alerts_mod.EmailAlertAgent()
        _run(agent)

        assert calls == []
        service_factory.assert_not_called()
        assert inbox_backend.calls == []
        assert agent.get_alerts() == []
        assert any(
            "EMAIL_AGENT_USER is set to 7" in w and "no configured mailbox" in w
            for w in resolution_warnings
        )

    def test_smart_reply_agent_idles_when_identity_has_no_mailbox(
        self, storage_backend, inbox_backend, resolution_warnings, monkeypatch
    ):
        _set_identity(storage_backend, "7")
        monkeypatch.setattr(_smart_mod, "find_configured_user_ids", lambda: [1, 2])
        calls = _context_calls(monkeypatch, _smart_mod)
        service_factory = MagicMock()
        monkeypatch.setattr(_smart_mod, "create_email_service", service_factory)

        _run(_smart_mod.SmartReplyAgent())

        assert calls == []
        service_factory.assert_not_called()
        assert inbox_backend.calls == []
        assert any(
            "EMAIL_AGENT_USER is set to 7" in w and "no configured mailbox" in w
            for w in resolution_warnings
        )


# ── Outage notice targeting ──────────────────────────────────────────────────


def _wire_failing_search(monkeypatch, module) -> MagicMock:
    service = MagicMock()
    service.search.side_effect = EmailConnectionError(_DESCRIPTION)
    monkeypatch.setattr(module, "create_email_service", lambda: service)
    return service


class TestOutageNoticeTargeting:
    def test_notice_is_user_targeted_when_identity_set(
        self, storage_backend, inbox_backend, monkeypatch
    ):
        _set_identity(storage_backend, "1")
        monkeypatch.setattr(_alerts_mod, "find_configured_user_ids", lambda: [1])
        _wire_failing_search(monkeypatch, _alerts_mod)
        alerts = _alerts_mod.EmailAlertAgent()

        _run(alerts)
        _run(alerts)
        assert inbox_backend.calls == []

        _run(alerts)  # 3rd consecutive failure tick — crosses the threshold
        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["title"] == "Email connection problem"
        assert call["target_type"] == "user"
        assert call["user_id"] == 1

    def test_smart_reply_notice_is_user_targeted_when_identity_set(
        self, storage_backend, inbox_backend, monkeypatch
    ):
        _set_identity(storage_backend, "1")
        monkeypatch.setattr(_smart_mod, "find_configured_user_ids", lambda: [1])
        _wire_failing_search(monkeypatch, _smart_mod)
        smart = _smart_mod.SmartReplyAgent()

        for _ in range(3):
            _run(smart)

        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["target_type"] == "user"
        assert call["user_id"] == 1

    def test_notice_stays_household_wide_without_identity(
        self, storage_backend, inbox_backend, monkeypatch
    ):
        monkeypatch.setattr(_alerts_mod, "find_configured_user_ids", lambda: [1])
        _wire_failing_search(monkeypatch, _alerts_mod)
        alerts = _alerts_mod.EmailAlertAgent()

        for _ in range(3):
            _run(alerts)

        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["target_type"] == "household"
        assert call["user_id"] is None


# ── Declaration: both agents + manifest aligned ──────────────────────────────


def _identity_secret(agent) -> Any:
    secrets = {s.key: s for s in agent.required_secrets}
    return secrets["EMAIL_AGENT_USER"]


class TestDeclaration:
    @pytest.mark.parametrize(
        "agent",
        [_alerts_mod.EmailAlertAgent(), _smart_mod.SmartReplyAgent()],
        ids=["email_alerts", "smart_reply"],
    )
    def test_both_agents_declare_the_identity_secret(self, agent):
        secret = _identity_secret(agent)
        assert secret.scope == "integration"
        assert secret.value_type == "user"
        assert secret.required is False
        assert secret.is_sensitive is False
        assert secret.friendly_name == "Notify user"
        assert secret.description == _IDENTITY_DESCRIPTION

    def test_manifest_is_aligned_with_the_agents(self):
        with open(os.path.join(_ROOT, "jarvis_package.yaml")) as f:
            manifest = yaml.safe_load(f)
        entries = [s for s in manifest["secrets"] if s["key"] == "EMAIL_AGENT_USER"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["scope"] == "integration"
        assert entry["value_type"] == "user"
        assert entry["is_sensitive"] is False
        assert entry["description"] == _IDENTITY_DESCRIPTION
