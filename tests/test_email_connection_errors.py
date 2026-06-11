"""Connection failures must be SAID, not swallowed (EmailConnectionError).

Field incident: the Proton Bridge died for a WEEK silently — search() caught
connection errors and returned [], so "check my email" said "You have no
unread emails". Now:

- ImapEmailService raises EmailConnectionError (with host:port, and "Proton
  Mail Bridge" naming for proton config) on connection-class failures during
  connect/STARTTLS/login and on SMTP connect failures during send.
- GoogleGmailService raises EmailConnectionError on httpx transport failures;
  the 401 → _flag_reauth path is UNCHANGED (still returns []).
- Non-connection failures keep today's behavior ([] / None / False).
- EmailCommand.run() catches it once at dispatch and answers with a spoken
  message (context_data["message"] set, so pre-routed paths speak it too);
  callbacks return clean errors instead of raising.
"""

import imaplib
import importlib.util
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
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


def _install_email_shared():
    _stub_log_client()
    if "email_shared" not in sys.modules:
        sys.modules["email_shared"] = types.ModuleType("email_shared")
    msg_mod = _load_real("email_shared.email_message", "email_shared", "email_message.py")
    if "email_shared.email_service_factory" not in sys.modules:
        esf = types.ModuleType("email_shared.email_service_factory")
        esf.create_email_service = lambda: None
        esf.get_email_provider = lambda: "gmail"
        sys.modules["email_shared.email_service_factory"] = esf
    _load_real("email_shared.triage", "email_shared", "triage.py")
    gmail = _load_real("email_shared.google_gmail_service", "email_shared", "google_gmail_service.py")
    imap = _load_real("email_shared.imap_email_service", "email_shared", "imap_email_service.py")
    return msg_mod, gmail, imap


_msg_mod, _gmail_mod, _imap_mod = _install_email_shared()

EmailConnectionError = _msg_mod.EmailConnectionError


def _load_command():
    """Load EmailCommand fresh, returning the EmailConnectionError it binds.

    Other test files reload email_shared.email_message during collection, so
    the class object in sys.modules at fixture time may differ from this
    module's collection-time capture — tests must raise the exact class the
    command module imported or the dispatch-level except won't match.
    """
    cmd_path = os.path.join(_ROOT, "commands", "email", "command.py")
    spec = importlib.util.spec_from_file_location("email_conn_cmd_under_test", cmd_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EmailCommand, module.EmailConnectionError


def _make_imap(provider: str = "") -> object:
    return _imap_mod.ImapEmailService(
        imap_host="localhost",
        imap_port=1143,
        smtp_host="smtp.local",
        smtp_port=1025,
        username="u@example.com",
        password="pw",
        provider=provider,
    )


def _raiser(exc):
    def _boom(*args, **kwargs):
        raise exc
    return _boom


# ── IMAP: connection-class failures raise; parse failures don't ──────────────


class TestImapConnectionErrors:
    def test_connect_refused_raises_with_host_port(self, monkeypatch):
        monkeypatch.setattr(
            _imap_mod.imaplib, "IMAP4", _raiser(ConnectionRefusedError(61, "refused"))
        )
        with pytest.raises(EmailConnectionError) as ei:
            _make_imap().search("is:unread in:inbox")
        assert "localhost:1143" in str(ei.value)
        assert "email server" in str(ei.value)

    def test_eof_abort_at_connect_raises(self, monkeypatch):
        monkeypatch.setattr(
            _imap_mod.imaplib, "IMAP4", _raiser(imaplib.IMAP4.abort("socket error: EOF"))
        )
        with pytest.raises(EmailConnectionError):
            _make_imap().search("is:unread")

    def test_eoferror_at_connect_raises(self, monkeypatch):
        monkeypatch.setattr(_imap_mod.imaplib, "IMAP4", _raiser(EOFError()))
        with pytest.raises(EmailConnectionError):
            _make_imap().search("is:unread")

    def test_starttls_failure_raises(self, monkeypatch):
        conn = MagicMock()
        conn.starttls.side_effect = OSError("tls handshake died")
        monkeypatch.setattr(_imap_mod.imaplib, "IMAP4", MagicMock(return_value=conn))
        with pytest.raises(EmailConnectionError):
            _make_imap().search("is:unread")

    def test_login_imap4_error_raises(self, monkeypatch):
        conn = MagicMock()
        conn.login.side_effect = imaplib.IMAP4.error("LOGIN failed")
        monkeypatch.setattr(_imap_mod.imaplib, "IMAP4", MagicMock(return_value=conn))
        with pytest.raises(EmailConnectionError):
            _make_imap().search("is:unread")

    def test_proton_config_names_the_bridge(self, monkeypatch):
        monkeypatch.setattr(
            _imap_mod.imaplib, "IMAP4", _raiser(ConnectionRefusedError(61, "refused"))
        )
        with pytest.raises(EmailConnectionError) as ei:
            _make_imap(provider="proton").search("is:unread")
        assert "Proton Mail Bridge" in str(ei.value)
        assert "localhost:1143" in str(ei.value)
        assert "email server" not in str(ei.value)

    def test_fetch_message_raises(self, monkeypatch):
        monkeypatch.setattr(
            _imap_mod.imaplib, "IMAP4", _raiser(ConnectionRefusedError(61, "refused"))
        )
        with pytest.raises(EmailConnectionError):
            _make_imap().fetch_message("42")

    @pytest.mark.parametrize("verb", ["archive", "trash", "star", "mark_read"])
    def test_verbs_raise(self, monkeypatch, verb):
        monkeypatch.setattr(
            _imap_mod.imaplib, "IMAP4", _raiser(ConnectionRefusedError(61, "refused"))
        )
        with pytest.raises(EmailConnectionError):
            getattr(_make_imap(), verb)("42")

    def test_smtp_connect_failure_on_send_raises_with_smtp_host_port(self, monkeypatch):
        monkeypatch.setattr(
            _imap_mod.smtplib, "SMTP", _raiser(ConnectionRefusedError(61, "refused"))
        )
        with pytest.raises(EmailConnectionError) as ei:
            _make_imap().send("a@b.com", "subject", "body")
        assert "smtp.local:1025" in str(ei.value)

    def test_non_connection_search_failure_keeps_returning_empty(self, monkeypatch):
        # IMAP4.error AFTER connect (mid-command) is not a connection-step
        # failure — today's swallow-to-[] behavior stays.
        conn = MagicMock()
        conn.uid.side_effect = imaplib.IMAP4.error("parse error")
        service = _make_imap()
        monkeypatch.setattr(service, "_connect_imap", MagicMock(return_value=conn))
        assert service.search("is:unread") == []

    def test_unparseable_message_still_skipped(self, monkeypatch):
        # The per-message parse guard inside search is untouched.
        conn = MagicMock()

        def uid_responder(command, *args):
            if command == "SEARCH":
                return ("OK", [b"7"])
            raise ValueError("broken message")

        conn.uid.side_effect = uid_responder
        service = _make_imap()
        monkeypatch.setattr(service, "_connect_imap", MagicMock(return_value=conn))
        assert service.search("is:unread") == []


# ── Gmail: httpx transport failures raise; 401 path unchanged ────────────────


class TestGmailConnectionErrors:
    @pytest.fixture
    def gmail(self):
        return _gmail_mod.GoogleGmailService("token", "refresh", "client-id")

    @pytest.mark.parametrize(
        "exc",
        [httpx.ConnectError("boom"), httpx.ConnectTimeout("slow"), httpx.ReadTimeout("slow")],
    )
    def test_search_transport_failure_raises(self, gmail, monkeypatch, exc):
        monkeypatch.setattr(_gmail_mod.httpx, "get", _raiser(exc))
        with pytest.raises(EmailConnectionError) as ei:
            gmail.search("is:unread")
        assert "Gmail" in str(ei.value)

    def test_fetch_message_transport_failure_raises(self, gmail, monkeypatch):
        monkeypatch.setattr(_gmail_mod.httpx, "get", _raiser(httpx.ConnectError("boom")))
        with pytest.raises(EmailConnectionError):
            gmail.fetch_message("m-1")

    def test_401_still_flags_reauth_and_returns_empty(self, gmail, monkeypatch):
        # The 401 → _flag_reauth + RuntimeError path stays EXACTLY as-is:
        # reauth is flagged and search returns [] (no EmailConnectionError).
        flag = MagicMock()
        monkeypatch.setattr(_gmail_mod.GoogleGmailService, "_flag_reauth", flag)
        monkeypatch.setattr(
            _gmail_mod.httpx,
            "get",
            lambda *a, **kw: SimpleNamespace(status_code=401),
        )
        assert gmail.search("is:unread") == []
        flag.assert_called_once()


# ── Command: spoken error at dispatch, clean callback errors ─────────────────


def _make_req(user_id: int | None = 42, is_pre_routed: bool = False) -> RequestInformation:
    return RequestInformation(
        voice_command="check my email",
        conversation_id="conv-1",
        user_id=user_id,
        is_pre_routed=is_pre_routed,
    )


_DESCRIPTION = "Couldn't connect to the Proton Mail Bridge at 127.0.0.1:1143"


@pytest.fixture
def cmd():
    command_cls, exc_cls = _load_command()
    instance = command_cls()
    # Pass the run() auth gate (gmail provider stub, token present)
    instance._storage = MagicMock()
    instance._storage.get_secret.return_value = "tok"
    instance._test_exc_cls = exc_cls  # the class the command actually catches
    return instance


def _wire_failing_service(cmd) -> MagicMock:
    exc = cmd._test_exc_cls(_DESCRIPTION)
    service = MagicMock()
    service.search.side_effect = exc
    service.fetch_message.side_effect = exc
    cmd._get_service = MagicMock(return_value=service)
    return service


class TestCommandConnectionFailure:
    @pytest.mark.parametrize("action", ["list", "triage", "unsubscribe_scan"])
    def test_actions_return_spoken_error(self, cmd, action):
        _wire_failing_service(cmd)
        resp = cmd.run(_make_req(), action=action)
        assert not resp.success
        assert _DESCRIPTION in resp.error_details
        assert _DESCRIPTION in resp.context_data["message"]

    def test_pre_routed_list_speaks_the_failure(self, cmd):
        # Pre-routed callers have no LLM downstream — message must be set so
        # the wrapper speaks it instead of "You have no unread emails".
        _wire_failing_service(cmd)
        resp = cmd.run(_make_req(is_pre_routed=True), action="list")
        assert not resp.success
        assert _DESCRIPTION in resp.context_data["message"]

    def test_read_returns_spoken_error(self, cmd):
        _wire_failing_service(cmd)
        cmd._last_email_list = [
            SimpleNamespace(id="id-1", subject="S1", thread_id="t-1")
        ]
        resp = cmd.run(_make_req(), action="read", email_index=1)
        assert not resp.success
        assert _DESCRIPTION in resp.context_data["message"]

    def test_search_action_returns_spoken_error(self, cmd):
        _wire_failing_service(cmd)
        resp = cmd.run(_make_req(), action="search", query="receipts")
        assert not resp.success
        assert _DESCRIPTION in resp.context_data["message"]


class TestCallbackConnectionFailure:
    def test_triage_callback_returns_clean_error(self, cmd):
        service = _wire_failing_service(cmd)
        service.mark_read.side_effect = cmd._test_exc_cls(_DESCRIPTION)
        data = {
            "action": "triage_mark_read",
            "selected": [{"key": "id-1"}, {"key": "id-2"}],
            "context": {"subjects": {"id-1": "S1", "id-2": "S2"}},
        }

        resp = cmd.triage_mark_read(data, _make_req())  # must not raise

        assert not resp.success
        assert _DESCRIPTION in resp.context_data["message"]
        # Short-circuited: the server is down, the second key is never tried.
        service.mark_read.assert_called_once()

    def test_send_draft_reply_returns_clean_error(self, cmd):
        service = _wire_failing_service(cmd)
        service.reply.side_effect = cmd._test_exc_cls(_DESCRIPTION)
        data = {"message_id": "id-1", "thread_id": "t-1", "body": "draft text"}

        resp = cmd.send_draft_reply(data, _make_req())  # must not raise

        assert not resp.success
        assert _DESCRIPTION in resp.context_data["message"]

    def test_unsubscribe_callback_counts_failure_without_raising(self, cmd):
        service = _wire_failing_service(cmd)
        service.send.side_effect = cmd._test_exc_cls(_DESCRIPTION)
        cmd._unsubscribe_storage = MagicMock()
        cmd._unsubscribe_storage.get.return_value = {
            "url": "",
            "mailto": "unsub@example.com",
            "one_click": False,
            "name": "Promo",
        }
        data = {"selected": [{"key": "promo@example.com"}]}

        resp = cmd.unsubscribe_selected(data, _make_req())  # must not raise

        assert resp.context_data["message"] == "Unsubscribed from 0 of 1."
        assert resp.context_data["detail_lines"] == ["Promo — failed"]
