"""BLOCK FROM THE DESKTOP, END TO END: THE DESKTOP'S DOCUMENT, THE PHONE'S OWN BLOCKED-NUMBERS CODE, AND BACK.

2026-10-22. tests/client/test_texts_block.py drives each half in the simulator with a 'enc:' cipher
and a setBlocked reply typed into the stub -- the same imitation-of-the-other-side gap that hid the
phone->desktop notification bug for months. Here:

1. The desktop (shipped sms.js, real account key) blocks a number: `pcai:smsblocks`, NIP-44 to self.
2. The phone's WebView (shipped sms.js, a handset) receives that exact event; its applyBlocksHere
   calls `Sms.setBlocked`, answered by the SHIPPED `SmsPlugin.setBlocked` -> `Blocklist.set` on a JVM
   (tests/sms_java_phone.py `block`; the platform's blocked-numbers provider is a set), and publishes
   what the list now says.
3. The desktop receives the phone's document live and its Texts list says "Blocked" -- not "waiting
   for your phone".
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
NUM = "+15550104477"

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


def blocks_event(out):
    return next(e for e in out["relayFull"] if e["tags"][0][1] == "pcai:smsblocks")


def round_trip(phone, tmp_path, block=True, available=True, clock_ahead=0, first=None):
    state = str(tmp_path / "phone.json")
    desk = sim(realKey=SEC, isPhone=False, telephony=False, clockAhead=clock_ahead,
               steps=(first or ["load"]) + [("block:" if block else "unblock:") + NUM])
    asked = blocks_event(desk)
    hand = sim(realKey=SEC, isPhone=True, telephony=True, relay=[asked], rawEvents=[asked],
               blockArgv=phone.argv("block", SEC, state, "true" if available else "false"),
               steps=["absorbRaw", "settle", "settle", "settle"])
    answer = blocks_event(hand)
    back = sim(realKey=SEC, isPhone=False, telephony=False, relay=[asked], rawEvents=[answer],
               steps=["load", "deliverRaw"])
    return desk, hand, answer, back, json.load(open(state))


def test_a_block_made_on_the_desktop_reaches_the_phones_list_and_comes_back_blocked(phone, tmp_path):
    desk, hand, answer, back, ph = round_trip(phone, tmp_path)
    assert desk["blockLabels"][NUM] == "Blocked — waiting for your phone", desk["blockLabels"]
    assert ["setBlocked", NUM, True] in hand["calls"], hand["calls"][-5:]
    assert ph["blocked"] == [NUM], "the phone's own blocked-numbers list does not hold the number: %s" % ph
    assert answer["id"] != blocks_event(desk)["id"], "the phone's answer never replaced the request on the relay"
    assert not [x for x in hand["phone"] if "refusedByRelay" in x], hand["phone"]
    assert back["blockLabels"][NUM] == "Blocked", back["blockLabels"]


def test_a_phone_that_cannot_block_says_so_on_the_desktop(phone, tmp_path):
    *_, back, ph = round_trip(phone, tmp_path, available=False)
    assert ph["blocked"] == []
    assert back["blockLabels"][NUM] == "Blocked here — your phone could not apply it", back["blockLabels"]


def test_the_phones_answer_lands_even_when_its_clock_is_behind_the_desktops(phone, tmp_path):
    """The answer replaces the desktop's document; a relay refuses a version older than the one it
    holds, so a phone a few seconds behind left the desktop on "waiting for your phone" for ever."""
    _, hand, _, back, ph = round_trip(phone, tmp_path, clock_ahead=5)
    assert ph["blocked"] == [NUM]
    assert not [x for x in hand["phone"] if "refusedByRelay" in x], hand["phone"]
    assert back["blockLabels"][NUM] == "Blocked", back["blockLabels"]


def test_a_block_after_switching_account_does_not_carry_the_other_accounts_numbers():
    """Every other piece of Texts state is retired on an account switch; the block list was not, so account B's next
    block published account A's blocked numbers into B's list (2026-10-22). The shipped sms.js, switched in place."""
    out = sim(steps=["block:+15550001", "account:" + "b" * 64, "block:+15550002"])
    numbers = sorted(v["number"] for v in out["blocks"].values())     # the list account B's save publishes
    assert numbers == ["+15550002"], ("account A's blocked number is in account B's list", numbers)
