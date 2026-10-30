"""save_attachments / draft_reply: files saved safely, replies staged in Drafts, never sent.

The live IMAP round trip runs only with JARVIS_IMAP_LIVE=host:port (an IMAPS server with a
throwaway account, e.g. GreenMail in Docker); never point it at a real mailbox.
"""

import email as email_lib
import importlib.util
import imaplib
import os
import sys
import time
import types
from datetime import datetime
from email import policy
from email.message import EmailMessage as MimeMessage
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))


def _load_real(name: str, filename: str):
    path = os.path.join(_ROOT, "email_shared", filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _install_email_shared():
    if "email_shared" not in sys.modules:
        sys.modules["email_shared"] = types.ModuleType("email_shared")
    _load_real("email_shared.email_message", "email_message.py")
    if "email_shared.email_service_factory" not in sys.modules:
        esf = types.ModuleType("email_shared.email_service_factory")
        esf.create_email_service = lambda: None
        esf.get_email_provider = lambda: "gmail"
        sys.modules["email_shared.email_service_factory"] = esf
    _load_real("email_shared.triage", "triage.py")
    att = _load_real("email_shared.attachments", "attachments.py")
    imap = _load_real("email_shared.imap_email_service", "imap_email_service.py")
    return att, imap


att, _imap_mod = _install_email_shared()


def _load_command():
    spec = importlib.util.spec_from_file_location(
        "email_cmd_attachments", os.path.join(_ROOT, "commands", "email", "command.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EmailCommand


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    return tmp_path


def _raw_email(subject="Timesheet", files=(("hours.xlsx", b"PK\x03\x04data"),), msgid="<orig-1@example.com>") -> bytes:
    m = MimeMessage()
    m["From"] = "Dana <dana@example.com>"
    m["To"] = "jarvis@localhost"
    m["Subject"] = subject
    m["Message-ID"] = msgid
    m["References"] = "<root@example.com>"
    m.set_content("Can you total these up and send it back?")
    for name, data in files:
        m.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    return m.as_bytes()


# ── attachments module ───────────────────────────────────────────────────────


class TestSafeName:
    @pytest.mark.parametrize("raw,want", [
        ("../../.ssh/authorized_keys", "authorized_keys"),
        ("..\\..\\evil.bat", "evil.bat"),
        (".bashrc", "bashrc"),
        ("a\x00b:c.txt", "a_b_c.txt"),
        ("", "attachment"),
        ("...", "attachment"),
    ])
    def test_reduces_to_a_safe_basename(self, raw, want):
        assert att.safe_name(raw) == want


class TestSaveAttachments:
    def test_saves_privately_without_overwriting(self, home):
        msg = att.parse_raw(_raw_email(files=[("hours.xlsx", b"one"), ("hours.xlsx", b"two")]))
        dest = home / "Documents" / "Jarvis" / "Attachments" / "x"
        dest.mkdir(parents=True)
        (dest / "hours.xlsx").write_bytes(b"user's own")

        saved = att.save_attachments(msg, str(dest))

        assert [os.path.basename(p) for p in saved] == ["hours (2).xlsx", "hours (3).xlsx"]
        assert (dest / "hours.xlsx").read_bytes() == b"user's own"
        assert open(saved[0], "rb").read() == b"one"
        assert os.stat(saved[0]).st_mode & 0o777 == 0o600

    def test_traversal_names_stay_in_the_folder(self, home):
        msg = att.parse_raw(_raw_email(files=[("../../../escape.txt", b"x")]))
        dest = home / "Documents" / "in"
        saved = att.save_attachments(msg, str(dest))
        assert saved == [str(dest / "escape.txt")]
        assert os.stat(dest).st_mode & 0o777 == 0o700

    def test_skips_files_over_the_cap(self, home, monkeypatch):
        monkeypatch.setattr(att, "MAX_ATTACHMENT_BYTES", 3)
        msg = att.parse_raw(_raw_email(files=[("big.bin", b"1234"), ("ok.bin", b"123")]))
        saved = att.save_attachments(msg, str(home / "Documents" / "d"))
        assert [os.path.basename(p) for p in saved] == ["ok.bin"]

    def test_folder_for_is_under_attachments(self):
        assert att.folder_for("Re: Q3/report", "2026-10-30") == os.path.join(
            att.ATTACHMENTS_DIR, "2026-10-30 Re_ Q3_report"
        )


class TestDraftFile:
    def test_accepts_a_file_in_documents(self, home):
        f = home / "Documents" / "out.ods"
        f.write_bytes(b"x")
        assert att.draft_file(str(f)) == str(f)

    def test_refuses_a_file_outside_documents(self, home):
        (home / ".ssh").mkdir()
        key = home / ".ssh" / "id_ed25519"
        key.write_bytes(b"secret")
        with pytest.raises(ValueError, match="isn't in your Documents"):
            att.draft_file(str(key))

    def test_refuses_a_symlink_out_of_documents(self, home):
        secret = home / "secret"
        secret.write_bytes(b"s")
        link = home / "Documents" / "innocent.pdf"
        link.symlink_to(secret)
        with pytest.raises(ValueError, match="isn't in your Documents"):
            att.draft_file(str(link))

    def test_refuses_dotdot_out_of_documents(self, home):
        (home / "secret").write_bytes(b"s")
        with pytest.raises(ValueError):
            att.draft_file(str(home / "Documents" / ".." / "secret"))

    def test_refuses_missing_file(self, home):
        with pytest.raises(ValueError, match="doesn't exist"):
            att.draft_file(str(home / "Documents" / "nope.odt"))


class TestBuildReplyDraft:
    def test_threads_and_attaches(self, home):
        f = home / "Documents" / "totals.ods"
        f.write_bytes(b"ODS")
        orig = att.parse_raw(_raw_email())

        draft = att.build_reply_draft(orig, "jarvis@localhost", "Totals attached.", [str(f)])

        assert draft["To"] == "Dana <dana@example.com>"
        assert draft["Subject"] == "Re: Timesheet"
        assert draft["In-Reply-To"] == "<orig-1@example.com>"
        assert draft["References"] == "<root@example.com> <orig-1@example.com>"
        parts = att.attachment_parts(att.parse_raw(draft.as_bytes()))
        assert parts == [("totals.ods", b"ODS")]

    def test_does_not_double_re(self):
        orig = att.parse_raw(_raw_email(subject="RE: hi", files=()))
        assert att.build_reply_draft(orig, "", "b", [])["Subject"] == "RE: hi"


# ── command actions ──────────────────────────────────────────────────────────


def _em(id_: str, subject: str = "Timesheet") -> SimpleNamespace:
    return SimpleNamespace(
        id=id_, thread_id=f"t-{id_}", sender="Dana <dana@example.com>", sender_name="Dana",
        subject=subject, date=datetime(2026, 10, 30, 9, 0),
    )


@pytest.fixture
def cmd(home):
    instance = _load_command()()
    instance._storage = MagicMock()
    return instance


@pytest.fixture
def service():
    s = MagicMock()
    # Newest first: id-2 has no files, id-1 does.
    s.search.return_value = [_em("id-2"), _em("id-1")]
    s.fetch_raw.side_effect = lambda i: _raw_email(files=()) if i == "id-2" else _raw_email()
    s.create_draft.return_value = {"id": "<d@x>", "folder": "Drafts", "status": "draft"}
    s.username = "jarvis@localhost"
    return s


class TestSaveAttachmentsAction:
    def test_saves_the_newest_match_with_files(self, cmd, service, home):
        cmd._get_service = MagicMock(return_value=service)

        resp = cmd._run_save_attachments(query="from:dana timesheet")

        assert resp.success
        data = resp.context_data
        assert [os.path.basename(p) for p in data["saved_files"]] == ["hours.xlsx"]
        assert data["saved_files"][0].startswith(str(home / "Documents" / "Jarvis" / "Attachments"))
        assert data["email"]["id"] == "id-1"
        assert "total these up" in data["email"]["body"]

    def test_none_with_files_is_not_found(self, cmd, service):
        service.fetch_raw.side_effect = lambda i: _raw_email(files=())
        cmd._get_service = MagicMock(return_value=service)
        resp = cmd._run_save_attachments(query="x")
        assert not resp.success
        assert resp.context_data == {"error": "not_found"}

    def test_needs_a_query_or_index(self, cmd, service):
        cmd._get_service = MagicMock(return_value=service)
        assert not cmd._run_save_attachments().success


class TestDraftReplyAction:
    def test_answers_the_email_whose_files_were_saved(self, cmd, service, home):
        cmd._get_service = MagicMock(return_value=service)
        cmd._run_save_attachments(query="timesheet")
        out = home / "Documents" / "totals.ods"
        out.write_bytes(b"ODS")

        resp = cmd._run_draft_reply(query="timesheet", body="Done, see attached.", attachments=str(out))

        assert resp.success
        draft = service.create_draft.call_args.args[0]
        assert draft["In-Reply-To"] == "<orig-1@example.com>"  # id-1, not the newer id-2
        assert resp.context_data["draft"]["attachments"] == [str(out)]
        assert "hasn't been sent" in resp.context_data["message"]
        service.send.assert_not_called()
        service.reply.assert_not_called()

    def test_refuses_an_attachment_outside_documents(self, cmd, service, home):
        cmd._get_service = MagicMock(return_value=service)
        (home / "keys").write_bytes(b"k")
        resp = cmd._run_draft_reply(query="timesheet", body="hi", attachments=str(home / "keys"))
        assert not resp.success
        assert resp.context_data == {"error": "bad_attachment"}
        service.create_draft.assert_not_called()

    def test_needs_a_body(self, cmd, service):
        cmd._get_service = MagicMock(return_value=service)
        assert not cmd._run_draft_reply(query="timesheet", body=" ").success
        service.create_draft.assert_not_called()


# ── IMAP Drafts folder ───────────────────────────────────────────────────────


class TestImapDraftsFolder:
    @pytest.mark.parametrize("lines,want", [
        ([b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Drafts) "/" "Brouillons"'], "Brouillons"),
        ([b'(\\HasNoChildren) "." "INBOX"', b'(\\HasNoChildren) "." "INBOX.Drafts"'], "INBOX.Drafts"),
        ([b'(\\HasNoChildren) "/" "INBOX"'], None),
    ])
    def test_picks_flagged_then_named(self, lines, want):
        conn = MagicMock()
        conn.list.return_value = ("OK", lines)
        assert _imap_mod.ImapEmailService._drafts_folder(conn) == want


# ── live IMAP round trip (opt-in) ────────────────────────────────────────────


_LIVE = os.environ.get("JARVIS_IMAP_LIVE", "")


@pytest.mark.skipif(not _LIVE, reason="set JARVIS_IMAP_LIVE=host:port for a throwaway IMAPS server")
def test_live_imap_round_trip(home):
    host, port = _LIVE.rsplit(":", 1)
    user = os.environ.get("JARVIS_IMAP_LIVE_USER", "jarvis@localhost")
    password = os.environ.get("JARVIS_IMAP_LIVE_PASSWORD", "pw")
    svc = _imap_mod.ImapEmailService(
        imap_host=host, imap_port=int(port), smtp_host=host, smtp_port=0,
        username=user, password=password, use_ssl=True,
    )
    msgid = f"<live-{time.time_ns()}@example.com>"
    conn = imaplib.IMAP4_SSL(host, int(port))
    conn.login(user, password)
    conn.append("INBOX", "", imaplib.Time2Internaldate(time.time()), _raw_email(msgid=msgid))
    conn.logout()

    found = svc.search("Timesheet", max_results=1)[0]
    raw = svc.fetch_raw(found.id)
    parsed = att.parse_raw(raw)
    saved = att.save_attachments(parsed, str(home / "Documents" / "in"))
    assert open(saved[0], "rb").read() == b"PK\x03\x04data"

    out = home / "Documents" / "totals.ods"
    out.write_bytes(b"ODS")
    result = svc.create_draft(att.build_reply_draft(parsed, user, "Totals attached.", [str(out)]))

    conn = imaplib.IMAP4_SSL(host, int(port))
    conn.login(user, password)
    try:
        assert conn.select(f'"{result["folder"]}"', readonly=True)[0] == "OK"
        status, data = conn.uid("SEARCH", None, "HEADER", "In-Reply-To", msgid)
        uids = data[0].split()
        assert status == "OK" and len(uids) == 1
        _, fetched = conn.uid("FETCH", uids[-1], "(FLAGS BODY.PEEK[])")
        assert b"\\Draft" in fetched[0][0]
        draft = email_lib.message_from_bytes(fetched[0][1], policy=policy.default)
        assert att.attachment_parts(draft) == [("totals.ods", b"ODS")]
        # Nothing went out: the INBOX holds only the original.
        conn.select("INBOX", readonly=True)
        _, sent = conn.uid("SEARCH", None, "HEADER", "In-Reply-To", msgid)
        assert sent[0].split() == []
    finally:
        conn.logout()
