"""Pre-route tests for the email command."""

import importlib.util
import os
import sys
import types

import pytest


def _stub_email_shared() -> None:
    if "email_shared" in sys.modules:
        return
    pkg = types.ModuleType("email_shared")
    sys.modules["email_shared"] = pkg

    em = types.ModuleType("email_shared.email_message")
    em.EmailMessage = type("EmailMessage", (), {})
    em.extract_email = lambda x: x
    sys.modules["email_shared.email_message"] = em

    esf = types.ModuleType("email_shared.email_service_factory")
    esf.create_email_service = lambda: None
    esf.get_email_provider = lambda: "gmail"
    sys.modules["email_shared.email_service_factory"] = esf


def _load_command():
    _stub_email_shared()
    here = os.path.dirname(os.path.abspath(__file__))
    cmd_path = os.path.join(here, "..", "commands", "email", "command.py")
    spec = importlib.util.spec_from_file_location("email_cmd_under_test", cmd_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EmailCommand


@pytest.fixture
def cmd():
    return _load_command()()


class TestPreRouteList:
    @pytest.mark.parametrize("phrase", [
        "check my email",
        "check my emails",
        "check my inbox",
        "check my Gmail",
        "any new emails",
        "any new mail",
        "what's in my inbox",
        "do I have any emails",
        "do I have mail",
        "read my emails",
        "show me my inbox",
        "what emails do I have",
    ])
    def test_list(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "list"}


class TestPreRouteRead:
    @pytest.mark.parametrize("phrase,index", [
        ("read email 3", 3),
        ("read the first email", 1),
        ("read the second email", 2),
        ("read the third email", 3),
        ("open email number 5", 5),
        ("read the last email", -1),
    ])
    def test_read(self, cmd, phrase, index):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "read", "email_index": index}


class TestPreRouteArchive:
    @pytest.mark.parametrize("phrase,index", [
        ("archive email 2", 2),
        ("archive the first email", 1),
        ("move email 2 to archive", 2),
    ])
    def test_archive(self, cmd, phrase, index):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "archive", "email_index": index}


class TestPreRouteTrash:
    @pytest.mark.parametrize("phrase,index", [
        ("delete email 2", 2),
        ("trash the first email", 1),
        ("delete the third email", 3),
        ("remove email 4", 4),
    ])
    def test_trash(self, cmd, phrase, index):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "trash", "email_index": index}


class TestPreRouteStar:
    @pytest.mark.parametrize("phrase,index", [
        ("star email 1", 1),
        ("star the second email", 2),
    ])
    def test_star(self, cmd, phrase, index):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "star", "email_index": index}


class TestPreRouteNoMatch:
    @pytest.mark.parametrize("phrase", [
        # Send / reply / search need LLM extraction — must fall through
        "send an email to john@example.com saying I'll be late",
        "reply to email 1 saying thanks",
        "search my email for receipts",
        "find emails from John",
        # Unrelated
        "tell me a joke",
        "what time is it",
        "",
    ])
    def test_returns_none(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None


class TestFastPathPatterns:
    def test_ids_stable(self, cmd):
        ids = {p.id for p in cmd.fast_path_patterns}
        assert ids == {
            "email.list",
            "email.read",
            "email.archive",
            "email.archive_to",
            "email.trash",
            "email.star",
        }
