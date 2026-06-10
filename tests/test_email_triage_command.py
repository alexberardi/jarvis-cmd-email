"""New email verbs + triage — dispatch, fast-paths, inbox post, and callbacks."""

import importlib.util
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jarvis_command_sdk import RequestInformation, set_inbox_backend
from jarvis_command_sdk.inbox import InboxBackend

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))


def _load_real(name: str, filename: str):
    path = os.path.join(_ROOT, "email_shared", filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _stub_log_client() -> None:
    """No-op logger so error-path tests don't queue real log batches."""
    stub = types.ModuleType("jarvis_log_client")

    class _Logger:
        def __init__(self, **kwargs): ...
        def info(self, *args, **kwargs): ...
        def warning(self, *args, **kwargs): ...
        def error(self, *args, **kwargs): ...
        def debug(self, *args, **kwargs): ...

    stub.JarvisLogger = _Logger
    sys.modules["jarvis_log_client"] = stub


def _install_email_shared():
    """Real email_message + triage; stub factory (no live service construction)."""
    _stub_log_client()
    if "email_shared" not in sys.modules:
        sys.modules["email_shared"] = types.ModuleType("email_shared")
    _load_real("email_shared.email_message", "email_message.py")
    if "email_shared.email_service_factory" not in sys.modules:
        esf = types.ModuleType("email_shared.email_service_factory")
        esf.create_email_service = lambda: None
        esf.get_email_provider = lambda: "gmail"
        sys.modules["email_shared.email_service_factory"] = esf
    _load_real("email_shared.triage", "triage.py")


_install_email_shared()


def _load_command():
    cmd_path = os.path.join(_ROOT, "commands", "email", "command.py")
    spec = importlib.util.spec_from_file_location("email_triage_cmd_under_test", cmd_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EmailCommand


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


def _make_req(user_id: int | None = 42, is_pre_routed: bool = False) -> RequestInformation:
    return RequestInformation(
        voice_command="triage my inbox",
        conversation_id="conv-1",
        user_id=user_id,
        is_pre_routed=is_pre_routed,
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
def cmd():
    instance = _load_command()()
    # Pass the run() auth gate (gmail provider, token present)
    instance._storage = MagicMock()
    instance._storage.get_secret.return_value = "tok"
    return instance


def _wire_service(cmd, emails: list[SimpleNamespace]) -> MagicMock:
    service = MagicMock()
    cmd._get_service = MagicMock(return_value=service)
    cmd._last_email_list = list(emails)
    return service


# ── Dispatch: mark_read / mark_unread / unstar / forward ────────────────────


class TestVerbDispatch:
    def test_mark_read(self, cmd):
        service = _wire_service(cmd, [_make_email(1), _make_email(2)])
        service.mark_read.return_value = True

        resp = cmd.run(_make_req(), action="mark_read", email_index=2)

        service.mark_read.assert_called_once_with("id-2")
        assert resp.success
        assert resp.context_data["message"] == "Marked as read: Subject 2"

    def test_mark_read_failure(self, cmd):
        service = _wire_service(cmd, [_make_email(1)])
        service.mark_read.return_value = False

        resp = cmd.run(_make_req(), action="mark_read", email_index=1)
        assert not resp.success

    def test_mark_read_requires_index(self, cmd):
        _wire_service(cmd, [_make_email(1)])
        resp = cmd.run(_make_req(), action="mark_read")
        assert not resp.success

    def test_mark_unread(self, cmd):
        service = _wire_service(cmd, [_make_email(1)])
        service.mark_unread.return_value = True

        resp = cmd.run(_make_req(), action="mark_unread", email_index=1)

        service.mark_unread.assert_called_once_with("id-1")
        assert resp.context_data["message"] == "Marked as unread: Subject 1"

    def test_unstar(self, cmd):
        service = _wire_service(cmd, [_make_email(1)])
        service.unstar.return_value = True

        resp = cmd.run(_make_req(), action="unstar", email_index=1)

        service.unstar.assert_called_once_with("id-1")
        assert resp.context_data["message"] == "Unstarred: Subject 1"

    def test_actions_in_parameter_enum(self, cmd):
        action_param = next(p for p in cmd.parameters if p.name == "action")
        for action in ("mark_read", "mark_unread", "unstar", "forward", "triage"):
            assert action in action_param.enum_values


class TestForward:
    def test_forward_builds_quoted_draft_with_confirm(self, cmd):
        service = _wire_service(cmd, [_make_email(1)])
        service.fetch_message.return_value = _make_email(1)

        resp = cmd.run(
            _make_req(), action="forward", email_index=1,
            to="john@example.com", body="FYI",
        )

        service.fetch_message.assert_called_once_with("id-1", max_body_chars=3000)
        draft = resp.context_data["draft"]
        assert draft["type"] == "send"
        assert draft["to"] == "john@example.com"
        assert draft["subject"] == "Fwd: Subject 1"
        assert draft["body"].startswith("FYI\n\n")
        assert "---------- Forwarded message ----------" in draft["body"]
        assert "From: Sender 1 <sender1@example.com>" in draft["body"]
        assert "Body 1" in draft["body"]
        # Same draft-confirm flow as send: Send/Cancel buttons, waits for tap
        assert resp.wait_for_input
        labels = {a.button_text for a in resp.actions}
        assert labels == {"Send", "Cancel"}

    def test_forward_without_note(self, cmd):
        service = _wire_service(cmd, [_make_email(1)])
        service.fetch_message.return_value = _make_email(1)

        resp = cmd.run(_make_req(), action="forward", email_index=1, to="a@b.com")
        assert resp.context_data["draft"]["body"].startswith(
            "---------- Forwarded message ----------"
        )

    def test_forward_keeps_existing_fwd_prefix(self, cmd):
        service = _wire_service(cmd, [_make_email(1)])
        original = _make_email(1)
        original.subject = "Fwd: Subject 1"
        service.fetch_message.return_value = original

        resp = cmd.run(_make_req(), action="forward", email_index=1, to="a@b.com")
        assert resp.context_data["draft"]["subject"] == "Fwd: Subject 1"

    def test_forward_requires_to(self, cmd):
        _wire_service(cmd, [_make_email(1)])
        resp = cmd.run(_make_req(), action="forward", email_index=1)
        assert not resp.success

    def test_forward_requires_index(self, cmd):
        _wire_service(cmd, [_make_email(1)])
        resp = cmd.run(_make_req(), action="forward", to="a@b.com")
        assert not resp.success


# ── Fast-paths ───────────────────────────────────────────────────────────────


class TestNewFastPaths:
    @pytest.mark.parametrize("phrase,index", [
        ("mark email 2 as read", 2),
        ("mark the second email as read", 2),
        ("mark email number 3 as read", 3),
    ])
    def test_mark_read(self, cmd, phrase, index):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "mark_read", "email_index": index}

    @pytest.mark.parametrize("phrase,index", [
        ("mark email 2 as unread", 2),
        ("mark the first email as unread", 1),
    ])
    def test_mark_unread(self, cmd, phrase, index):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "mark_unread", "email_index": index}

    @pytest.mark.parametrize("phrase,index", [
        ("unstar email 2", 2),
        ("unstar the first email", 1),
    ])
    def test_unstar(self, cmd, phrase, index):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "unstar", "email_index": index}

    @pytest.mark.parametrize("phrase", [
        "triage my inbox",
        "triage my emails",
        "send my inbox to my phone",
    ])
    def test_triage(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "triage"}

    def test_star_does_not_swallow_unstar(self, cmd):
        result = cmd.pre_route("unstar email 2")
        assert result.arguments["action"] == "unstar"

    @pytest.mark.parametrize("phrase", [
        "forward email 2 to john@example.com",  # needs LLM extraction of the address
        "mark all my emails as read",
    ])
    def test_no_match_falls_through(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None


# ── Triage run ───────────────────────────────────────────────────────────────


class TestRunTriage:
    def test_posts_interactive_list(self, cmd, inbox_backend):
        service = _wire_service(cmd, [])
        emails = [_make_email(1), _make_email(2), _make_email(3)]
        service.search.return_value = emails

        resp = cmd.run(_make_req(user_id=42), action="triage")

        service.search.assert_called_once_with("is:unread in:inbox", max_results=25)
        assert resp.success
        assert resp.context_data["message"] == (
            "I've sent a triage list of 3 emails to your phone."
        )

        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["command_name"] == "email"
        assert call["title"] == "Inbox triage — 3 unread"
        assert call["category"] == "interactive_list"
        assert call["create_push_notification"] is True
        assert call["target_type"] == "user"
        assert call["user_id"] == 42
        assert call["body"] == (
            "- Sender 1: Subject 1\n- Sender 2: Subject 2\n- Sender 3: Subject 3"
        )

        metadata = call["metadata"]
        assert metadata["type"] == "interactive_list"
        assert metadata["command_name"] == "email"
        assert metadata["context"]["subjects"] == {
            "id-1": "Subject 1", "id-2": "Subject 2", "id-3": "Subject 3",
        }
        rows = metadata["sections"][0]["rows"]
        assert [r["key"] for r in rows] == ["id-1", "id-2", "id-3"]
        assert all(r["control"] == "checkbox" for r in rows)
        assert all(r["default"] == {"selected": False} for r in rows)
        assert [a["callback"] for a in metadata["actions"]] == [
            "triage_mark_read", "triage_archive", "triage_star",
        ]

    def test_single_email_singular_message(self, cmd, inbox_backend):
        service = _wire_service(cmd, [])
        service.search.return_value = [_make_email(1)]

        resp = cmd.run(_make_req(), action="triage")
        assert resp.context_data["message"] == (
            "I've sent a triage list of 1 email to your phone."
        )

    def test_no_user_targets_household(self, cmd, inbox_backend):
        service = _wire_service(cmd, [])
        service.search.return_value = [_make_email(1)]

        cmd.run(_make_req(user_id=None), action="triage")

        call = inbox_backend.calls[0]
        assert call["target_type"] == "household"
        assert call["user_id"] is None

    def test_empty_inbox_speaks_without_posting(self, cmd, inbox_backend):
        service = _wire_service(cmd, [])
        service.search.return_value = []

        resp = cmd.run(_make_req(is_pre_routed=True), action="triage")

        assert resp.success
        assert resp.context_data["message"] == "No unread emails to triage."
        assert inbox_backend.calls == []

    @pytest.mark.parametrize("tag", ["no_backend", "no_cc_url", "http_error", "invalid"])
    def test_post_failure_maps_to_spoken_error(self, cmd, inbox_backend, tag):
        inbox_backend.tag = tag
        service = _wire_service(cmd, [])
        service.search.return_value = [_make_email(1)]

        resp = cmd.run(_make_req(), action="triage")

        assert not resp.success
        assert resp.context_data["message"]  # spoken even on the pre-route path
        assert resp.error_details

    def test_no_backend_registered(self, cmd):
        # No inbox_backend fixture — facade returns "no_backend"
        service = _wire_service(cmd, [])
        service.search.return_value = [_make_email(1)]

        resp = cmd.run(_make_req(), action="triage")
        assert not resp.success
        assert resp.context_data["message"]


# ── Triage callbacks ─────────────────────────────────────────────────────────


def _callback_data(keys: list[str], subjects: dict[str, str] | None, action: str) -> dict:
    data = {
        "action": action,
        "selected": [{"key": k} for k in keys],
    }
    if subjects is not None:
        data["context"] = {"subjects": subjects}
    return data


class TestTriageCallbacks:
    def test_callbacks_registered(self, cmd):
        names = set(cmd.get_callbacks())
        assert {"triage_mark_read", "triage_archive", "triage_star"} <= names

    def test_mark_read_batch(self, cmd):
        service = _wire_service(cmd, [])
        service.mark_read.return_value = True
        data = _callback_data(
            ["id-1", "id-2"],
            {"id-1": "Subject 1", "id-2": "Subject 2"},
            "triage_mark_read",
        )

        resp = cmd.triage_mark_read(data, _make_req())

        assert service.mark_read.call_count == 2
        assert resp.context_data["message"] == "Marked 2 read."
        assert resp.context_data["detail_lines"] == ["Subject 1", "Subject 2"]

    def test_archive_partial_failure(self, cmd):
        service = _wire_service(cmd, [])
        service.archive.side_effect = [True, False]
        data = _callback_data(
            ["id-1", "id-2"],
            {"id-1": "Subject 1", "id-2": "Subject 2"},
            "triage_archive",
        )

        resp = cmd.triage_archive(data, _make_req())

        assert resp.context_data["message"] == "Archived 1. 1 failed."
        assert resp.context_data["detail_lines"] == ["Subject 1"]
        assert resp.context_data["failed"] == 1

    def test_star_batch(self, cmd):
        service = _wire_service(cmd, [])
        service.star.return_value = True
        data = _callback_data(["id-1"], {"id-1": "Subject 1"}, "triage_star")

        resp = cmd.triage_star(data, _make_req())

        service.star.assert_called_once_with("id-1")
        assert resp.context_data["message"] == "Starred 1."

    def test_missing_subject_falls_back_to_id(self, cmd):
        service = _wire_service(cmd, [])
        service.mark_read.return_value = True
        data = _callback_data(["id-9"], {}, "triage_mark_read")

        resp = cmd.triage_mark_read(data, _make_req())
        assert resp.context_data["detail_lines"] == ["id-9"]

    def test_missing_context_falls_back_to_id(self, cmd):
        service = _wire_service(cmd, [])
        service.mark_read.return_value = True
        data = _callback_data(["id-9"], None, "triage_mark_read")

        resp = cmd.triage_mark_read(data, _make_req())
        assert resp.context_data["detail_lines"] == ["id-9"]

    def test_empty_selection(self, cmd):
        _wire_service(cmd, [])
        resp = cmd.triage_mark_read(
            {"action": "triage_mark_read", "selected": [], "context": {}}, _make_req()
        )
        assert resp.context_data["message"] == "Nothing selected."

    def test_per_id_exception_does_not_abort_batch(self, cmd):
        service = _wire_service(cmd, [])
        service.archive.side_effect = [RuntimeError("boom"), True]
        data = _callback_data(
            ["id-1", "id-2"], {"id-1": "S1", "id-2": "S2"}, "triage_archive"
        )

        resp = cmd.triage_archive(data, _make_req())

        assert resp.context_data["message"] == "Archived 1. 1 failed."
        assert resp.context_data["detail_lines"] == ["S2"]

    def test_callbacks_never_resolve_through_session_cache(self, cmd):
        # Row keys carry message ids — _last_email_list must stay untouched
        service = _wire_service(cmd, [_make_email(7)])
        service.mark_read.return_value = True
        data = _callback_data(["id-1"], {"id-1": "S1"}, "triage_mark_read")

        cmd.triage_mark_read(data, _make_req())
        service.mark_read.assert_called_once_with("id-1")
