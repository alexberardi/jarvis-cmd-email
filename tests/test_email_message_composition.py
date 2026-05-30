"""Verify EmailCommand.run() composes a spoken `message` for list/read on pre-route path."""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest


def _stub_email_shared() -> None:
    if "email_shared" in sys.modules:
        return
    pkg = types.ModuleType("email_shared")
    sys.modules["email_shared"] = pkg

    em = types.ModuleType("email_shared.email_message")
    em.EmailMessage = type("EmailMessage", (), {})
    em.extract_email = lambda x: x
    sys.modules["email_shared.email_message"] = em

    esf = types.ModuleType("email_shared.email_service_factory")
    esf.create_email_service = lambda: None
    esf.get_email_provider = lambda: "gmail"
    sys.modules["email_shared.email_service_factory"] = esf


def _load_command():
    _stub_email_shared()
    here = os.path.dirname(os.path.abspath(__file__))
    cmd_path = os.path.join(here, "..", "commands", "email", "command.py")
    spec = importlib.util.spec_from_file_location("email_msg_test", cmd_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cmd_module():
    return _load_command()


def _make_email(idx: int):
    from datetime import datetime
    m = MagicMock()
    m.id = f"id-{idx}"
    m.sender_name = f"Sender {idx}"
    m.sender = f"sender{idx}@example.com"
    m.subject = f"Subject {idx}"
    m.snippet = f"Snippet {idx}"
    m.date = datetime(2026, 5, 30, 12, 0, 0)
    m.body = f"Body {idx} " * 100
    return m


def _make_req(is_pre_routed: bool):
    from core.request_information import RequestInformation
    return RequestInformation(
        voice_command="check my email",
        conversation_id="c",
        is_validation_response=False,
        is_pre_routed=is_pre_routed,
    )


def _make_cmd_with_auth(cmd_module):
    cmd = cmd_module.EmailCommand()
    cmd._storage = MagicMock()
    cmd._storage.get_secret.return_value = "fake-token"
    return cmd


def test_list_empty_message(cmd_module, monkeypatch):
    cmd = _make_cmd_with_auth(cmd_module)
    svc = MagicMock(); svc.search.return_value = []
    monkeypatch.setattr(cmd, "_get_service", lambda: svc)

    resp = cmd.run(_make_req(is_pre_routed=True), action="list")
    msg = resp.context_data.get("message")
    assert msg
    assert "no unread" in msg.lower() or "0" in msg


def test_list_with_emails_message(cmd_module, monkeypatch):
    cmd = _make_cmd_with_auth(cmd_module)
    emails = [_make_email(1), _make_email(2)]
    svc = MagicMock(); svc.search.return_value = emails
    monkeypatch.setattr(cmd, "_get_service", lambda: svc)

    resp = cmd.run(_make_req(is_pre_routed=True), action="list")
    msg = resp.context_data.get("message")
    assert msg
    assert "Subject 1" in msg or "Sender 1" in msg


def test_list_no_message_when_not_pre_routed(cmd_module, monkeypatch):
    cmd = _make_cmd_with_auth(cmd_module)
    emails = [_make_email(1)]
    svc = MagicMock(); svc.search.return_value = emails
    monkeypatch.setattr(cmd, "_get_service", lambda: svc)

    resp = cmd.run(_make_req(is_pre_routed=False), action="list")
    assert resp.context_data.get("message") is None


def test_read_message_includes_subject(cmd_module, monkeypatch):
    cmd = _make_cmd_with_auth(cmd_module)
    email = _make_email(3)
    cmd._last_email_list = [email]
    svc = MagicMock()
    svc.fetch_message.return_value = email
    monkeypatch.setattr(cmd, "_get_service", lambda: svc)

    resp = cmd.run(_make_req(is_pre_routed=True), action="read", email_index=1)
    msg = resp.context_data.get("message")
    assert msg
    assert "Sender 3" in msg
    assert "Subject 3" in msg
