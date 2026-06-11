"""Email connection-health tracking (email_shared.connection_health) + agents.

The Proton-Bridge-down-for-a-week incident: agents must never read a
connection failure as "no candidates". Coverage:

- record_failure matrix: 1st/2nd → False, 3rd → True exactly once (and writes
  a 24h-TTL notice record), 4th+ → False, record_success resets so a later
  outage re-alerts, fresh module objects over the same storage don't re-notify
  (the storage IS the cross-agent/restart lock).
- A failed notice POST releases the notice record — the once-per-day notice
  is never burned on an inbox post that the user never saw.
- Both agents swallow EmailConnectionError from search (nothing escapes
  run(), so the scheduler's 3-strike auto-disable never trips), feed the
  shared counter, and post exactly ONE household inbox notice across the two.
- Health is aggregated PER TICK: N broken mailboxes in one tick are ONE
  failure increment, and a healthy mailbox in the same tick must not clear
  a broken mailbox's streak (no masking).
- A successful search resets the counter and notice.
"""

import asyncio
import importlib.util
import itertools
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from jarvis_command_sdk import set_backend, set_inbox_backend
from jarvis_command_sdk.inbox import InboxBackend
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


def _install_email_shared() -> None:
    _stub_log_client()
    if "email_shared" not in sys.modules:
        sys.modules["email_shared"] = types.ModuleType("email_shared")
    _load_real("email_shared.email_message", "email_shared", "email_message.py")
    _load_real("email_shared.senders", "email_shared", "senders.py")
    _load_real("email_shared.user_resolution", "email_shared", "user_resolution.py")
    if "email_shared.email_service_factory" not in sys.modules:
        esf = types.ModuleType("email_shared.email_service_factory")
        esf.create_email_service = lambda: None
        esf.get_email_provider = lambda: "gmail"
        sys.modules["email_shared.email_service_factory"] = esf
    _load_real("email_shared.triage", "email_shared", "triage.py")
    _load_real("email_shared.connection_health", "email_shared", "connection_health.py")
    _load_real("email_shared.reply_rubric", "email_shared", "reply_rubric.py")
    _load_real("email_shared.reply_drafts", "email_shared", "reply_drafts.py")


_install_email_shared()
_health = sys.modules["email_shared.connection_health"]
_alerts_mod = _load_real(
    "email_alerts_agent_for_health", "agents", "email_alerts", "agent.py"
)
_smart_mod = _load_real(
    "smart_reply_agent_for_health", "agents", "smart_reply", "agent.py"
)

EmailConnectionError = sys.modules["email_shared.email_message"].EmailConnectionError

_DESCRIPTION = "Couldn't connect to the Proton Mail Bridge at 127.0.0.1:1143"


class _FakeStorageBackend(StorageBackend):
    """In-memory records (honoring expires_at) + secrets keyed by (key, scope)."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict] = {}
        self.expires: dict[tuple[str, str], datetime | None] = {}
        self.secrets: dict[tuple[str, str], str] = {}

    def save(self, command_name, data_key, data, expires_at=None) -> None:
        self.records[(command_name, data_key)] = data
        self.expires[(command_name, data_key)] = expires_at

    def get(self, command_name, data_key):
        key = (command_name, data_key)
        if key not in self.records:
            return None
        exp = self.expires.get(key)
        if exp is not None and exp <= datetime.now(timezone.utc):
            return None
        return self.records[key]

    def get_all(self, command_name):
        return [v for (c, _), v in self.records.items() if c == command_name]

    def delete(self, command_name, data_key) -> bool:
        self.expires.pop((command_name, data_key), None)
        return self.records.pop((command_name, data_key), None) is not None

    def delete_all(self, command_name) -> int:
        keys = [k for k in self.records if k[0] == command_name]
        for k in keys:
            del self.records[k]
        return len(keys)

    def get_secret(self, key, scope, user_id=None):
        return self.secrets.get((key, scope))

    def set_secret(self, key, value, scope, value_type="string", user_id=None) -> None:
        self.secrets[(key, scope)] = value

    def delete_secret(self, key, scope, user_id=None) -> None:
        self.secrets.pop((key, scope), None)


class _FakeInboxBackend(InboxBackend):
    def __init__(self, tag: str = "ok") -> None:
        self.tag = tag
        self.calls: list[dict] = []

    def post_inbox_item(self, command_name, **kwargs) -> str:
        self.calls.append({"command_name": command_name, **kwargs})
        return self.tag


@pytest.fixture
def storage_backend():
    backend = _FakeStorageBackend()
    # smart_reply only probes when its filter is set; keep the digest quiet.
    backend.secrets[("EMAIL_NOTIFICATION_FILTER", "integration")] = "clients"
    quiet_hour = (datetime.now(timezone.utc).hour + 2) % 24
    backend.secrets[("EMAIL_ALERT_DIGEST_HOUR", "integration")] = str(quiet_hour)
    set_backend(backend)
    yield backend
    set_backend(None)


@pytest.fixture
def inbox_backend():
    backend = _FakeInboxBackend()
    set_inbox_backend(backend)
    yield backend
    set_inbox_backend(None)


@pytest.fixture(autouse=True)
def one_configured_user(monkeypatch):
    monkeypatch.setattr(_alerts_mod, "find_configured_user_ids", lambda: [1])
    monkeypatch.setattr(_smart_mod, "find_configured_user_ids", lambda: [1])


def _run(agent) -> None:
    asyncio.run(agent.run())


def _wire_failing_search(monkeypatch) -> MagicMock:
    service = MagicMock()
    service.search.side_effect = EmailConnectionError(_DESCRIPTION)
    monkeypatch.setattr(_alerts_mod, "create_email_service", lambda: service)
    monkeypatch.setattr(_smart_mod, "create_email_service", lambda: service)
    return service


# ── record_failure / record_success matrix ───────────────────────────────────


class TestRecordFailureMatrix:
    def test_first_two_failures_return_false(self, storage_backend):
        assert _health.record_failure("err") is False
        assert _health.record_failure("err") is False

    def test_third_failure_returns_true_exactly_once(self, storage_backend):
        _health.record_failure("err")
        _health.record_failure("err")
        assert _health.record_failure("err") is True
        # 4th and later: the live notice record suppresses re-alerting.
        assert _health.record_failure("err") is False
        assert _health.record_failure("err") is False

    def test_notice_record_carries_24h_ttl(self, storage_backend):
        before = datetime.now(timezone.utc)
        for _ in range(3):
            _health.record_failure("err")
        after = datetime.now(timezone.utc)

        expires = storage_backend.expires[("email_health", "outage_notice")]
        assert before + timedelta(hours=24) <= expires <= after + timedelta(hours=24)

    def test_expired_notice_rearms_during_a_long_outage(self, storage_backend):
        for _ in range(3):
            _health.record_failure("err")
        # Simulate the 24h TTL lapsing while the outage continues.
        storage_backend.expires[("email_health", "outage_notice")] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        )
        assert _health.record_failure("err") is True  # one notice per day

    def test_success_resets_counter_and_notice(self, storage_backend):
        for _ in range(3):
            _health.record_failure("err")
        _health.record_success()

        assert storage_backend.get("email_health", "consecutive_failures") is None
        assert storage_backend.get("email_health", "outage_notice") is None
        # A later, separate outage re-alerts on its own 3rd failure.
        assert _health.record_failure("err") is False
        assert _health.record_failure("err") is False
        assert _health.record_failure("err") is True

    def test_success_below_threshold_resets_count(self, storage_backend):
        _health.record_failure("err")
        _health.record_failure("err")
        _health.record_success()
        # Counter restarted — two more failures still aren't 3 consecutive.
        assert _health.record_failure("err") is False
        assert _health.record_failure("err") is False
        assert _health.record_failure("err") is True

    def test_fresh_module_same_storage_does_not_renotify(self, storage_backend):
        # Restart simulation: a fresh module (fresh JarvisStorage object) over
        # the same backend sees the persisted notice and stays quiet.
        for _ in range(3):
            _health.record_failure("err")
        fresh = _load_real(
            "email_shared.connection_health", "email_shared", "connection_health.py"
        )
        assert fresh.record_failure("err") is False

    def test_counter_survives_restart(self, storage_backend):
        _health.record_failure("err")
        _health.record_failure("err")
        fresh = _load_real(
            "email_shared.connection_health", "email_shared", "connection_health.py"
        )
        assert fresh.record_failure("err") is True

    def test_no_backend_is_safe(self):
        # Facade no-ops without a backend: every call counts as "first".
        assert _health.record_failure("err") is False
        _health.record_success()  # must not raise


# ── Notice post failure: the once-per-day notice is never burned ─────────────


class TestNoticePostFailure:
    def test_failed_notice_post_releases_lock_and_retries_next_tick(
        self, storage_backend, inbox_backend
    ):
        inbox_backend.tag = "http_error"
        assert _health.report_connection_failure(_DESCRIPTION) is False  # 1st
        assert _health.report_connection_failure(_DESCRIPTION) is False  # 2nd
        assert inbox_backend.calls == []

        # 3rd failure crosses the threshold; the post is attempted but fails —
        # report False AND delete the notice record (it served as the
        # cross-agent lock only during the attempt).
        assert _health.report_connection_failure(_DESCRIPTION) is False
        assert len(inbox_backend.calls) == 1
        assert storage_backend.get("email_health", "outage_notice") is None

        # Next failure tick: the inbox recovers, the record is rewritten and
        # the notice finally posts.
        inbox_backend.tag = "ok"
        assert _health.report_connection_failure(_DESCRIPTION) is True
        assert len(inbox_backend.calls) == 2
        assert storage_backend.get("email_health", "outage_notice") is not None

        # And the live notice suppresses re-posting as usual.
        assert _health.report_connection_failure(_DESCRIPTION) is False
        assert len(inbox_backend.calls) == 2


# ── Agents: one notice across both, nothing escapes run(), recovery ──────────


class TestAgentOutageNotice:
    def test_one_notice_across_both_agents(
        self, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_failing_search(monkeypatch)
        alerts = _alerts_mod.EmailAlertAgent()
        smart = _smart_mod.SmartReplyAgent()

        _run(alerts)  # failure 1
        _run(alerts)  # failure 2
        assert inbox_backend.calls == []

        _run(smart)  # failure 3 — crosses the threshold
        assert len(inbox_backend.calls) == 1
        call = inbox_backend.calls[0]
        assert call["command_name"] == "email"
        assert call["title"] == "Email connection problem"
        assert _DESCRIPTION in call["body"]
        assert "paused" in call["body"]
        assert call["category"] == "general"
        assert call["target_type"] == "household"
        assert call["create_push_notification"] is True

        _run(alerts)  # failure 4 — must NOT re-post
        _run(smart)  # failure 5 — must NOT re-post
        assert len(inbox_backend.calls) == 1

    def test_three_failures_from_one_agent_posts_once(
        self, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_failing_search(monkeypatch)
        alerts = _alerts_mod.EmailAlertAgent()

        for _ in range(4):
            _run(alerts)

        assert len(inbox_backend.calls) == 1
        assert inbox_backend.calls[0]["title"] == "Email connection problem"

    def test_no_exception_escapes_run(self, storage_backend, inbox_backend, monkeypatch):
        # The scheduler's 3-strike auto-disable trips on raised exceptions —
        # these runs must complete cleanly so the agents keep probing.
        _wire_failing_search(monkeypatch)
        _run(_alerts_mod.EmailAlertAgent())  # asyncio.run re-raises any escape
        _run(_smart_mod.SmartReplyAgent())

    def test_alerts_agent_emits_no_alerts_during_outage(
        self, storage_backend, inbox_backend, monkeypatch
    ):
        _wire_failing_search(monkeypatch)
        alerts = _alerts_mod.EmailAlertAgent()
        _run(alerts)
        assert alerts.get_alerts() == []

    def test_successful_search_resets_health(
        self, storage_backend, inbox_backend, monkeypatch
    ):
        service = _wire_failing_search(monkeypatch)
        alerts = _alerts_mod.EmailAlertAgent()

        _run(alerts)
        _run(alerts)

        # Bridge recovers before the third tick — counter must clear.
        service.search.side_effect = None
        service.search.return_value = []
        _run(alerts)
        assert storage_backend.get("email_health", "consecutive_failures") is None

        # Outage returns: it takes 3 fresh consecutive failures to notify.
        service.search.side_effect = EmailConnectionError(_DESCRIPTION)
        _run(alerts)
        _run(alerts)
        assert inbox_backend.calls == []
        _run(alerts)
        assert len(inbox_backend.calls) == 1

    def test_recovery_after_notice_allows_a_later_realert(
        self, storage_backend, inbox_backend, monkeypatch
    ):
        service = _wire_failing_search(monkeypatch)
        alerts = _alerts_mod.EmailAlertAgent()

        for _ in range(3):
            _run(alerts)
        assert len(inbox_backend.calls) == 1

        # Recovery clears the notice; a separate outage re-alerts.
        service.search.side_effect = None
        service.search.return_value = []
        _run(alerts)

        service.search.side_effect = EmailConnectionError(_DESCRIPTION)
        for _ in range(3):
            _run(alerts)
        assert len(inbox_backend.calls) == 2

    def test_smart_reply_search_success_records_success(
        self, storage_backend, inbox_backend, monkeypatch
    ):
        service = _wire_failing_search(monkeypatch)
        smart = _smart_mod.SmartReplyAgent()

        _run(smart)
        _run(smart)
        assert storage_backend.get("email_health", "consecutive_failures") is not None

        service.search.side_effect = None
        service.search.return_value = []
        _run(smart)
        assert storage_backend.get("email_health", "consecutive_failures") is None


# ── Per-TICK aggregation: ticks are the debounce unit, not users ─────────────


def _wire_mixed_services(monkeypatch, module) -> None:
    """create_email_service alternates healthy (uid 1) / broken (uid 2) per tick."""
    healthy = MagicMock()
    healthy.search.return_value = []
    broken = MagicMock()
    broken.search.side_effect = EmailConnectionError(_DESCRIPTION)
    services = itertools.cycle([healthy, broken])
    monkeypatch.setattr(module, "create_email_service", lambda: next(services))


class TestPerTickAggregation:
    def test_all_users_failing_is_one_increment_per_tick(
        self, storage_backend, inbox_backend, monkeypatch
    ):
        # Three broken mailboxes in ONE tick must not burn the whole
        # 3-strike debounce — exactly one increment, no notice.
        monkeypatch.setattr(_alerts_mod, "find_configured_user_ids", lambda: [1, 2, 3])
        _wire_failing_search(monkeypatch)

        _run(_alerts_mod.EmailAlertAgent())

        record = storage_backend.get("email_health", "consecutive_failures")
        assert record["count"] == 1
        assert inbox_backend.calls == []

    def test_all_users_failing_notice_after_three_ticks(
        self, storage_backend, inbox_backend, monkeypatch
    ):
        monkeypatch.setattr(_alerts_mod, "find_configured_user_ids", lambda: [1, 2, 3])
        _wire_failing_search(monkeypatch)
        alerts = _alerts_mod.EmailAlertAgent()

        _run(alerts)
        _run(alerts)
        assert inbox_backend.calls == []

        _run(alerts)
        assert len(inbox_backend.calls) == 1
        assert inbox_backend.calls[0]["title"] == "Email connection problem"

        _run(alerts)  # 4th tick — still exactly one notice
        assert len(inbox_backend.calls) == 1

    def test_healthy_mailbox_does_not_mask_broken_one(
        self, storage_backend, inbox_backend, monkeypatch
    ):
        # uid 1 healthy, uid 2 broken: each tick is a FAILURE — the healthy
        # user must not clear the broken user's streak, so the notice still
        # fires after the threshold ticks.
        monkeypatch.setattr(_alerts_mod, "find_configured_user_ids", lambda: [1, 2])
        _wire_mixed_services(monkeypatch, _alerts_mod)
        alerts = _alerts_mod.EmailAlertAgent()

        _run(alerts)
        record = storage_backend.get("email_health", "consecutive_failures")
        assert record["count"] == 1  # not cleared by the healthy mailbox
        _run(alerts)
        assert inbox_backend.calls == []

        _run(alerts)
        assert len(inbox_backend.calls) == 1
        assert inbox_backend.calls[0]["title"] == "Email connection problem"

    def test_smart_reply_all_users_failing_is_one_increment_per_tick(
        self, storage_backend, inbox_backend, monkeypatch
    ):
        monkeypatch.setattr(_smart_mod, "find_configured_user_ids", lambda: [1, 2, 3])
        _wire_failing_search(monkeypatch)

        _run(_smart_mod.SmartReplyAgent())

        record = storage_backend.get("email_health", "consecutive_failures")
        assert record["count"] == 1
        assert inbox_backend.calls == []

    def test_smart_reply_healthy_mailbox_does_not_mask_broken_one(
        self, storage_backend, inbox_backend, monkeypatch
    ):
        monkeypatch.setattr(_smart_mod, "find_configured_user_ids", lambda: [1, 2])
        _wire_mixed_services(monkeypatch, _smart_mod)
        smart = _smart_mod.SmartReplyAgent()

        _run(smart)
        _run(smart)
        assert inbox_backend.calls == []

        _run(smart)
        assert len(inbox_backend.calls) == 1
        assert inbox_backend.calls[0]["title"] == "Email connection problem"
