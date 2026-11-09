"""unsubscribe_selected callback — one-click POST / mailto-send / manual-visit ladder."""

import importlib.util
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jarvis_command_sdk import ProposableAction, RequestInformation

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
    _load_real("email_shared.attachments", "email_shared", "attachments.py")


_install_email_shared()


def _load_command_module():
    cmd_path = os.path.join(_ROOT, "commands", "email", "command.py")
    spec = importlib.util.spec_from_file_location("email_draft_from_call_cmd_under_test", cmd_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_cmd_module = _load_command_module()


def _req() -> RequestInformation:
    return RequestInformation(voice_command="cb:draft_from_call", conversation_id="callback:1", user_id=42)


@pytest.fixture
def cmd():
    instance = _cmd_module.EmailCommand()
    instance._storage = MagicMock()
    instance._storage.get_secret.return_value = "tok"
    return instance


@pytest.fixture
def service(cmd, monkeypatch):
    svc = MagicMock()
    svc.username = "alex@example.com"
    svc.create_draft.return_value = {"id": "d1", "folder": "Drafts"}
    monkeypatch.setattr(cmd, "_get_service", lambda: svc)
    return svc


def test_draft_from_call_is_offered_after_calls(cmd):
    actions = cmd.get_proposable_actions()  # validates against the @callback
    if "offer_after_calls" not in ProposableAction.__dataclass_fields__:
        # An SDK before jarvis-command-sdk#20: nothing offered, and the command still loads.
        assert actions == {}
        return
    action = actions["draft_from_call"]
    assert action.offer_after_calls and "email address said on the call" in action.offer_after_calls
    assert action.idempotency_param == "idempotency_key"
    assert {p.name for p in action.params} == {"to", "subject", "body", "idempotency_key"}


def test_draft_from_call_saves_a_draft_never_sends(cmd, service):
    resp = cmd.draft_from_call(
        {"to": "service@quickauto.example", "subject": "Photos of the brakes", "body": "Hi, here are the photos.",
         "idempotency_key": "call:s1:1"}, _req())
    assert resp.success
    msg = service.create_draft.call_args.args[0]
    assert msg["To"] == "service@quickauto.example" and msg["From"] == "alex@example.com"
    assert msg["Subject"] == "Photos of the brakes" and "here are the photos" in msg.get_content()
    assert not service.send.called
    assert "hasn't been sent" in resp.context_data["message"]


@pytest.mark.parametrize("data", [
    {"to": "not an address", "subject": "s", "body": "b"},
    {"to": "a@b.example", "subject": "s", "body": "  "},
    {},
])
def test_draft_from_call_refuses_bad_input(cmd, service, data):
    resp = cmd.draft_from_call(data, _req())
    assert not resp.success
    assert not service.create_draft.called


def test_draft_from_call_reports_a_mailbox_error(cmd, service):
    service.create_draft.side_effect = RuntimeError("IMAP down")
    resp = cmd.draft_from_call({"to": "a@b.example", "subject": "s", "body": "b"}, _req())
    assert not resp.success and "couldn't save" in resp.context_data["message"]
