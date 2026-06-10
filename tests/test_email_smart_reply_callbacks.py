"""Smart-reply command callbacks — send_draft_reply (reply + best-effort mark_read) and dismiss_draft."""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

from jarvis_command_sdk import RequestInformation

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


def _load_command():
    cmd_path = os.path.join(_ROOT, "commands", "email", "command.py")
    spec = importlib.util.spec_from_file_location(
        "email_smart_reply_cmd_under_test", cmd_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EmailCommand


def _make_req() -> RequestInformation:
    return RequestInformation(
        voice_command="[mobile callback]",
        conversation_id="conv-1",
        user_id=42,
    )


def _send_data(
    message_id: str = "id-1",
    thread_id: str = "thread-1",
    body: str = "Sounds good — see you then.",
) -> dict:
    return {"message_id": message_id, "thread_id": thread_id, "body": body}


@pytest.fixture
def cmd():
    instance = _load_command()()
    instance._storage = MagicMock()
    instance._storage.get_secret.return_value = "tok"
    return instance


@pytest.fixture
def service(cmd):
    svc = MagicMock()
    cmd._get_service = MagicMock(return_value=svc)
    return svc


class TestCallbackRegistration:
    def test_callbacks_registered(self, cmd):
        names = set(cmd.get_callbacks())
        assert {"send_draft_reply", "dismiss_draft"} <= names


class TestSendDraftReply:
    def test_sends_reply_and_marks_read(self, cmd, service):
        service.reply.return_value = {"id": "sent-1"}
        service.mark_read.return_value = True

        resp = cmd.send_draft_reply(_send_data(), _make_req())

        service.reply.assert_called_once_with(
            "id-1", "thread-1", "Sounds good — see you then."
        )
        service.mark_read.assert_called_once_with("id-1")
        assert resp.success
        assert resp.context_data["message"] == "Reply sent."
        assert "detail_lines" not in resp.context_data

    def test_mark_read_failure_is_best_effort(self, cmd, service):
        service.reply.return_value = {"id": "sent-1"}
        service.mark_read.side_effect = RuntimeError("boom")

        resp = cmd.send_draft_reply(_send_data(), _make_req())

        assert resp.success
        assert resp.context_data["message"] == "Reply sent."

    def test_reply_failure_is_spoken_error(self, cmd, service):
        service.reply.side_effect = RuntimeError("smtp down")

        resp = cmd.send_draft_reply(_send_data(), _make_req())

        assert not resp.success
        assert resp.context_data["message"] == "I couldn't send the reply."
        assert resp.error_details
        service.mark_read.assert_not_called()

    def test_missing_message_id_rejected(self, cmd, service):
        resp = cmd.send_draft_reply(_send_data(message_id=""), _make_req())
        assert not resp.success
        assert resp.context_data["message"]
        service.reply.assert_not_called()

    def test_missing_body_rejected(self, cmd, service):
        resp = cmd.send_draft_reply(_send_data(body=""), _make_req())
        assert not resp.success
        service.reply.assert_not_called()

    def test_service_not_configured(self, cmd):
        cmd._get_service = MagicMock(side_effect=ValueError("no creds"))

        resp = cmd.send_draft_reply(_send_data(), _make_req())

        assert not resp.success
        assert resp.context_data["message"] == "Email isn't configured on this device."

    def test_never_resolves_through_session_cache(self, cmd, service):
        # Draft data carries message ids — _last_email_list must stay untouched
        cmd._last_email_list = [MagicMock(id="other-id")]
        service.reply.return_value = {}

        cmd.send_draft_reply(_send_data(), _make_req())
        service.reply.assert_called_once_with(
            "id-1", "thread-1", "Sounds good — see you then."
        )


class TestDismissDraft:
    def test_dismiss_no_side_effects(self, cmd, service):
        resp = cmd.dismiss_draft({"message_id": "id-1"}, _make_req())

        assert resp.success
        assert resp.context_data["message"] == "Dismissed."
        assert service.method_calls == []
