"""Unsubscribe scan — fast-path, queries, grouping, thresholds, storage records, inbox post."""

import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jarvis_command_sdk import RequestInformation, set_inbox_backend
from jarvis_command_sdk.inbox import InboxBackend

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


def _install_email_shared():
    """Real email_message + triage; stub factory (no live service construction)."""
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


def _load_command_module():
    cmd_path = os.path.join(_ROOT, "commands", "email", "command.py")
    spec = importlib.util.spec_from_file_location("email_unsub_scan_cmd_under_test", cmd_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_cmd_module = _load_command_module()

GMAIL_QUERY = (
    "is:unread older_than:7d newer_than:90d (category:promotions OR category:updates)"
)
IMAP_QUERY = "is:unread newer_than:90d"


def _make_email(addr: str, name: str, idx: int, days_old: int = 30) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"{addr}-{idx}",
        thread_id=f"thread-{addr}-{idx}",
        sender=f"{name} <{addr}>",
        sender_name=name,
        subject=f"Subject {idx}",
        snippet=f"Snippet {idx}",
        body="",
        date=datetime.now(timezone.utc) - timedelta(days=days_old),
    )


def _make_full(url: str = "", mailto: str = "", one_click: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        unsubscribe_url=url,
        unsubscribe_mailto=mailto,
        unsubscribe_one_click=one_click,
    )


def _make_req(user_id: int | None = 42, is_pre_routed: bool = False) -> RequestInformation:
    return RequestInformation(
        voice_command="clean up my subscriptions",
        conversation_id="conv-1",
        user_id=user_id,
        is_pre_routed=is_pre_routed,
    )


class _FakeInboxBackend(InboxBackend):
    def __init__(self, tag: str = "ok") -> None:
        self.tag = tag
        self.calls: list[dict] = []

    def post_inbox_item(self, command_name, **kwargs) -> str:
        self.calls.append({"command_name": command_name, **kwargs})
        return self.tag


@pytest.fixture
def inbox_backend():
    backend = _FakeInboxBackend()
    set_inbox_backend(backend)
    yield backend
    set_inbox_backend(None)


@pytest.fixture
def cmd():
    instance = _cmd_module.EmailCommand()
    # Pass the run() auth gate (token/credentials present)
    instance._storage = MagicMock()
    instance._storage.get_secret.return_value = "tok"
    instance._unsubscribe_storage = MagicMock()
    return instance


def _wire_service(
    cmd,
    search_results: list[SimpleNamespace],
    fulls: dict[str, SimpleNamespace | None] | None = None,
) -> MagicMock:
    """Wire a fake service: search returns the list, fetch_message maps by id."""
    service = MagicMock()
    service.search.return_value = search_results
    service.fetch_message.side_effect = lambda mid, **kw: (fulls or {}).get(mid)
    cmd._get_service = MagicMock(return_value=service)
    return service


def _sender_block(addr: str, name: str, count: int, full: SimpleNamespace | None):
    """Emails + the full-fetch mapping (headers live on the sender's first message)."""
    emails = [_make_email(addr, name, i) for i in range(1, count + 1)]
    return emails, {emails[0].id: full}


# ── Fast-path + dispatch ─────────────────────────────────────────────────────


class TestUnsubscribeFastPath:
    @pytest.mark.parametrize("phrase", [
        "clean up my subscriptions",
        "clean up my email subscriptions",
        "Clean up my subscriptions.",
    ])
    def test_matches(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "unsubscribe_scan"}

    @pytest.mark.parametrize("phrase", [
        "clean up my inbox",
        "unsubscribe from everything",  # needs the scan to surface choices first
    ])
    def test_no_match_falls_through(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None

    def test_action_in_parameter_enum(self, cmd):
        action_param = next(p for p in cmd.parameters if p.name == "action")
        assert "unsubscribe_scan" in action_param.enum_values


# ── Scan run ─────────────────────────────────────────────────────────────────


class TestRunUnsubscribeScan:
    def test_gmail_query(self, cmd, inbox_backend):
        service = _wire_service(cmd, [])
        cmd.run(_make_req(), action="unsubscribe_scan")
        service.search.assert_called_once_with(GMAIL_QUERY, max_results=50)

    def test_grouping_threshold_and_payload(self, cmd, inbox_backend):
        a_emails, a_fulls = _sender_block(
            "a@example.com", "Sender A", 4,
            _make_full(url="https://a.example/u", one_click=True),
        )
        b_emails, b_fulls = _sender_block(
            "b@example.com", "Sender B", 3,
            _make_full(mailto="unsub@b.example"),
        )
        c_emails, c_fulls = _sender_block(
            "c@example.com", "Sender C", 2,  # below the >=3 threshold
            _make_full(url="https://c.example/u"),
        )
        service = _wire_service(
            cmd, a_emails + b_emails + c_emails, {**a_fulls, **b_fulls, **c_fulls}
        )

        resp = cmd.run(_make_req(user_id=42, is_pre_routed=True), action="unsubscribe_scan")

        # One full fetch per candidate; sub-threshold senders never fetched
        fetched = [call.args[0] for call in service.fetch_message.call_args_list]
        assert fetched == [a_emails[0].id, b_emails[0].id]

        assert resp.success
        assert resp.context_data["message"] == (
            "I found 2 senders you never read. "
            "Check your phone to pick which to unsubscribe."
        )

        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["command_name"] == "email"
        assert call["title"] == "Subscription cleanup — 2 senders"
        assert call["category"] == "interactive_list"
        assert call["create_push_notification"] is True
        assert call["target_type"] == "user"
        assert call["user_id"] == 42
        assert call["body"] == "- Sender A: 4 unread\n- Sender B: 3 unread"

        metadata = call["metadata"]
        assert metadata["type"] == "interactive_list"
        assert metadata["command_name"] == "email"
        assert metadata["empty_text"] == "No stale subscriptions found."
        rows = metadata["sections"][0]["rows"]
        assert [r["key"] for r in rows] == ["a@example.com", "b@example.com"]
        assert [r["label"] for r in rows] == ["Sender A", "Sender B"]
        assert [r["caption"] for r in rows] == [
            "4 unread in the last 90 days", "3 unread in the last 90 days",
        ]
        assert all(r["control"] == "checkbox" for r in rows)
        assert all(r["default"] == {"selected": False} for r in rows)
        assert metadata["actions"] == [
            {"label": "Unsubscribe {n}", "callback": "unsubscribe_selected", "style": "destructive"},
        ]

    def test_storage_records_with_24h_ttl(self, cmd, inbox_backend):
        a_emails, a_fulls = _sender_block(
            "a@example.com", "Sender A", 3,
            _make_full(url="https://a.example/u", mailto="unsub@a.example", one_click=True),
        )
        _wire_service(cmd, a_emails, a_fulls)
        before = datetime.now(timezone.utc)

        cmd.run(_make_req(), action="unsubscribe_scan")

        cmd._unsubscribe_storage.save.assert_called_once()
        args, kwargs = cmd._unsubscribe_storage.save.call_args
        assert args[0] == "a@example.com"
        assert args[1] == {
            "url": "https://a.example/u",
            "mailto": "unsub@a.example",
            "one_click": True,
            "count": 3,
            "name": "Sender A",
        }
        expires_at = kwargs["expires_at"]
        assert timedelta(hours=23, minutes=59) < (expires_at - before) <= timedelta(hours=24, minutes=1)

    def test_drops_sender_without_unsubscribe_data(self, cmd, inbox_backend):
        a_emails, a_fulls = _sender_block(
            "a@example.com", "Sender A", 3, _make_full(url="https://a.example/u"),
        )
        b_emails, b_fulls = _sender_block(
            "b@example.com", "Sender B", 3, _make_full(),  # no unsubscribe data at all
        )
        _wire_service(cmd, a_emails + b_emails, {**a_fulls, **b_fulls})

        cmd.run(_make_req(), action="unsubscribe_scan")

        rows = inbox_backend.calls[0]["metadata"]["sections"][0]["rows"]
        assert [r["key"] for r in rows] == ["a@example.com"]
        saved_keys = [c.args[0] for c in cmd._unsubscribe_storage.save.call_args_list]
        assert saved_keys == ["a@example.com"]

    def test_fetch_failure_skips_candidate(self, cmd, inbox_backend):
        a_emails, a_fulls = _sender_block(
            "a@example.com", "Sender A", 3, None,  # transient fetch failure
        )
        b_emails, b_fulls = _sender_block(
            "b@example.com", "Sender B", 3, _make_full(mailto="unsub@b.example"),
        )
        _wire_service(cmd, a_emails + b_emails, {**a_fulls, **b_fulls})

        cmd.run(_make_req(), action="unsubscribe_scan")

        rows = inbox_backend.calls[0]["metadata"]["sections"][0]["rows"]
        assert [r["key"] for r in rows] == ["b@example.com"]

    def test_candidates_capped_at_15(self, cmd, inbox_backend):
        emails: list[SimpleNamespace] = []
        fulls: dict[str, SimpleNamespace] = {}
        for i in range(20):
            block, block_fulls = _sender_block(
                f"s{i:02d}@example.com", f"Sender {i:02d}", 3,
                _make_full(url=f"https://s{i:02d}.example/u"),
            )
            emails.extend(block)
            fulls.update(block_fulls)
        service = _wire_service(cmd, emails, fulls)

        resp = cmd.run(_make_req(), action="unsubscribe_scan")

        assert service.fetch_message.call_count == 15
        rows = inbox_backend.calls[0]["metadata"]["sections"][0]["rows"]
        assert len(rows) == 15
        assert "I found 15 senders" in resp.context_data["message"]

    def test_zero_candidates_spoken_only(self, cmd, inbox_backend):
        # Two senders, both below the >=3 threshold
        a_emails, a_fulls = _sender_block(
            "a@example.com", "Sender A", 2, _make_full(url="https://a.example/u"),
        )
        b_emails, b_fulls = _sender_block(
            "b@example.com", "Sender B", 1, _make_full(url="https://b.example/u"),
        )
        service = _wire_service(cmd, a_emails + b_emails, {**a_fulls, **b_fulls})

        resp = cmd.run(_make_req(is_pre_routed=True), action="unsubscribe_scan")

        assert resp.success
        assert resp.context_data["message"] == "No stale subscriptions found."
        assert inbox_backend.calls == []
        service.fetch_message.assert_not_called()
        cmd._unsubscribe_storage.save.assert_not_called()

    def test_all_candidates_dropped_spoken_only(self, cmd, inbox_backend):
        a_emails, a_fulls = _sender_block(
            "a@example.com", "Sender A", 3, _make_full(),  # no unsubscribe headers
        )
        _wire_service(cmd, a_emails, a_fulls)

        resp = cmd.run(_make_req(), action="unsubscribe_scan")

        assert resp.success
        assert resp.context_data["message"] == "No stale subscriptions found."
        assert inbox_backend.calls == []

    def test_single_sender_singular_message(self, cmd, inbox_backend):
        a_emails, a_fulls = _sender_block(
            "a@example.com", "Sender A", 3, _make_full(mailto="unsub@a.example"),
        )
        _wire_service(cmd, a_emails, a_fulls)

        resp = cmd.run(_make_req(), action="unsubscribe_scan")
        assert resp.context_data["message"].startswith("I found 1 sender you never read.")

    def test_no_user_targets_household(self, cmd, inbox_backend):
        a_emails, a_fulls = _sender_block(
            "a@example.com", "Sender A", 3, _make_full(mailto="unsub@a.example"),
        )
        _wire_service(cmd, a_emails, a_fulls)

        cmd.run(_make_req(user_id=None), action="unsubscribe_scan")

        call = inbox_backend.calls[0]
        assert call["target_type"] == "household"
        assert call["user_id"] is None

    @pytest.mark.parametrize("tag", ["no_backend", "no_cc_url", "http_error", "invalid"])
    def test_post_failure_maps_to_spoken_error(self, cmd, inbox_backend, tag):
        inbox_backend.tag = tag
        a_emails, a_fulls = _sender_block(
            "a@example.com", "Sender A", 3, _make_full(url="https://a.example/u"),
        )
        _wire_service(cmd, a_emails, a_fulls)

        resp = cmd.run(_make_req(), action="unsubscribe_scan")

        assert not resp.success
        assert resp.context_data["message"]  # spoken even on the pre-route path
        assert resp.error_details


class TestImapScanPath:
    @pytest.fixture
    def imap_provider(self, monkeypatch):
        monkeypatch.setattr(_cmd_module, "get_email_provider", lambda: "imap")

    def test_imap_query_and_aged_filter(self, cmd, inbox_backend, imap_provider):
        # Sender D: 3 unread but one is only 2 days old -> 2 aged -> below threshold
        d_emails = [
            _make_email("d@example.com", "Sender D", 1, days_old=30),
            _make_email("d@example.com", "Sender D", 2, days_old=20),
            _make_email("d@example.com", "Sender D", 3, days_old=2),
        ]
        # Sender E: 3 aged-unread -> candidate
        e_emails = [
            _make_email("e@example.com", "Sender E", i, days_old=10 + i)
            for i in range(1, 4)
        ]
        fulls = {e_emails[0].id: _make_full(mailto="unsub@e.example")}
        service = _wire_service(cmd, d_emails + e_emails, fulls)

        resp = cmd.run(_make_req(), action="unsubscribe_scan")

        service.search.assert_called_once_with(IMAP_QUERY, max_results=50)
        rows = inbox_backend.calls[0]["metadata"]["sections"][0]["rows"]
        assert [r["key"] for r in rows] == ["e@example.com"]
        assert "I found 1 sender" in resp.context_data["message"]
