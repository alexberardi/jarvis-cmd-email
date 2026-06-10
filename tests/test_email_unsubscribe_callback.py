"""unsubscribe_selected callback — one-click POST / mailto-send / manual-visit ladder."""

import importlib.util
import os
import sys
import types
from types import SimpleNamespace
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


def _load_command_module():
    cmd_path = os.path.join(_ROOT, "commands", "email", "command.py")
    spec = importlib.util.spec_from_file_location(
        "email_unsub_callback_cmd_under_test", cmd_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_cmd_module = _load_command_module()


def _make_req() -> RequestInformation:
    return RequestInformation(
        voice_command="[mobile callback]",
        conversation_id="conv-1",
        user_id=42,
    )


def _record(
    url: str = "",
    mailto: str = "",
    one_click: bool = False,
    name: str = "Promo",
    count: int = 4,
) -> dict:
    return {"url": url, "mailto": mailto, "one_click": one_click, "count": count, "name": name}


def _data(keys: list[str]) -> dict:
    return {
        "action": "unsubscribe_selected",
        "selected": [{"key": k} for k in keys],
        "context": {},
    }


@pytest.fixture
def cmd():
    instance = _cmd_module.EmailCommand()
    instance._storage = MagicMock()
    instance._storage.get_secret.return_value = "tok"
    instance._unsubscribe_storage = MagicMock()
    instance._unsubscribe_storage.get.return_value = None
    return instance


@pytest.fixture
def service(cmd):
    svc = MagicMock()
    cmd._get_service = MagicMock(return_value=svc)
    return svc


@pytest.fixture
def http(monkeypatch):
    """Replace the command module's `requests` with a recording fake."""
    fake = SimpleNamespace(post=MagicMock(return_value=SimpleNamespace(status_code=200)))
    monkeypatch.setattr(_cmd_module, "requests", fake)
    return fake


def _stored(cmd, records: dict[str, dict | None]) -> None:
    cmd._unsubscribe_storage.get.side_effect = lambda key: records.get(key)


class TestCallbackRegistration:
    def test_callback_registered(self, cmd):
        assert "unsubscribe_selected" in set(cmd.get_callbacks())


class TestOneClick:
    def test_one_click_2xx_succeeds(self, cmd, service, http):
        _stored(cmd, {"a@x.com": _record(url="https://x/u", one_click=True)})

        resp = cmd.unsubscribe_selected(_data(["a@x.com"]), _make_req())

        http.post.assert_called_once_with(
            "https://x/u", data={"List-Unsubscribe": "One-Click"}, timeout=10
        )
        service.send.assert_not_called()
        assert resp.success
        assert resp.context_data["message"] == "Unsubscribed from 1 of 1."
        assert resp.context_data["detail_lines"] == ["Promo — done"]

    def test_one_click_non_2xx_without_mailto_fails(self, cmd, service, http):
        http.post.return_value = SimpleNamespace(status_code=500)
        _stored(cmd, {"a@x.com": _record(url="https://x/u", one_click=True)})

        resp = cmd.unsubscribe_selected(_data(["a@x.com"]), _make_req())

        assert resp.context_data["message"] == "Unsubscribed from 0 of 1."
        assert resp.context_data["detail_lines"] == ["Promo — failed"]

    def test_one_click_exception_without_mailto_fails(self, cmd, service, http):
        http.post.side_effect = RuntimeError("connection refused")
        _stored(cmd, {"a@x.com": _record(url="https://x/u", one_click=True)})

        resp = cmd.unsubscribe_selected(_data(["a@x.com"]), _make_req())

        assert resp.context_data["detail_lines"] == ["Promo — failed"]

    def test_one_click_failure_falls_back_to_mailto(self, cmd, service, http):
        http.post.return_value = SimpleNamespace(status_code=403)
        _stored(cmd, {
            "a@x.com": _record(url="https://x/u", mailto="unsub@x.com", one_click=True),
        })

        resp = cmd.unsubscribe_selected(_data(["a@x.com"]), _make_req())

        service.send.assert_called_once_with(
            "unsub@x.com", "unsubscribe", "Please unsubscribe this address."
        )
        assert resp.context_data["message"] == "Unsubscribed from 1 of 1."
        assert resp.context_data["detail_lines"] == ["Promo — done"]


class TestMailto:
    def test_mailto_sends_through_own_path(self, cmd, service, http):
        _stored(cmd, {"a@x.com": _record(mailto="unsub@x.com")})

        resp = cmd.unsubscribe_selected(_data(["a@x.com"]), _make_req())

        http.post.assert_not_called()
        service.send.assert_called_once_with(
            "unsub@x.com", "unsubscribe", "Please unsubscribe this address."
        )
        assert resp.context_data["message"] == "Unsubscribed from 1 of 1."
        assert resp.context_data["detail_lines"] == ["Promo — done"]

    def test_mailto_send_failure(self, cmd, service, http):
        service.send.side_effect = RuntimeError("smtp down")
        _stored(cmd, {"a@x.com": _record(mailto="unsub@x.com")})

        resp = cmd.unsubscribe_selected(_data(["a@x.com"]), _make_req())

        assert resp.context_data["message"] == "Unsubscribed from 0 of 1."
        assert resp.context_data["detail_lines"] == ["Promo — failed"]


class TestUrlOnly:
    def test_url_only_needs_manual_visit_and_is_never_fetched(self, cmd, service, http):
        _stored(cmd, {"a@x.com": _record(url="https://x/u")})

        resp = cmd.unsubscribe_selected(_data(["a@x.com"]), _make_req())

        http.post.assert_not_called()  # report only — never GET/POST a plain URL
        service.send.assert_not_called()
        assert resp.context_data["message"] == "Unsubscribed from 0 of 1."
        assert resp.context_data["detail_lines"] == ["Promo — needs manual visit"]


class TestExpiredAndBatch:
    def test_missing_record_counts_as_expired(self, cmd, service, http):
        _stored(cmd, {})  # nothing persisted (or TTL elapsed — backend returns None)

        resp = cmd.unsubscribe_selected(_data(["a@x.com"]), _make_req())

        assert resp.context_data["message"] == "Unsubscribed from 0 of 1."
        assert resp.context_data["detail_lines"] == [
            "a@x.com — expired — run the scan again",
        ]

    def test_batch_partial_failure_does_not_abort(self, cmd, service, http):
        _stored(cmd, {
            "a@x.com": _record(url="https://a/u", one_click=True, name="Sender A"),
            "b@x.com": None,  # expired
            "c@x.com": _record(url="https://c/u", name="Sender C"),  # manual
        })

        resp = cmd.unsubscribe_selected(
            _data(["a@x.com", "b@x.com", "c@x.com"]), _make_req()
        )

        assert resp.context_data["message"] == "Unsubscribed from 1 of 3."
        assert resp.context_data["detail_lines"] == [
            "Sender A — done",
            "b@x.com — expired — run the scan again",
            "Sender C — needs manual visit",
        ]

    def test_empty_selection(self, cmd, service, http):
        resp = cmd.unsubscribe_selected(_data([]), _make_req())
        assert resp.success
        assert resp.context_data["message"] == "Nothing selected."
        cmd._unsubscribe_storage.get.assert_not_called()

    def test_service_not_configured(self, cmd):
        cmd._get_service = MagicMock(side_effect=ValueError("no creds"))

        resp = cmd.unsubscribe_selected(_data(["a@x.com"]), _make_req())

        assert not resp.success
        assert resp.context_data["message"] == "Email isn't configured on this device."

    def test_never_resolves_through_session_cache(self, cmd, service, http):
        # Row keys carry sender addresses — _last_email_list must stay untouched
        cmd._last_email_list = [MagicMock(id="other-id")]
        _stored(cmd, {"a@x.com": _record(mailto="unsub@x.com")})

        cmd.unsubscribe_selected(_data(["a@x.com"]), _make_req())

        cmd._unsubscribe_storage.get.assert_called_once_with("a@x.com")
