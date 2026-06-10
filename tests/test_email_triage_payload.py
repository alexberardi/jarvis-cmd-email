"""Triage payload shape — build_triage_payload / build_triage_body wire format."""

import importlib.util
import os
import sys
import types
from types import SimpleNamespace

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
    return _load_real("email_shared.triage", "triage.py")


_triage = _install_email_shared()


def _email(msg_id: str, sender_name: str, subject: str):
    return SimpleNamespace(id=msg_id, sender_name=sender_name, subject=subject)


class TestBuildTriagePayload:
    def test_full_wire_dict(self):
        emails = [
            _email("id-1", "Alice", "Lunch tomorrow?"),
            _email("id-2", "Bob", "Invoice #42"),
        ]
        metadata, context = _triage.build_triage_payload(emails)

        assert context == {"subjects": {"id-1": "Lunch tomorrow?", "id-2": "Invoice #42"}}
        assert metadata == {
            "type": "interactive_list",
            "version": 1,
            "command_name": "email",
            "empty_text": "Inbox zero — nothing to triage.",
            "context": {"subjects": {"id-1": "Lunch tomorrow?", "id-2": "Invoice #42"}},
            "sections": [
                {
                    "rows": [
                        {
                            "key": "id-1",
                            "label": "Alice",
                            "caption": "Lunch tomorrow?",
                            "control": "checkbox",
                            "default": {"selected": False},
                        },
                        {
                            "key": "id-2",
                            "label": "Bob",
                            "caption": "Invoice #42",
                            "control": "checkbox",
                            "default": {"selected": False},
                        },
                    ],
                },
            ],
            "actions": [
                {"label": "Mark {n} read", "callback": "triage_mark_read", "style": "primary"},
                {"label": "Archive {n}", "callback": "triage_archive", "style": "secondary"},
                {"label": "Star {n}", "callback": "triage_star", "style": "secondary"},
            ],
        }

    def test_context_embedded_in_payload(self):
        metadata, context = _triage.build_triage_payload([_email("a", "A", "S")])
        assert metadata["context"] is context

    def test_long_fields_sliced_to_caps(self):
        emails = [_email("id-1", "S" * 300, "T" * 300)]
        metadata, context = _triage.build_triage_payload(emails)

        row = metadata["sections"][0]["rows"][0]
        assert row["label"] == "S" * 120
        assert row["caption"] == "T" * 200
        # context subjects are capped tighter (80) to bound payload size
        assert context["subjects"]["id-1"] == "T" * 80

    def test_empty_sender_name_falls_back_to_unknown(self):
        metadata, _ = _triage.build_triage_payload([_email("id-1", "  ", "Subj")])
        assert metadata["sections"][0]["rows"][0]["label"] == "Unknown"

    def test_empty_subject_tolerated(self):
        metadata, context = _triage.build_triage_payload(
            [SimpleNamespace(id="id-1", sender_name="Alice", subject=None)]
        )
        assert metadata["sections"][0]["rows"][0]["caption"] == ""
        assert context["subjects"]["id-1"] == ""

    def test_duplicate_row_keys_rejected_by_builder(self):
        emails = [_email("dup", "A", "S1"), _email("dup", "B", "S2")]
        with pytest.raises(ValueError):
            _triage.build_triage_payload(emails)


class TestBuildTriageBody:
    def test_plain_text_listing(self):
        emails = [
            _email("id-1", "Alice", "Lunch tomorrow?"),
            _email("id-2", "Bob", "Invoice #42"),
        ]
        assert _triage.build_triage_body(emails) == (
            "- Alice: Lunch tomorrow?\n- Bob: Invoice #42"
        )

    def test_empty_list(self):
        assert _triage.build_triage_body([]) == ""
