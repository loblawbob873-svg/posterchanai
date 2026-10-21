"""Texts can block a number from ANY device -- web, desktop, PosterChanOS -- and the phone applies it.

2026-10-21: "make sure texts on web/desktop/OS can block people in Texts". Only the phone can write Android's
blocked-numbers list (Blocklist.java: the list the telephony stack enforces, shared with the stock apps), so a
laptop's Block is recorded in the account's encrypted `pcai:smsblocks` document and the phone applies it the next
time it sees the document, recording what the list then says. Every device stops notifying for that number at once.

Runs the SHIPPED sms.js in the stub-phone simulator (tests/client/sms_sim.js).
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tests/client/sms_sim.js"
NUM = "+15550104477"


def sim(**opt):
    r = subprocess.run(["node", str(SIM), json.dumps(opt)], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def blocks_docs(out):
    docs = []
    for ev in out["published"]:
        if ev.get("d") == "pcai:smsblocks":
            docs.append(json.loads(ev["content"][4:]))
    return docs


def blocks_doc_event(numbers):
    return {"kind": 30078, "pubkey": "me", "created_at": 1700000000, "tags": [["d", "pcai:smsblocks"], ["l", "pcai-sms"]],
            "content": "enc:" + json.dumps({"v": 1, "numbers": numbers})}


def test_a_laptop_records_the_block_and_does_not_touch_a_phone_list_it_has_not_got():
    out = sim(isPhone=False, telephony=False, steps=["load", "block:" + NUM])
    docs = blocks_docs(out)
    assert docs, "the block was not written to the account's list"
    entry = next(iter(docs[-1]["numbers"].values()))
    assert entry["number"] == NUM and entry["block"] is True and "applied" not in entry
    assert not [c for c in out["calls"] if c[0] == "setBlocked"], "a device with no phone list must not try to write one"


def test_the_phone_applies_a_block_made_elsewhere_and_says_what_its_list_now_says():
    req = {"0104477": {"number": NUM, "block": True, "at": 1700000000000}}
    out = sim(relay=[blocks_doc_event(req)], steps=["load"])
    assert ["setBlocked", NUM, True] in out["calls"], out["calls"][-5:]
    applied = blocks_docs(out)[-1]["numbers"]["0104477"]["applied"]
    assert applied["state"] == "blocked" and applied["at"] >= 1700000000000


def test_a_block_already_applied_is_not_applied_again():
    req = {"0104477": {"number": NUM, "block": True, "at": 1700000000000, "applied": {"state": "blocked", "at": 1700000000500}}}
    out = sim(relay=[blocks_doc_event(req)], steps=["load"])
    assert not [c for c in out["calls"] if c[0] == "setBlocked"], "an applied request was re-applied on every load"


def test_a_phone_that_cannot_write_the_list_says_so_instead_of_claiming_it_blocked():
    out = sim(blockable=False, steps=["load", "block:" + NUM])
    applied = blocks_docs(out)[-1]["numbers"]["0104477"]["applied"]
    assert applied["state"] == "unknown", "a list that could not be asked must never read as blocked"


def test_unblock_from_the_phone_reaches_its_list():
    out = sim(steps=["load", "block:" + NUM, "unblock:" + NUM])
    assert ["setBlocked", NUM, False] in out["calls"]
    assert blocks_docs(out)[-1]["numbers"]["0104477"]["applied"]["state"] == "unblocked"


def test_a_blocked_number_raises_no_notification_on_any_device():
    other = "+15550109999"
    out = sim(isPhone=False, telephony=False, steps=["load", "block:" + NUM, "liveMsg:" + NUM + ":buy now", "liveMsg:" + other + ":hello"])
    bodies = [n[1] for n in out["notified"]]
    assert not any("buy now" in b for b in bodies), ("a blocked number still notified", out["notified"])
    assert any("hello" in b for b in bodies), ("an ordinary text must still notify", out["notified"])


def test_the_android_plugin_reads_back_the_list():
    java = (ROOT / "mobile/android/app/src/main/java/place/poster/app/sms/SmsPlugin.java").read_text()
    body = java[java.index("public void setBlocked(PluginCall call)"):][:700]
    assert "place.poster.app.phone.Blocklist.set(getContext(), number, block)" in body
    assert "now == null ? org.json.JSONObject.NULL : now" in body, "could-not-ask must reach the page as null"
