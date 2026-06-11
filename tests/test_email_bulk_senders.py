"""Deterministic bulk/automated-sender detection (email_shared.senders).

Two independent signals, either marks a sender bulk:
1. local-part match against the conservative automated-sender list — exact
   after dot/dash/underscore normalization, or a list entry followed by a
   non-letter ("noreply+tag", "alerts-us");
2. any List-Unsubscribe signal on the message (url / mailto / one-click),
   even with a human-looking local part (the marketplace-messages@amazon.com
   field incident).
"""

import importlib.util
import os
import sys
import types
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))


def _load_real(name: str, *parts: str):
    path = os.path.join(_ROOT, *parts)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if "email_shared" not in sys.modules:
    sys.modules["email_shared"] = types.ModuleType("email_shared")
_load_real("email_shared.email_message", "email_shared", "email_message.py")
_senders = _load_real("email_shared.senders", "email_shared", "senders.py")

is_bulk_sender = _senders.is_bulk_sender


def _email(
    sender: str = "Alex Smith <alexsmith@example.com>",
    unsubscribe_url: str = "",
    unsubscribe_mailto: str = "",
    unsubscribe_one_click: bool = False,
    is_promotional: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="id-1",
        sender=sender,
        unsubscribe_url=unsubscribe_url,
        unsubscribe_mailto=unsubscribe_mailto,
        unsubscribe_one_click=unsubscribe_one_click,
        is_promotional=is_promotional,
    )


class TestLocalPartMatching:
    @pytest.mark.parametrize(
        "local",
        [
            "noreply", "no-reply", "donotreply", "do-not-reply",
            "notifications", "notification", "notify",
            "alerts", "alert", "mailer", "mailer-daemon",
            "bounce", "bounces", "newsletter", "news", "marketing",
            "promo", "promotions", "offers", "deals", "updates",
            "digest", "auto-confirm", "autoreply", "auto-reply",
        ],
    )
    def test_listed_local_parts_are_bulk(self, local):
        assert is_bulk_sender(_email(f"Shop <{local}@shop.example.com>")) is True

    @pytest.mark.parametrize(
        "local",
        [
            "noreply+tag",     # list entry + non-letter (plus tag)
            "alerts-us",       # list entry + non-letter (region suffix)
            "no.reply",        # separator-normalized exact match
            "no_reply",        # separator-normalized exact match
            "NoReply",         # case-insensitive
            "noreply2024",     # list entry + digit
            "news.daily",      # list entry + non-letter
        ],
    )
    def test_variant_local_parts_are_bulk(self, local):
        assert is_bulk_sender(_email(f"Shop <{local}@shop.example.com>")) is True

    @pytest.mark.parametrize(
        "local",
        [
            "alexsmith",
            "bob.jones",
            "newsom",      # "news" + letter — NOT a prefix match
            "alberta",     # no list entry is a prefix
            "notifyme",    # "notify" + letter — NOT a prefix match
            "alertson",    # "alerts"/"alert" + letter
        ],
    )
    def test_human_local_parts_are_not_bulk(self, local):
        assert is_bulk_sender(_email(f"Person <{local}@example.com>")) is False

    def test_bare_address_without_display_name(self):
        assert is_bulk_sender(_email("noreply@shop.example.com")) is True
        assert is_bulk_sender(_email("alexsmith@example.com")) is False

    def test_empty_sender_is_not_bulk(self):
        assert is_bulk_sender(_email("")) is False
        assert is_bulk_sender(_email(None)) is False  # type: ignore[arg-type]


class TestUnsubscribeSignals:
    def test_unsubscribe_url_marks_bulk_despite_human_local_part(self):
        # The field incident: marketing mail from a human-looking address.
        email = _email(
            "Amazon <marketplace-messages@amazon.com>",
            unsubscribe_url="https://amazon.com/unsubscribe",
        )
        assert is_bulk_sender(email) is True

    def test_unsubscribe_mailto_marks_bulk(self):
        email = _email(
            "Jane Doe <jane@example.com>",
            unsubscribe_mailto="unsub@example.com",
        )
        assert is_bulk_sender(email) is True

    def test_one_click_marks_bulk(self):
        email = _email(
            "Jane Doe <jane@example.com>",
            unsubscribe_one_click=True,
        )
        assert is_bulk_sender(email) is True

    def test_object_without_unsubscribe_fields_is_safe(self):
        # Shapes that predate the subscription-cleanup fields (or test stubs)
        # must not raise — getattr defaults treat them as no-signal.
        email = SimpleNamespace(id="x", sender="Jane <jane@example.com>")
        assert is_bulk_sender(email) is False
        bulk = SimpleNamespace(id="y", sender="Shop <noreply@shop.com>")
        assert is_bulk_sender(bulk) is True


class TestPromotionalSignal:
    def test_is_promotional_marks_bulk_despite_clean_address_and_headers(self):
        # The actual field incident: marketplace-messages@amazon.com matched
        # neither the local-part list nor any unsubscribe header — Gmail's
        # CATEGORY_UPDATES label (surfaced as is_promotional) is the signal.
        email = _email(
            '"Amazon Marketplace" <marketplace-messages@amazon.com>',
            is_promotional=True,
        )
        assert is_bulk_sender(email) is True

    def test_not_promotional_human_sender_passes(self):
        assert is_bulk_sender(_email(is_promotional=False)) is False

    def test_missing_is_promotional_field_is_safe(self):
        email = SimpleNamespace(
            id="z",
            sender="Jane <jane@example.com>",
            unsubscribe_url="",
            unsubscribe_mailto="",
            unsubscribe_one_click=False,
        )
        assert is_bulk_sender(email) is False
