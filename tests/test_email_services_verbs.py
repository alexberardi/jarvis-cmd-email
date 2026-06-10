"""mark_read / mark_unread service verbs — Gmail UNREAD label, IMAP \\Seen flag.

Also regression coverage that ImapEmailService addresses messages by stable UID
(``conn.uid(...)``) and never by sequence number (``conn.search/fetch/store/copy``),
since persisted ids tapped later must not be renumbered by an intervening EXPUNGE.
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))


def _load_real(name: str, filename: str):
    """Load a real email_shared module by path under its package name."""
    path = os.path.join(_ROOT, "email_shared", filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _install_email_shared():
    """Make email_shared importable: real modules + a stub factory.

    The pre-route/composition test files stub email_shared lazily behind an
    `if "email_shared" in sys.modules` guard, so whichever file imports first
    must leave a complete module set behind for the others.
    """
    if "email_shared" not in sys.modules:
        sys.modules["email_shared"] = types.ModuleType("email_shared")
    _load_real("email_shared.email_message", "email_message.py")
    if "email_shared.email_service_factory" not in sys.modules:
        esf = types.ModuleType("email_shared.email_service_factory")
        esf.create_email_service = lambda: None
        esf.get_email_provider = lambda: "gmail"
        sys.modules["email_shared.email_service_factory"] = esf
    _load_real("email_shared.triage", "triage.py")
    gmail = _load_real("email_shared.google_gmail_service", "google_gmail_service.py")
    imap = _load_real("email_shared.imap_email_service", "imap_email_service.py")
    return gmail, imap


_gmail_mod, _imap_mod = _install_email_shared()


@pytest.fixture
def gmail():
    return _gmail_mod.GoogleGmailService("token", "refresh", "client-id")


@pytest.fixture
def imap():
    return _imap_mod.ImapEmailService(
        imap_host="localhost",
        imap_port=993,
        smtp_host="localhost",
        smtp_port=465,
        username="u@example.com",
        password="pw",
    )


class TestGmailMarkReadUnread:
    def test_mark_read_removes_unread_label(self, gmail):
        gmail.modify_labels = MagicMock(return_value=True)
        assert gmail.mark_read("msg-1") is True
        gmail.modify_labels.assert_called_once_with("msg-1", remove_labels=["UNREAD"])

    def test_mark_unread_adds_unread_label(self, gmail):
        gmail.modify_labels = MagicMock(return_value=True)
        assert gmail.mark_unread("msg-1") is True
        gmail.modify_labels.assert_called_once_with("msg-1", add_labels=["UNREAD"])

    def test_mark_read_propagates_failure(self, gmail):
        gmail.modify_labels = MagicMock(return_value=False)
        assert gmail.mark_read("msg-1") is False


class TestImapMarkReadUnread:
    def test_mark_read_sets_seen_flag(self, imap):
        imap._set_flag = MagicMock(return_value=True)
        assert imap.mark_read("42") is True
        imap._set_flag.assert_called_once_with("42", "\\Seen", add=True)

    def test_mark_unread_clears_seen_flag(self, imap):
        imap._set_flag = MagicMock(return_value=True)
        assert imap.mark_unread("42") is True
        imap._set_flag.assert_called_once_with("42", "\\Seen", add=False)

    def test_mark_unread_propagates_failure(self, imap):
        imap._set_flag = MagicMock(return_value=False)
        assert imap.mark_unread("42") is False


# ── IMAP UID regression (ids must be stable UIDs, never sequence numbers) ────


_HEADER_BYTES = (
    b"From: Sender <sender@example.com>\r\n"
    b"Subject: Hello\r\n"
    b"Date: Tue, 09 Jun 2026 10:00:00 +0000\r\n"
    b"Message-ID: <id-7@example.com>\r\n"
    b"\r\n"
)


def _uid_responder(command, *args):
    """Fake conn.uid() — mirrors imaplib's (status, data) return shapes."""
    if command == "SEARCH":
        return ("OK", [b"7 9"])
    if command == "FETCH":
        if args[1] == "(RFC822 FLAGS)":
            raw = _HEADER_BYTES + b"body text\r\n"
            return ("OK", [(b"1 (UID 42 FLAGS (\\Seen) RFC822 {138}", raw), b")"])
        # envelope fetch: RFC822.HEADER FLAGS BODY.PEEK[TEXT]<0.200>
        return (
            "OK",
            [
                (b"1 (UID 7 FLAGS () RFC822.HEADER {123}", _HEADER_BYTES),
                (b" BODY[TEXT]<0> {9}", b"body text"),
                b")",
            ],
        )
    if command in ("STORE", "COPY"):
        return ("OK", [b""])
    raise AssertionError(f"unexpected UID command: {command}")


@pytest.fixture
def imap_conn(imap):
    """Mock the imaplib connection; the service must only issue UID variants."""
    conn = MagicMock()
    conn.uid.side_effect = _uid_responder
    imap._connect_imap = MagicMock(return_value=conn)
    return conn


def _assert_no_sequence_number_commands(conn):
    conn.search.assert_not_called()
    conn.fetch.assert_not_called()
    conn.store.assert_not_called()
    conn.copy.assert_not_called()


class TestImapUsesUidCommands:
    def test_search_uses_uid_search_and_uid_fetch(self, imap, imap_conn):
        emails = imap.search("is:unread")
        imap_conn.uid.assert_any_call("SEARCH", None, "UNSEEN")
        imap_conn.uid.assert_any_call(
            "FETCH", b"9", "(RFC822.HEADER FLAGS BODY.PEEK[TEXT]<0.200>)"
        )
        imap_conn.uid.assert_any_call(
            "FETCH", b"7", "(RFC822.HEADER FLAGS BODY.PEEK[TEXT]<0.200>)"
        )
        # ids surfaced to callers are the UIDs returned by UID SEARCH, newest first
        assert [e.id for e in emails] == ["9", "7"]
        _assert_no_sequence_number_commands(imap_conn)

    def test_fetch_message_uses_uid_fetch(self, imap, imap_conn):
        msg = imap.fetch_message("42")
        imap_conn.uid.assert_called_once_with("FETCH", "42", "(RFC822 FLAGS)")
        assert msg is not None
        assert msg.id == "42"
        _assert_no_sequence_number_commands(imap_conn)

    def test_mark_read_uses_uid_store(self, imap, imap_conn):
        assert imap.mark_read("42") is True
        imap_conn.uid.assert_called_once_with("STORE", "42", "+FLAGS", "\\Seen")
        _assert_no_sequence_number_commands(imap_conn)

    def test_star_uses_uid_store(self, imap, imap_conn):
        assert imap.star("42") is True
        imap_conn.uid.assert_called_once_with("STORE", "42", "+FLAGS", "\\Flagged")
        _assert_no_sequence_number_commands(imap_conn)

    def test_archive_uses_uid_copy_and_store(self, imap, imap_conn):
        assert imap.archive("42") is True
        imap_conn.uid.assert_any_call("COPY", "42", "Archive")
        imap_conn.uid.assert_any_call("STORE", "42", "+FLAGS", "\\Deleted")
        imap_conn.expunge.assert_called_once()
        _assert_no_sequence_number_commands(imap_conn)

    def test_trash_uses_uid_copy_and_store(self, imap, imap_conn):
        assert imap.trash("42") is True
        imap_conn.uid.assert_any_call("COPY", "42", "Trash")
        imap_conn.uid.assert_any_call("STORE", "42", "+FLAGS", "\\Deleted")
        imap_conn.expunge.assert_called_once()
        _assert_no_sequence_number_commands(imap_conn)
