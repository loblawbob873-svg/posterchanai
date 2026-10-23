"""THE PHONE'S ARCHIVE EVENT FOR AN INCOMING TEXT, RUN — NOT READ.

`SmsOutbox.archiveIncoming` is what `SignerRelayService` publishes the moment a text arrives with
PosterChan closed, and it is the only way a desktop hears about that text. Until 2026-10-22 nothing
had ever executed it ("I am not seeing any SMS notifications on OS and windows app ... that should
have had a regression test a long time ago"): test_android_sms_outbox.py checks that the call EXISTS.

This runs the shipped Java (tests/sms_java_event.py) and checks the event against the server's own
independent Nostr implementation: a valid id and BIP-340 signature by the account key, the tags the
desktop's live Texts filter selects on, and NIP-44 v2 content that decrypts with the account's
conversation key to exactly what the desktop reads -- including `received`, the phone's own time of
receipt that the desktop uses to decide the text is new (sms.js notifyNew).
tests/client/test_sms_phone_to_desktop_notification_full_app.py feeds this same event to the
bundled desktop client.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sms_java_event  # noqa: E402

from app.services.nostr import bip340  # noqa: E402
from app.services.nostr.event import verify_event  # noqa: E402
from app.services.nostr.nip44 import decrypt, get_conversation_key  # noqa: E402

SEC = bytes.fromhex("01" * 32)
pytestmark = pytest.mark.skipif(not sms_java_event.available(), reason="JDK + android.jar required")


@pytest.fixture(scope="module")
def build():
    b = sms_java_event.Builder()
    yield b.event
    b.close()


def _open(ev):
    me = bip340.pubkey_from_seckey(SEC)
    return json.loads(decrypt(ev["content"], get_conversation_key(SEC, me)))


def test_it_is_a_valid_event_signed_by_the_account(build):
    ev = build(SEC.hex(), "+15550142", "hello", 1792700000123, 1792700004567)
    assert ev["pubkey"] == bip340.pubkey_from_seckey(SEC).hex()
    assert ev["kind"] == 30078
    assert verify_event(ev), "the relay and every client would drop this event"


def test_it_carries_what_the_desktops_live_filter_selects_on(build):
    ev = build(SEC.hex(), "+15550142", "hello", 1792700000123, 1792700004567)
    tags = {t[0]: t[1] for t in ev["tags"]}
    assert tags["l"] == "pcai-sms"                 # sms.js FILTER(): '#l': [L_TAG]
    assert tags["d"].startswith("pcai:sms:")       # sms.js D_MSG -- only these can notify


def test_the_content_decrypts_to_the_message(build):
    body = 'Running 10 min late / "the place" é \U0001F600'
    ev = build(SEC.hex(), "+15550142", body, 1792700000123, 1792700004567)
    assert _open(ev) == {"address": "+15550142", "body": body, "date": 1792700000123,
                         "incoming": True, "name": "", "received": 1792700004567}


def test_received_is_omitted_when_unknown(build):
    """A queue row from before this field existed has no receipt time; it must not invent one."""
    ev = build(SEC.hex(), "+15550142", "hi", 1792700000123, 0)
    assert "received" not in _open(ev)


def test_the_same_text_is_the_same_document(build):
    """Addressable by its d tag: the native publish and the JS mirror of the same text must collide,
    or the desktop shows it twice (SmsKeys.docId is parity-tested against sms.js docIdFor in
    tests/test_android_sms.py; this pins that archiveIncoming uses it)."""
    a = build(SEC.hex(), "+15550142", "same", 1792700000123, 1)
    b = build(SEC.hex(), "+15550142", "same", 1792700000999, 2)   # same second
    c = build(SEC.hex(), "+15550142", "other", 1792700000123, 1)
    assert a["tags"][0] == b["tags"][0] != c["tags"][0]
