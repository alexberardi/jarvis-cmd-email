"""Gmail OAuth tokens are user-scoped — store_auth_values must write the
token owner's rows (SDK user ContextVar, set by the node's auth pull /
token refresh agent) and refuse loudly when no owner is in context, rather
than store tokens the user-scoped reads can never find."""

import importlib.util
import os
import sys
import types

import pytest

from jarvis_command_sdk import set_backend, set_current_user_id
from jarvis_command_sdk.storage import StorageBackend


def _stub_email_shared() -> None:
    if "email_shared" in sys.modules:
        return
    pkg = types.ModuleType("email_shared")
    sys.modules["email_shared"] = pkg

    em = types.ModuleType("email_shared.email_message")
    em.EmailMessage = type("EmailMessage", (), {})
    em.EmailConnectionError = type("EmailConnectionError", (Exception,), {})
    em.extract_email = lambda x: x
    sys.modules["email_shared.email_message"] = em

    esf = types.ModuleType("email_shared.email_service_factory")
    esf.create_email_service = lambda: None
    esf.get_email_provider = lambda: "gmail"
    sys.modules["email_shared.email_service_factory"] = esf

    tr = types.ModuleType("email_shared.triage")
    tr.build_triage_payload = lambda emails: ({}, {})
    tr.build_triage_body = lambda emails: ""
    sys.modules["email_shared.triage"] = tr


def _load_command():
    _stub_email_shared()
    here = os.path.dirname(os.path.abspath(__file__))
    cmd_path = os.path.join(here, "..", "commands", "email", "command.py")
    spec = importlib.util.spec_from_file_location("email_cmd_token_scope_test", cmd_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EmailCommand


class _FakeBackend(StorageBackend):
    """Minimal backend keyed on (key, scope, user_id)."""

    def __init__(self) -> None:
        self.secrets: dict = {}

    def save(self, command_name, data_key, data, expires_at=None) -> None: ...
    def get(self, command_name, data_key): return None
    def get_all(self, command_name): return {}
    def delete(self, command_name, data_key) -> bool: return False
    def delete_all(self, command_name) -> int: return 0

    def get_secret(self, key, scope, user_id=None):
        return self.secrets.get((key, scope, user_id))

    def set_secret(self, key, value, scope, value_type="string", user_id=None) -> None:
        self.secrets[(key, scope, user_id)] = value

    def delete_secret(self, key, scope, user_id=None) -> None:
        self.secrets.pop((key, scope, user_id), None)


@pytest.fixture
def backend():
    b = _FakeBackend()
    set_backend(b)
    yield b
    set_current_user_id(None)


def test_store_auth_values_writes_user_scoped_tokens(backend):
    cmd = _load_command()()

    set_current_user_id(7)
    try:
        cmd.store_auth_values({"access_token": "at", "refresh_token": "rt"})
    finally:
        set_current_user_id(None)

    assert backend.secrets[("GMAIL_ACCESS_TOKEN", "user", 7)] == "at"
    assert backend.secrets[("GMAIL_REFRESH_TOKEN", "user", 7)] == "rt"


def test_store_auth_values_refuses_without_user(backend):
    cmd = _load_command()()

    cmd.store_auth_values({"access_token": "at", "refresh_token": "rt"})

    # No owner — nothing stored anywhere (loud log, no orphan rows).
    assert backend.secrets == {}
