"""Voice follow-up ("mark those as read") for the email command.

_run_list surfaces ReferenceableItems (ref_id = message id, action = mark_read);
the node's act_on_items dispatch then calls the mark_read @callback with the same
{action, selected, context} shape a mobile triage tap sends.
"""

import importlib.util
import os
import sys
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jarvis_command_sdk import RequestInformation


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
    spec = importlib.util.spec_from_file_location("email_cmd_voice_followup", cmd_path)
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
        date=datetime(2026, 6, 16, 9, idx),
    )


def _req(is_pre_routed: bool = False) -> RequestInformation:
    return RequestInformation(
        voice_command="what emails do I have",
        conversation_id="conv-1",
        user_id=42,
        is_pre_routed=is_pre_routed,
    )


@pytest.fixture
def cmd():
    instance = _load_command()()
    instance._storage = MagicMock()
    instance._storage.get_secret.return_value = "tok"
    return instance


# ── _run_list surfaces referenceable items ───────────────────────────────────


def test_run_list_surfaces_referenceable_items(cmd):
    emails = [_make_email(1), _make_email(2), _make_email(3)]
    service = MagicMock()
    service.search.return_value = emails
    cmd._get_service = MagicMock(return_value=service)

    resp = cmd._run_list(_req())

    items = resp.referenceable_items
    assert items is not None and len(items) == 3
    assert [i.ref_id for i in items] == ["id-1", "id-2", "id-3"]
    assert items[0].label == "Sender 1: Subject 1"
    assert items[0].attrs == {"sender": "Sender 1", "subject": "Subject 1"}
    assert items[0].actions == ["mark_read"]


def test_run_list_no_emails_no_items(cmd):
    service = MagicMock()
    service.search.return_value = []
    cmd._get_service = MagicMock(return_value=service)

    resp = cmd._run_list(_req())
    assert resp.referenceable_items is None


def test_build_referenceable_items_degrades_without_sdk_support(cmd, monkeypatch):
    """On a pre-0.4.0 node ReferenceableItem is None — listing still works."""
    globs = type(cmd)._build_referenceable_items.__globals__
    monkeypatch.setitem(globs, "ReferenceableItem", None)
    assert cmd._build_referenceable_items([_make_email(1)]) is None


# ── mark_read @callback reuses the triage path ───────────────────────────────


def test_mark_read_callback_marks_each_selected_id(cmd):
    service = MagicMock()
    service.mark_read.return_value = True
    cmd._get_service = MagicMock(return_value=service)

    data = {
        "action": "mark_read",
        "selected": [{"key": "id-1", "sender": "Sender 1"}, {"key": "id-2"}],
        "context": {"source_tool": "email"},
    }
    resp = cmd.mark_read(data, _req())

    assert resp.success
    assert resp.context_data["message"] == "Marked 2 read."
    marked = {c.args[0] for c in service.mark_read.call_args_list}
    assert marked == {"id-1", "id-2"}


def test_mark_read_callback_registered_as_callback(cmd):
    assert "mark_read" in cmd.get_callbacks()


def test_mark_read_callback_empty_selection(cmd):
    resp = cmd.mark_read({"action": "mark_read", "selected": []}, _req())
    assert resp.success
    assert "Nothing selected" in resp.context_data["message"]
