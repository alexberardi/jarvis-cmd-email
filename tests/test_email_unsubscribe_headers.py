"""List-Unsubscribe header retention — parser + both services' _parse_message sites."""

import email as email_lib
import importlib.util
import os
import sys
import types

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
    msg_mod = _load_real("email_shared.email_message", "email_message.py")
    if "email_shared.email_service_factory" not in sys.modules:
        esf = types.ModuleType("email_shared.email_service_factory")
        esf.create_email_service = lambda: None
        esf.get_email_provider = lambda: "gmail"
        sys.modules["email_shared.email_service_factory"] = esf
    _load_real("email_shared.triage", "triage.py")
    gmail = _load_real("email_shared.google_gmail_service", "google_gmail_service.py")
    imap = _load_real("email_shared.imap_email_service", "imap_email_service.py")
    return msg_mod, gmail, imap


_msg_mod, _gmail_mod, _imap_mod = _install_email_shared()

parse_unsubscribe_headers = _msg_mod.parse_unsubscribe_headers


# ── Shared parser ────────────────────────────────────────────────────────────


class TestParseUnsubscribeHeaders:
    def test_combined_mailto_and_https(self):
        url, mailto, one_click = parse_unsubscribe_headers(
            "<mailto:unsub@example.com?subject=stop>, <https://example.com/u?id=1>",
            "List-Unsubscribe=One-Click",
        )
        assert url == "https://example.com/u?id=1"
        assert mailto == "unsub@example.com"  # address only, query dropped
        assert one_click is True

    def test_first_https_entry_wins(self):
        url, _, _ = parse_unsubscribe_headers(
            "<https://first.example/u>, <https://second.example/u>", ""
        )
        assert url == "https://first.example/u"

    def test_first_mailto_entry_wins(self):
        _, mailto, _ = parse_unsubscribe_headers(
            "<mailto:a@example.com>, <mailto:b@example.com>", ""
        )
        assert mailto == "a@example.com"

    def test_plain_http_is_not_picked_up(self):
        url, _, _ = parse_unsubscribe_headers("<http://example.com/u>", "")
        assert url == ""

    def test_empty_headers(self):
        assert parse_unsubscribe_headers("", "") == ("", "", False)

    def test_one_click_is_case_insensitive(self):
        _, _, one_click = parse_unsubscribe_headers(
            "<https://example.com/u>", "list-unsubscribe=one-click"
        )
        assert one_click is True

    def test_garbage_post_header_is_not_one_click(self):
        _, _, one_click = parse_unsubscribe_headers(
            "<https://example.com/u>", "something-else"
        )
        assert one_click is False

    def test_whitespace_between_entries(self):
        url, mailto, _ = parse_unsubscribe_headers(
            "< mailto:unsub@example.com >,\r\n < https://example.com/u >", ""
        )
        assert url == "https://example.com/u"
        assert mailto == "unsub@example.com"


# ── Gmail _parse_message ─────────────────────────────────────────────────────


def _gmail_raw(extra_headers: list[dict] | None = None) -> dict:
    headers = [
        {"name": "From", "value": "Promo <promo@example.com>"},
        {"name": "Subject", "value": "Big deals"},
        {"name": "Date", "value": "Tue, 09 Jun 2026 10:00:00 +0000"},
    ]
    headers.extend(extra_headers or [])
    return {
        "id": "m-1",
        "threadId": "t-1",
        "snippet": "deals deals deals",
        "labelIds": ["UNREAD", "INBOX"],
        "payload": {"mimeType": "text/plain", "headers": headers, "body": {}},
    }


@pytest.fixture
def gmail():
    return _gmail_mod.GoogleGmailService("token", "refresh", "client-id")


class TestGmailUnsubscribeParsing:
    def test_populates_unsubscribe_fields(self, gmail):
        raw = _gmail_raw([
            {
                "name": "List-Unsubscribe",
                "value": "<mailto:unsub@example.com?subject=stop>, <https://example.com/u?id=1>",
            },
            {"name": "List-Unsubscribe-Post", "value": "List-Unsubscribe=One-Click"},
        ])
        msg = gmail._parse_message(raw)
        assert msg.unsubscribe_url == "https://example.com/u?id=1"
        assert msg.unsubscribe_mailto == "unsub@example.com"
        assert msg.unsubscribe_one_click is True

    def test_header_name_case_insensitive(self, gmail):
        raw = _gmail_raw([
            {"name": "LIST-UNSUBSCRIBE", "value": "<https://example.com/u>"},
            {"name": "LIST-UNSUBSCRIBE-POST", "value": "LIST-UNSUBSCRIBE=ONE-CLICK"},
        ])
        msg = gmail._parse_message(raw)
        assert msg.unsubscribe_url == "https://example.com/u"
        assert msg.unsubscribe_one_click is True

    def test_url_only_without_one_click(self, gmail):
        raw = _gmail_raw([
            {"name": "List-Unsubscribe", "value": "<https://example.com/u>"},
        ])
        msg = gmail._parse_message(raw)
        assert msg.unsubscribe_url == "https://example.com/u"
        assert msg.unsubscribe_mailto == ""
        assert msg.unsubscribe_one_click is False

    def test_defaults_when_headers_absent(self, gmail):
        msg = gmail._parse_message(_gmail_raw())
        assert msg.unsubscribe_url == ""
        assert msg.unsubscribe_mailto == ""
        assert msg.unsubscribe_one_click is False


# ── IMAP _parse_message ──────────────────────────────────────────────────────


def _imap_message(extra_headers: str = "") -> email_lib.message.Message:
    raw = (
        "From: Promo <promo@example.com>\r\n"
        "Subject: Big deals\r\n"
        "Date: Tue, 09 Jun 2026 10:00:00 +0000\r\n"
        "Message-ID: <abc@example.com>\r\n"
        f"{extra_headers}"
        "\r\n"
        "deals deals deals\r\n"
    )
    return email_lib.message_from_string(raw)


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


class TestImapUnsubscribeParsing:
    def test_populates_unsubscribe_fields(self, imap):
        mime = _imap_message(
            "List-Unsubscribe: <https://example.com/u?id=1>, <mailto:unsub@example.com?subject=stop>\r\n"
            "List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n"
        )
        msg = imap._parse_message(mime, "7", b"FLAGS ()")
        assert msg.unsubscribe_url == "https://example.com/u?id=1"
        assert msg.unsubscribe_mailto == "unsub@example.com"
        assert msg.unsubscribe_one_click is True

    def test_mailto_only(self, imap):
        mime = _imap_message("List-Unsubscribe: <mailto:unsub@example.com>\r\n")
        msg = imap._parse_message(mime, "7", b"FLAGS ()")
        assert msg.unsubscribe_url == ""
        assert msg.unsubscribe_mailto == "unsub@example.com"
        assert msg.unsubscribe_one_click is False

    def test_defaults_when_headers_absent(self, imap):
        msg = imap._parse_message(_imap_message(), "7", b"FLAGS ()")
        assert msg.unsubscribe_url == ""
        assert msg.unsubscribe_mailto == ""
        assert msg.unsubscribe_one_click is False
