"""A TEXT SENT FROM THE DESKTOP, END TO END: THE DESKTOP'S OWN EVENT, PERFORMED BY THE PHONE'S OWN JAVA.

2026-10-22. The phone->desktop SMS notification bug survived for months because each side was only
ever tested against a hand-made imitation of the other side's data. The desktop->phone direction had
the same gap: tests/client/sms_sim.js checked what sms.js publishes using a fake 'enc:' cipher, and
tests/test_android_sms_outbox.py checked SmsOutbox by reading it. Nothing had handed the phone a
request the desktop actually wrote.

Here (no browser, no phone):

1. The SHIPPED static/js/client/sms.js runs under node as a desktop (no radio) with a REAL account key
   (tests/client/sms_sim.js `realKey`: the vendored nostr-tools NIP-44 + signed events) and sends a
   text. It publishes the `pcai:smsout:` request -- NIP-44 to the account's own key -- and draws a
   "sending" bubble.
2. The relay's copy of every Texts document is handed to the SHIPPED `SmsOutbox.perform` on a JVM
   (tests/sms_java_phone.py), as `SignerRelayService.smsOutbox` hands it each event its sockets
   receive -- the same request from TWO relays, then replayed by a third in a later run of the same
   phone. A recorder stands in for the radio.
3. The phone's signed `{done:true}` receipt goes back through the relay to the desktop's LIVE Texts
   subscription, and the desktop's thread is read: one message, sent, not pending, not failed.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import sms_java_phone  # noqa: E402

SIM = ROOT / "tests/client/sms_sim.js"
SEC = "01" * 32
TO = "+15550142"
BODY = 'On my way / "the usual place" é \U0001F600'

pytestmark = pytest.mark.skipif(not sms_java_phone.available(), reason="JDK + android.jar required")


@pytest.fixture(scope="module")
def phone():
    p = sms_java_phone.Phone()
    yield p
    p.close()


def sim(**opt):
    r = subprocess.run(["node", str(SIM), json.dumps(opt)], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-4000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def desktop_sends(phone, tmp_path, **extra):
    state = str(tmp_path / "phone.json")
    out = sim(realKey=SEC, isPhone=False, telephony=False, phoneRelays=2,
              phoneArgv=phone.argv("perform", SEC, state),
              steps=["load", "send:%s:%s" % (TO, BODY), "phone"], **extra)
    return out, state


def test_the_phone_sends_the_desktops_text_once_and_the_desktop_shows_it_sent(phone, tmp_path):
    out, state = desktop_sends(phone, tmp_path)
    assert ["sendResult", True, "queued"] in out["calls"], out["calls"][-6:]
    run = out["phone"][0]
    assert run["handed"] >= 2, "the request never reached the phone"
    # Exactly one radio send, to the right number with the right body, though two relays delivered it.
    assert run["radio"] == [{"kind": "sms", "to": TO, "body": BODY}], run
    assert len(run["published"]) == 1, "one request, one receipt"
    assert not [x for x in out["phone"] if "refusedByRelay" in x], out["phone"]
    # The desktop: one bubble, and it says SENT.
    t = out["threads"][0]
    assert t["bodies"] == [BODY], t
    assert t["pending"] == [False] and t["failed"] == [False], t

    # A third relay replays the original request to the same phone later: nothing goes out again.
    req = next(e for e in out["relayFull"] if e["tags"][0][1].startswith("pcai:smsout:"))
    again = phone.run("perform", SEC, state, [req])
    assert again["radio"] == [] and again["published"] == [], again


def test_a_desktop_clock_ahead_of_the_phone_still_gets_its_receipt(phone, tmp_path):
    """The receipt REPLACES the request (same `d`), and a relay refuses a replacement older than what it
    holds -- refused, it is broadcast to nobody. A desktop whose clock runs a few seconds ahead of the
    phone's (or a phone that answers within the request's own second on a strict relay) left the
    request standing: the desktop said "sending" for ever, and the phone's durable claim forbade ever
    trying again."""
    out, _ = desktop_sends(phone, tmp_path, clockAhead=5)
    assert out["phone"][0]["radio"], "the phone did not perform the request"
    assert not [x for x in out["phone"] if "refusedByRelay" in x], out["phone"]
    t = out["threads"][0]
    assert t["pending"] == [False] and t["failed"] == [False], t


def test_the_phones_own_drain_answers_the_desktop_too_even_with_its_clock_behind(phone, tmp_path):
    """With PosterChan ON SCREEN the native drain stands aside (AppVisible) and the WebView's sms.js
    drainOutbox performs the request. Same request, same receipt rule: the phone's `{done:true}` must
    replace the desktop's request on the relay even though the phone's clock is behind the desktop's."""
    desk = sim(realKey=SEC, isPhone=False, telephony=False, clockAhead=5,
               steps=["load", "send:%s:%s" % (TO, BODY)])
    req = next(e for e in desk["relayFull"] if e["tags"][0][1].startswith("pcai:smsout:"))
    hand = sim(realKey=SEC, isPhone=True, telephony=True, relay=[req], steps=["drain"])
    assert [c for c in hand["calls"] if c[0] == "send"] == [["send", TO, BODY]], hand["calls"]
    assert not [x for x in hand["phone"] if "refusedByRelay" in x], hand["phone"]
    receipt = next(e for e in hand["relayFull"] if e["tags"][0][1] == req["tags"][0][1])
    assert receipt["id"] != req["id"], "the request still stands on the relay: the receipt was refused"
    # And the desktop, hearing that receipt live, shows the text as sent.
    seen = sim(realKey=SEC, isPhone=False, telephony=False, rawEvents=[receipt], steps=["load", "deliverRaw"])
    t = seen["threads"][0]
    assert t["bodies"] == [BODY] and t["pending"] == [False] and t["failed"] == [False], t
