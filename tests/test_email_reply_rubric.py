"""Layered reply-worthiness rubric (email_shared.reply_rubric).

- To/Cc header retention at both services' parse sites (the rubric judges
  directness from them).
- is_direct_recipient matrix — conservative on missing data, strict on a
  known address absent from a populated To header.
- resolve_user_address — user-scoped IMAP_USERNAME secret, None fallback.
- select_reply_worthy — single fail-closed LLM call: BASELINE_RULES + the
  user's instructions ON TOP, candidate lines with From/To/Cc/Subject/snippet,
  JSON-indices output with noise stripping and out-of-range dropping.
- Stage-2 draft prompt (email_shared.reply_drafts) embeds the same rubric.
"""

import email as email_lib
import importlib.util
import os
import sys
import types
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from jarvis_command_sdk import set_backend
from jarvis_command_sdk.storage import StorageBackend

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
    _load_real("email_shared.email_message", "email_shared", "email_message.py")
    if "email_shared.email_service_factory" not in sys.modules:
        esf = types.ModuleType("email_shared.email_service_factory")
        esf.create_email_service = lambda: None
        esf.get_email_provider = lambda: "gmail"
        sys.modules["email_shared.email_service_factory"] = esf
    gmail = _load_real("email_shared.google_gmail_service", "email_shared", "google_gmail_service.py")
    imap = _load_real("email_shared.imap_email_service", "email_shared", "imap_email_service.py")
    rubric = _load_real("email_shared.reply_rubric", "email_shared", "reply_rubric.py")
    drafts = _load_real("email_shared.reply_drafts", "email_shared", "reply_drafts.py")
    return gmail, imap, rubric, drafts


_gmail_mod, _imap_mod, _rubric, _drafts = _install_email_shared()

BASELINE_RULES = _rubric.BASELINE_RULES
is_direct_recipient = _rubric.is_direct_recipient
select_reply_worthy = _rubric.select_reply_worthy


class _FakeStorageBackend(StorageBackend):
    def __init__(self) -> None:
        self.secrets: dict[tuple[str, str], str] = {}

    def save(self, command_name, data_key, data, expires_at=None) -> None: ...

    def get(self, command_name, data_key):
        return None

    def get_all(self, command_name):
        return []

    def delete(self, command_name, data_key) -> bool:
        return False

    def delete_all(self, command_name) -> int:
        return 0

    def get_secret(self, key, scope, user_id=None):
        return self.secrets.get((key, scope))

    def set_secret(self, key, value, scope, value_type="string", user_id=None) -> None:
        self.secrets[(key, scope)] = value

    def delete_secret(self, key, scope, user_id=None) -> None:
        self.secrets.pop((key, scope), None)


def _install_fake_node_llm_client(monkeypatch, ask_llm_impl) -> None:
    services_mod = sys.modules.get("services") or types.ModuleType("services")
    monkeypatch.setitem(sys.modules, "services", services_mod)
    node_mod = types.ModuleType("services.node_llm_client")
    node_mod.ask_llm = ask_llm_impl  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.node_llm_client", node_mod)
    services_mod.node_llm_client = node_mod  # type: ignore[attr-defined]


def _make_email(idx: int, to: str = "", cc: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        id=f"id-{idx}",
        sender=f"Sender {idx} <sender{idx}@example.com>",
        subject=f"Subject {idx}",
        snippet=f"Snippet {idx}",
        to=to,
        cc=cc,
    )


# ── To/Cc parsing: Gmail ─────────────────────────────────────────────────────


def _gmail_raw(extra_headers: list[dict] | None = None) -> dict:
    headers = [
        {"name": "From", "value": "Jane <jane@example.com>"},
        {"name": "Subject", "value": "Lunch?"},
        {"name": "Date", "value": "Tue, 09 Jun 2026 10:00:00 +0000"},
    ]
    headers.extend(extra_headers or [])
    return {
        "id": "m-1",
        "threadId": "t-1",
        "snippet": "are you free",
        "labelIds": ["UNREAD", "INBOX"],
        "payload": {"mimeType": "text/plain", "headers": headers, "body": {}},
    }


@pytest.fixture
def gmail():
    return _gmail_mod.GoogleGmailService("token", "refresh", "client-id")


class TestGmailToCcParsing:
    def test_populates_to_and_cc(self, gmail):
        raw = _gmail_raw([
            {"name": "To", "value": "Alex Berardi <alex@example.com>"},
            {"name": "Cc", "value": "carol@example.com, dave@example.com"},
        ])
        msg = gmail._parse_message(raw)
        assert msg.to == "Alex Berardi <alex@example.com>"
        assert msg.cc == "carol@example.com, dave@example.com"

    def test_header_name_case_insensitive(self, gmail):
        raw = _gmail_raw([{"name": "TO", "value": "alex@example.com"}])
        assert gmail._parse_message(raw).to == "alex@example.com"

    def test_defaults_when_headers_absent(self, gmail):
        msg = gmail._parse_message(_gmail_raw())
        assert msg.to == ""
        assert msg.cc == ""


# ── To/Cc parsing: IMAP (full parse + search envelope) ───────────────────────


def _imap_message(extra_headers: str = "") -> email_lib.message.Message:
    raw = (
        "From: Jane <jane@example.com>\r\n"
        "Subject: Lunch?\r\n"
        "Date: Tue, 09 Jun 2026 10:00:00 +0000\r\n"
        "Message-ID: <abc@example.com>\r\n"
        f"{extra_headers}"
        "\r\n"
        "are you free\r\n"
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


class TestImapToCcParsing:
    def test_parse_message_populates_to_and_cc(self, imap):
        mime = _imap_message(
            "To: Alex Berardi <alex@example.com>\r\n"
            "Cc: carol@example.com\r\n"
        )
        msg = imap._parse_message(mime, "7", b"FLAGS ()")
        assert msg.to == "Alex Berardi <alex@example.com>"
        assert msg.cc == "carol@example.com"

    def test_parse_message_defaults_when_absent(self, imap):
        msg = imap._parse_message(_imap_message(), "7", b"FLAGS ()")
        assert msg.to == ""
        assert msg.cc == ""

    def test_fetch_envelope_populates_to_and_cc(self, imap):
        # Search results come through _fetch_envelope — the directness screen
        # runs on those, so the headers must survive the lightweight path too.
        header_bytes = (
            b"From: Jane <jane@example.com>\r\n"
            b"Subject: Lunch?\r\n"
            b"To: Alex Berardi <alex@example.com>\r\n"
            b"Cc: carol@example.com\r\n"
            b"Date: Tue, 09 Jun 2026 10:00:00 +0000\r\n"
            b"\r\n"
        )
        conn = MagicMock()
        conn.uid.return_value = (
            "OK",
            [
                (b"1 (UID 7 FLAGS () RFC822.HEADER {123}", header_bytes),
                (b" BODY[TEXT]<0> {9}", b"body text"),
                b")",
            ],
        )
        msg = imap._fetch_envelope(conn, b"7")
        assert msg.to == "Alex Berardi <alex@example.com>"
        assert msg.cc == "carol@example.com"


# ── is_direct_recipient matrix ───────────────────────────────────────────────


class TestIsDirectRecipient:
    def test_address_in_to_is_direct(self):
        email = _make_email(1, to="Alex Berardi <alex@example.com>")
        assert is_direct_recipient(email, "alex@example.com") is True

    def test_only_in_cc_is_not_direct(self):
        email = _make_email(1, to="other@example.com", cc="alex@example.com")
        assert is_direct_recipient(email, "alex@example.com") is False

    def test_absent_everywhere_is_not_direct(self):
        email = _make_email(1, to="other@example.com", cc="third@example.com")
        assert is_direct_recipient(email, "alex@example.com") is False

    def test_unknown_address_defers_to_llm(self):
        email = _make_email(1, to="other@example.com")
        assert is_direct_recipient(email, None) is True

    def test_empty_to_header_never_false_drops(self):
        # Some providers omit To on fetch — don't drop on missing data.
        email = _make_email(1, to="")
        assert is_direct_recipient(email, "alex@example.com") is True

    def test_missing_to_attribute_never_false_drops(self):
        email = SimpleNamespace(id="x", sender="s", subject="s", snippet="s")
        assert is_direct_recipient(email, "alex@example.com") is True

    def test_match_is_case_insensitive(self):
        email = _make_email(1, to="ALEX@Example.COM")
        assert is_direct_recipient(email, "alex@example.com") is True

    def test_multi_recipient_to_matches(self):
        email = _make_email(1, to="bob@example.com, Alex <alex@example.com>")
        assert is_direct_recipient(email, "alex@example.com") is True


# ── resolve_user_address ─────────────────────────────────────────────────────


class TestResolveUserAddress:
    def test_reads_user_scoped_imap_username(self):
        backend = _FakeStorageBackend()
        backend.secrets[("IMAP_USERNAME", "user")] = "alex@example.com"
        set_backend(backend)
        try:
            assert _rubric.resolve_user_address() == "alex@example.com"
        finally:
            set_backend(None)

    def test_missing_secret_returns_none(self):
        backend = _FakeStorageBackend()
        set_backend(backend)
        try:
            assert _rubric.resolve_user_address() is None
        finally:
            set_backend(None)

    def test_whitespace_secret_returns_none(self):
        backend = _FakeStorageBackend()
        backend.secrets[("IMAP_USERNAME", "user")] = "   "
        set_backend(backend)
        try:
            assert _rubric.resolve_user_address() is None
        finally:
            set_backend(None)

    def test_no_backend_returns_none(self):
        assert _rubric.resolve_user_address() is None


# ── select_reply_worthy: prompt contract ─────────────────────────────────────


class TestSelectReplyWorthyPrompt:
    def _capture(self, monkeypatch, response="[]"):
        captured: dict[str, Any] = {}

        def fake_ask(prompt: str, *, system=None, **kw):
            captured["prompt"] = prompt
            captured["system"] = system
            return response

        _install_fake_node_llm_client(monkeypatch, fake_ask)
        return captured

    def test_system_contains_baseline_rules_and_instructions_on_top(self, monkeypatch):
        captured = self._capture(monkeypatch)
        select_reply_worthy([_make_email(1)], "clients and invoices", None)

        assert BASELINE_RULES in captured["system"]
        assert "apply ON TOP" in captured["system"]
        assert "clients and invoices" in captured["system"]
        # Strict-judge framing
        assert "HARD" in captured["system"]
        assert "SKIP" in captured["system"]
        assert "false positive" in captured["system"].lower()

    def test_candidate_lines_carry_from_to_cc_subject_snippet(self, monkeypatch):
        captured = self._capture(monkeypatch)
        email = _make_email(1, to="Alex <alex@example.com>", cc="carol@example.com")
        select_reply_worthy([email], "rule", "alex@example.com")

        assert (
            "1. From: Sender 1 <sender1@example.com>\n"
            "   To: Alex <alex@example.com>\n"
            "   Cc: carol@example.com\n"
            "   Subject: Subject 1\n"
            "   Snippet 1"
        ) in captured["prompt"]

    def test_user_address_included_when_known(self, monkeypatch):
        captured = self._capture(monkeypatch)
        select_reply_worthy([_make_email(1)], "rule", "alex@example.com")
        assert "alex@example.com" in captured["prompt"]

    def test_user_address_line_omitted_when_unknown(self, monkeypatch):
        captured = self._capture(monkeypatch)
        select_reply_worthy([_make_email(1)], "rule", None)
        assert "user's own email address" not in captured["prompt"]

    def test_snippet_truncated_to_300_chars(self, monkeypatch):
        captured = self._capture(monkeypatch)
        email = _make_email(1)
        email.snippet = "x" * 1000
        select_reply_worthy([email], "rule", None)
        assert "x" * 300 in captured["prompt"]
        assert "x" * 301 not in captured["prompt"]


# ── select_reply_worthy: fail-closed matrix ──────────────────────────────────


class TestSelectReplyWorthyFailClosed:
    def test_no_emails_returns_empty_without_llm(self, monkeypatch):
        ask = MagicMock()
        _install_fake_node_llm_client(monkeypatch, ask)
        assert select_reply_worthy([], "rule", None) == []
        ask.assert_not_called()

    def test_llm_unavailable_fails_closed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "services.node_llm_client", None)
        assert select_reply_worthy([_make_email(1)], "rule", None) == []

    def test_llm_returns_matching_indices(self, monkeypatch):
        emails = [_make_email(1), _make_email(2), _make_email(3)]
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: "[1, 3]")
        assert select_reply_worthy(emails, "rule", None) == [emails[0], emails[2]]

    def test_empty_array_means_no_matches(self, monkeypatch):
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: "[]")
        assert select_reply_worthy([_make_email(1)], "rule", None) == []

    def test_none_response_fails_closed(self, monkeypatch):
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: None)
        assert select_reply_worthy([_make_email(1)], "rule", None) == []

    def test_garbage_fails_closed(self, monkeypatch):
        _install_fake_node_llm_client(
            monkeypatch, lambda *a, **kw: "Emails 1 and 2 look important."
        )
        assert select_reply_worthy([_make_email(1), _make_email(2)], "rule", None) == []

    def test_think_block_and_code_fence_stripped(self, monkeypatch):
        emails = [_make_email(1), _make_email(2), _make_email(3)]
        raw = "<think>let me reason</think>\n```json\n[2]\n```"
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: raw)
        assert select_reply_worthy(emails, "rule", None) == [emails[1]]

    def test_out_of_range_indices_dropped(self, monkeypatch):
        emails = [_make_email(1), _make_email(2)]
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: "[1, 5, 99, 0, -1]")
        assert select_reply_worthy(emails, "rule", None) == [emails[0]]

    def test_non_numeric_indices_dropped(self, monkeypatch):
        emails = [_make_email(1), _make_email(2)]
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: '["foo", 2]')
        assert select_reply_worthy(emails, "rule", None) == [emails[1]]

    def test_non_array_json_fails_closed(self, monkeypatch):
        _install_fake_node_llm_client(monkeypatch, lambda *a, **kw: '{"match": 1}')
        assert select_reply_worthy([_make_email(1)], "rule", None) == []


# ── Stage-2 draft prompt embeds the same rubric ──────────────────────────────


class TestStage2PromptCarriesRubric:
    def test_draft_system_prompt_contains_baseline_rules(self, monkeypatch):
        captured: dict[str, Any] = {}

        def fake_ask(prompt: str, *, system=None, **kw):
            captured["system"] = system
            return '{"should_reply": false, "draft": ""}'

        _install_fake_node_llm_client(monkeypatch, fake_ask)
        email = _make_email(1)
        email.body = "Body 1"
        _drafts.generate_draft_status(email)

        assert BASELINE_RULES in captured["system"]
        # The original drafting guidance is still present alongside it.
        assert "should_reply" in captured["system"]
        assert "150 words" in captured["system"]
