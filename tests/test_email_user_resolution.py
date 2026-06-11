"""find_configured_user_ids — node-side enumeration of mailbox-configured users.

Mailbox secrets are user-scoped and agents run with no ambient user in the
SDK ContextVar, so the helper enumerates user-scope secret rows via the
node's services.secret_service and checks each candidate's credentials with
explicit user_id reads.
"""

import importlib.util
import os
import sys
import types
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))


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


def _load_module():
    _stub_log_client()
    path = os.path.join(_ROOT, "email_shared", "user_resolution.py")
    spec = importlib.util.spec_from_file_location("user_resolution_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["user_resolution_under_test"] = module
    spec.loader.exec_module(module)
    return module


_mod = _load_module()


def _row(key: str, user_id: int | None, value: str) -> SimpleNamespace:
    return SimpleNamespace(key=key, user_id=user_id, value=value)


def _install_fake_secret_service(monkeypatch, rows: list[SimpleNamespace]) -> None:
    """Make `from services.secret_service import ...` resolve to row-backed fakes."""
    services_mod = types.ModuleType("services")
    ss_mod = types.ModuleType("services.secret_service")

    def get_all_secrets(scope: str, user_id: int | None = None):
        return [r for r in rows]

    def get_secret_value(key: str, scope: str, user_id: int | None = None):
        for r in rows:
            if r.key == key and r.user_id == user_id:
                return r.value
        return None

    ss_mod.get_all_secrets = get_all_secrets  # type: ignore[attr-defined]
    ss_mod.get_secret_value = get_secret_value  # type: ignore[attr-defined]
    services_mod.secret_service = ss_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services", services_mod)
    monkeypatch.setitem(sys.modules, "services.secret_service", ss_mod)


class TestNotOnANode:
    def test_no_services_module_returns_empty(self, monkeypatch):
        # e.g. Pantry container test — the node runtime isn't importable
        monkeypatch.delitem(sys.modules, "services", raising=False)
        monkeypatch.delitem(sys.modules, "services.secret_service", raising=False)
        assert _mod.find_configured_user_ids() == []


class TestGmailUsers:
    def test_user_with_access_token_is_usable(self, monkeypatch):
        _install_fake_secret_service(
            monkeypatch, [_row("GMAIL_ACCESS_TOKEN", 1, "tok")]
        )
        assert _mod.find_configured_user_ids() == [1]

    def test_default_provider_is_gmail_when_no_provider_row(self, monkeypatch):
        # EMAIL_PROVIDER absent → gmail branch → token required
        _install_fake_secret_service(
            monkeypatch,
            [_row("IMAP_USERNAME", 1, "alex@example.com")],  # candidate, but gmail branch
        )
        assert _mod.find_configured_user_ids() == []

    def test_gmail_user_without_token_is_unusable(self, monkeypatch):
        _install_fake_secret_service(
            monkeypatch, [_row("EMAIL_PROVIDER", 1, "gmail")]
        )
        assert _mod.find_configured_user_ids() == []

    def test_empty_provider_value_means_gmail(self, monkeypatch):
        _install_fake_secret_service(
            monkeypatch,
            [
                _row("EMAIL_PROVIDER", 1, "   "),
                _row("GMAIL_ACCESS_TOKEN", 1, "tok"),
            ],
        )
        assert _mod.find_configured_user_ids() == [1]


class TestImapPresetUsers:
    @pytest.mark.parametrize("provider", ["proton", "yahoo", "outlook", "fastmail", "imap"])
    def test_preset_user_with_imap_creds_is_usable_without_gmail_token(
        self, monkeypatch, provider
    ):
        # The blocker scenario: IMAP presets must NOT fall into the Gmail branch.
        _install_fake_secret_service(
            monkeypatch,
            [
                _row("EMAIL_PROVIDER", 2, provider),
                _row("IMAP_USERNAME", 2, "alex@example.com"),
                _row("IMAP_PASSWORD", 2, "hunter2"),
            ],
        )
        assert _mod.find_configured_user_ids() == [2]

    def test_provider_value_is_normalized(self, monkeypatch):
        _install_fake_secret_service(
            monkeypatch,
            [
                _row("EMAIL_PROVIDER", 2, "  Proton "),
                _row("IMAP_USERNAME", 2, "alex@example.com"),
                _row("IMAP_PASSWORD", 2, "hunter2"),
            ],
        )
        assert _mod.find_configured_user_ids() == [2]

    def test_missing_password_is_unusable(self, monkeypatch):
        _install_fake_secret_service(
            monkeypatch,
            [
                _row("EMAIL_PROVIDER", 2, "proton"),
                _row("IMAP_USERNAME", 2, "alex@example.com"),
            ],
        )
        assert _mod.find_configured_user_ids() == []

    def test_missing_username_is_unusable(self, monkeypatch):
        _install_fake_secret_service(
            monkeypatch,
            [
                _row("EMAIL_PROVIDER", 2, "proton"),
                _row("IMAP_PASSWORD", 2, "hunter2"),
            ],
        )
        assert _mod.find_configured_user_ids() == []

    def test_imap_user_does_not_need_gmail_token(self, monkeypatch):
        # A gmail token for ANOTHER user must not bleed into the imap user
        _install_fake_secret_service(
            monkeypatch,
            [
                _row("EMAIL_PROVIDER", 2, "proton"),
                _row("IMAP_USERNAME", 2, "alex@example.com"),
                _row("IMAP_PASSWORD", 2, "hunter2"),
                _row("GMAIL_ACCESS_TOKEN", 9, "tok"),
            ],
        )
        assert _mod.find_configured_user_ids() == [2, 9]


class TestMixedAndEdgeCases:
    def test_multiple_users_sorted(self, monkeypatch):
        _install_fake_secret_service(
            monkeypatch,
            [
                _row("GMAIL_ACCESS_TOKEN", 5, "tok"),
                _row("EMAIL_PROVIDER", 2, "fastmail"),
                _row("IMAP_USERNAME", 2, "a@b.c"),
                _row("IMAP_PASSWORD", 2, "pw"),
            ],
        )
        assert _mod.find_configured_user_ids() == [2, 5]

    def test_rows_without_user_id_are_skipped(self, monkeypatch):
        _install_fake_secret_service(
            monkeypatch, [_row("EMAIL_PROVIDER", None, "gmail")]
        )
        assert _mod.find_configured_user_ids() == []

    def test_empty_credential_values_are_unusable(self, monkeypatch):
        _install_fake_secret_service(
            monkeypatch, [_row("GMAIL_ACCESS_TOKEN", 1, "")]
        )
        assert _mod.find_configured_user_ids() == []

    def test_non_mailbox_user_rows_are_ignored(self, monkeypatch):
        _install_fake_secret_service(
            monkeypatch, [_row("SOME_OTHER_SECRET", 1, "value")]
        )
        assert _mod.find_configured_user_ids() == []

    def test_exception_returns_empty_never_raises(self, monkeypatch):
        services_mod = types.ModuleType("services")
        ss_mod = types.ModuleType("services.secret_service")

        def boom(scope, user_id=None):
            raise RuntimeError("db locked")

        ss_mod.get_all_secrets = boom  # type: ignore[attr-defined]
        ss_mod.get_secret_value = lambda *a, **kw: None  # type: ignore[attr-defined]
        services_mod.secret_service = ss_mod  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "services", services_mod)
        monkeypatch.setitem(sys.modules, "services.secret_service", ss_mod)

        assert _mod.find_configured_user_ids() == []
