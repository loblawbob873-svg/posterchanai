"""THE ANDROID PUSH CHAIN, END TO END, WITH EVERY HOP'S REAL PRODUCER (2026-10-22).

    server event → nostr_push_service / mail_notify_service → push_service.send → the Direct queue →
    /api/push/direct/ws (starlette's own send_json) → DirectPushService.onMessage → PushEventService
    .deliver → NotificationManager.notify(tag, id) → the tap's intent → LaunchView → phoneshell.js →
    the screen.

WHY THIS FILE EXISTS. An SMS phone→desktop notification bug survived for months because both of its
tests fed the other side a HAND-MADE imitation of its data. The Android push chain had the same
shape: tests/test_direct_push_server.py and tests/test_dm_notification_is_one_and_lands_on_messages.py
assert on payloads they built, tests/test_android_push_delivery_runtime.py renders strings somebody
typed, and nothing chained them. Here nothing is typed: the triggering events are signed and
PUBLISHED to the shipped relay, the server's own watchers turn them into frames, and the exact text
the socket would carry is handed to the shipped Java (tests/push_chain_server.py,
tests/push_chain_android.py), whose recorded notification and tap route are handed to the shipped
phoneshell.js (tests/client/push_chain_landing_runtime.mjs). The Web Push payload of the same events
goes through the shipped sw.js (tests/client/push_chain_sw_runtime.mjs).

What it found, each run red before its fix:
  * a CALL notification tapped on Android opened Notifications, not Calls (the PWA's sw.js has always
    mapped type 'call' → calls; the payload named no view, and PushEventService falls back to
    "notifications");
  * a MAIL notification likewise opened Notifications (sw.js: type 'mail' → mail);
  * a REACTION (and a repost, and a zap) opened a thread on the reaction EVENT itself — a card that
    reads "🔥" — while the client's own notification for the SAME event, under the SAME tag, opens the
    post that was reacted to (`_notifCtxId`), so which screen a tap reached depended on which of the
    two cards happened to be drawn last;
  * the phone-local type filter never matched mail or reminders: the payload types are `mail` and
    `reminder`, the toggles `email` and `reminders`, so "Email" off on the phone did nothing whenever
    the server row was the stale one it exists to back up;
  * the SELF-COPY of a DM you sent from another device stamped your 10-second DM cooldown before the
    "you sent this" check dropped it, so the reply that came back inside those ten seconds — the
    likeliest moment in a conversation — never reached your phone at all.

What still needs a real phone: that Android honours (tag, id) replacement and the full-screen intent
the way the recording NotificationManager here assumes, the channel sounds, and a tap through a real
MainActivity (LauncherTileLandsOnItsScreenDeviceTest covers that carrier for tiles).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import push_chain_android as PA

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(not (PA.available() and shutil.which("node")),
                                reason="javac/java/node not installed")


# ---- the server, run once for the whole module ---------------------------------------------------

@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    """Every triggering event through the REAL server; returns what reached each transport."""
    from tests.push_chain_server import Server, BOB, BOB_SK
    mp = pytest.MonkeyPatch()
    s = Server(tmp_path_factory.mktemp("push-chain"), mp)
    frames, web, ev = {}, {}, {}

    async def step(label, fn):
        await fn()
        frames[label] = await s.drain()
        web[label] = s.take_webpush()

    async def go():
        await s.register()
        ev["bobs_note"] = await s.publish(s.note(1, "my post", [], sk=BOB_SK))
        note = ev["bobs_note"]["id"]

        async def mention():
            ev["mention"] = await s.publish(s.note(1, "hey nostr:bob", [["p", BOB]]))
            await s.nps._poll()

        async def reply():
            ev["reply"] = await s.publish(s.note(1, "nice post", [["e", note, "", "root"], ["p", BOB]]))
            await s.nps._poll()

        async def reaction():
            ev["reaction"] = await s.publish(s.note(7, "🔥", [["e", note], ["p", BOB]]))
            await s.nps._poll()

        async def repost():
            ev["repost"] = await s.publish(s.note(6, json.dumps(ev["bobs_note"]), [["e", note], ["p", BOB]]))
            await s.nps._poll()

        async def dm():
            s.nps._dm_recent.clear()
            ev["dm"] = await s.publish(s.dm_to_bob("hi bob"))
            assert await s.wait_queued(), "the DM watcher queued nothing for a real gift wrap"

        async def own_then_reply():
            # Bob writes to Alice FROM HIS DESKTOP: the desktop tells the node (POST /api/push/sent)
            # and publishes the self-copy; Alice answers a second later.
            s.nps._dm_recent.clear()
            own = s.bobs_self_copy("hi alice")
            ev["own"] = own
            await s.note_sent([own["id"]])
            await s.publish(own)
            await asyncio.sleep(1.0)
            ev["answer"] = await s.publish(s.dm_to_bob("hi again"))
            await s.wait_queued(timeout=8.0)

        async def call():
            # Ringing is OPT-IN: a device that never turned "Ring for incoming calls" on is not rung.
            s.nps._call_recent.clear()
            await s.publish(s.note(25050, "<nip44>", [["p", BOB], ["t", "invite"]]))
            assert not await s.wait_queued(timeout=2.0), "a call rang a device that never turned ringing on"
            await s.set_prefs({"calls": True})
            await s.set_prefs({"calls": True}, device=False)   # the Web Push row turned ringing on too
            s.nps._call_recent.clear()
            ev["call"] = await s.publish(s.note(25050, "<nip44>", [["p", BOB], ["t", "invite"]]))
            assert await s.wait_queued(), "the call watcher queued nothing for a real invite with ringing on"

        async def mail():
            await s.mail()

        async def prefs_on_the_server():
            # The phone mirrors "likes off, email off" through the real /api/push/prefs.
            await s.set_prefs({"likes": False, "email": False, "dm": True})
            ev["reaction2"] = await s.publish(s.note(7, "👍", [["e", note], ["p", BOB]]))
            await s.mail(title="📧 Second email")
            ev["mention2"] = await s.publish(s.note(1, "again nostr:bob", [["p", BOB]]))
            await s.nps._poll()

        async def dm_off_on_the_server():
            await s.set_prefs({"dm": False})
            s.nps._dm_recent.clear()
            ev["dm_off"] = await s.publish(s.dm_to_bob("you will not hear this"))
            await asyncio.sleep(1.5)
            ev["mention3"] = await s.publish(s.note(1, "third nostr:bob", [["p", BOB]]))
            await s.nps._poll()
            await s.set_prefs({})

        for label, fn in (("mention", mention), ("reply", reply), ("reaction", reaction),
                          ("repost", repost), ("dm", dm), ("own_then_reply", own_then_reply),
                          ("call", call), ("mail", mail), ("prefs", prefs_on_the_server),
                          ("dm_off", dm_off_on_the_server)):
            await step(label, fn)

    try:
        s.run(go(), timeout=240)
    finally:
        s.close()
        mp.undo()
    return {"frames": frames, "web": web, "ev": ev}


@pytest.fixture(scope="module")
def phone():
    p = PA.Phone()
    yield p
    p.close()


def _one(chain, label):
    got = chain["frames"][label]
    assert len(got) == 1, f"{label}: {len(got)} frames on the wire, expected exactly one: {got}"
    return got[0]


def _shade(phone, label, frame, *, prefs=None, client_dm_ago=None, sent_wid=None):
    return phone.run([(label, prefs, client_dm_ago, sent_wid, frame)])[label]


def _landing(routes):
    env = dict(os.environ, PC_ROUTES=json.dumps(sorted(set(routes))))
    r = subprocess.run(["node", str(ROOT / "tests/client/push_chain_landing_runtime.mjs")],
                       capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _client_route(ev):
    """Where the CLIENT'S OWN notification for this event goes: the shipped `_notifCtxId`."""
    src = (ROOT / "static/js/client/notifview.js").read_text()
    a = src.index("  function _notifCtxId(e){")
    b = src.index("\n  }\n", a) + 4
    js = ("const replyParentId=()=>{throw new Error('not for this kind')};" + src[a:b]
          + "const e=JSON.parse(process.argv[1]);const t=_notifCtxId(e)||e.id;"
            "console.log(t?'post:'+t:'notifications');")
    r = subprocess.run(["node", "-e", js, json.dumps(ev)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


# ---- one card per event, the right tag, the right words, and where the tap lands -----------------

def test_a_mention_is_one_card_that_opens_the_post(chain, phone):
    frame = _one(chain, "mention")
    eid = chain["ev"]["mention"]["id"]
    out = _shade(phone, "mention", frame)
    assert len(out["cards"]) == 1, out
    card = out["cards"][0]
    assert card["tag"] == "nostr-" + eid          # the tag the client's notifPing uses for this event
    assert card["text"] == "Alice mentioned you"
    assert card["landed"] == "post:" + eid
    assert out["acks"] == [str(json.loads(frame)["id"])], "a drawn card must be acknowledged"
    assert _landing([card["landed"]])[card["landed"]]["cold"] == [["openThread", eid]]


def test_a_reply_is_one_card_that_opens_the_conversation(chain, phone):
    frame = _one(chain, "reply")
    rid = chain["ev"]["reply"]["id"]
    out = _shade(phone, "reply", frame)
    assert [c["tag"] for c in out["cards"]] == ["nostr-" + rid]
    landed = out["cards"][0]["landed"]
    assert landed.startswith("post:")
    assert _landing([landed])[landed]["warm"][0][0] == "openThread"


@pytest.mark.parametrize("label", ["reaction", "repost"])
def test_a_reaction_or_repost_opens_the_post_it_is_about(chain, phone, label):
    """The push and the client's own notification share the tag `nostr-<event id>`, so either may be
    the card left in the shade — and both must open the SAME thing: the post that was reacted to or
    reposted, never a thread rooted on the reaction event."""
    frame = _one(chain, label)
    ev = chain["ev"][label]
    out = _shade(phone, label, frame)
    assert len(out["cards"]) == 1, out
    card = out["cards"][0]
    assert card["tag"] == "nostr-" + ev["id"], "the push no longer shares the client's tag for this event"
    want = _client_route(ev)
    assert want == "post:" + chain["ev"]["bobs_note"]["id"]
    assert card["landed"] == want, (
        f"the {label} push opens {card['landed']}, the client's own card for the same event opens {want}")
    assert _landing([want])[want]["cold"] == [["openThread", chain["ev"]["bobs_note"]["id"]]]


def test_a_dm_is_one_card_that_opens_messages(chain, phone):
    frame = _one(chain, "dm")
    payload = json.loads(frame)["payload"]
    assert payload["wid"] == chain["ev"]["dm"]["id"]
    out = _shade(phone, "dm", frame)
    assert len(out["cards"]) == 1, out
    card = out["cards"][0]
    assert card["tag"] == "pc-dm"                  # the client's own decrypted DM card REPLACES it
    assert card["text"] == "Someone sent you a message"
    assert card["landed"] == "messages"
    assert _landing(["messages"])["messages"] == {"cold": [["switchView", "messages"]],
                                                  "warm": [["switchView", "messages"]]}


def test_a_dm_the_client_already_announced_is_not_drawn_twice_but_is_acknowledged(chain, phone):
    """ClientNotified: a live client drew "Alice sent you a DM" 5s ago → the blind push is dropped.
    Acknowledged anyway, or the durable queue replays it for ever."""
    frame = _one(chain, "dm")
    inside = _shade(phone, "dm-after-client", frame, client_dm_ago=5_000)
    assert inside["cards"] == [] and inside["acks"] == [str(json.loads(frame)["id"])]
    # ...and it FAILS OPEN: a client record older than the window speaks for nothing.
    stale = _shade(phone, "dm-stale-client", frame, client_dm_ago=120_000)
    assert len(stale["cards"]) == 1


def test_a_dm_this_phone_sent_itself_does_not_notify_it(chain, phone):
    frame = _one(chain, "dm")
    wid = json.loads(frame)["payload"]["wid"]
    out = _shade(phone, "dm-own", frame, sent_wid=wid)
    assert out["cards"] == [] and len(out["acks"]) == 1


def test_a_replayed_frame_is_acknowledged_and_never_drawn_twice(chain, phone):
    frame = _one(chain, "mention")
    out = phone.run([("first", None, None, None, frame), ("again", "=", None, None, frame)])
    assert len(out["first"]["cards"]) == 1
    assert not any(c["posted_now"] for c in out["again"]["cards"]), "a replay was drawn again"
    assert out["again"]["acks"] == out["first"]["acks"]


def test_a_dm_you_sent_from_another_device_reaches_no_device_and_hides_no_reply(chain, phone):
    """Bob's desktop sends; the self-copy must buzz neither the phone nor the browser. Alice's
    answer one second later MUST — it is the most likely moment in a conversation for a reply."""
    got = chain["frames"]["own_then_reply"]
    wids = [json.loads(f)["payload"].get("wid") for f in got]
    assert chain["ev"]["own"]["id"] not in wids, "the phone was pushed about a message Bob sent"
    web_wids = [json.loads(w).get("wid") for w in chain["web"]["own_then_reply"]]
    assert chain["ev"]["own"]["id"] not in web_wids, "the browser was pushed about a message Bob sent"
    assert wids == [chain["ev"]["answer"]["id"]], (
        "Alice's reply never reached the phone: the self-copy of Bob's own message spent the DM "
        f"cooldown before it was recognised as his. On the wire: {wids}")
    out = _shade(phone, "answer", got[0])
    assert [c["landed"] for c in out["cards"]] == ["messages"]


def test_a_call_rings_full_screen_and_a_tap_opens_calls(chain, phone):
    frame = _one(chain, "call")
    out = _shade(phone, "call", frame)
    assert len(out["cards"]) == 1, out
    card = out["cards"][0]
    assert card["channel"] == "pcai_calls" and card["fullScreen"] and card["category"] == "call"
    assert card["text"] == "Alice is calling…"
    assert card["landed"] == "calls", (
        f"tapping an incoming-call notification opens {card['landed']!r}; the PWA opens Calls")
    assert _landing(["calls"])["calls"]["cold"] == [["switchView", "calls"]]


def test_a_mail_notification_opens_mail(chain, phone):
    frame = _one(chain, "mail")
    out = _shade(phone, "mail", frame)
    assert [c["title"] for c in out["cards"]] == ["📧 New email"]
    assert out["cards"][0]["landed"] == "mail", (
        f"tapping a mail notification opens {out['cards'][0]['landed']!r}; the PWA opens Mail")
    assert _landing(["mail"])["mail"]["cold"] == [["switchView", "mail"]]


# ---- the person's toggles, end to end ------------------------------------------------------------

def test_the_server_row_filters_per_device(chain):
    """`likes`/`email` off on the PHONE (mirrored through /api/push/prefs with its device id): the
    phone gets only the mention; the browser, which said nothing, still gets all three."""
    phone_frames = [json.loads(f)["payload"] for f in chain["frames"]["prefs"]]
    assert [p.get("eid") for p in phone_frames] == [chain["ev"]["mention2"]["id"]], phone_frames
    web_types = sorted(json.loads(w)["type"] for w in chain["web"]["prefs"])
    assert web_types == ["likes", "mail", "mentions"], web_types


def test_dms_off_on_the_server_silences_the_phone_only(chain):
    phone_frames = [json.loads(f)["payload"] for f in chain["frames"]["dm_off"]]
    assert [p.get("type") for p in phone_frames] == ["mentions"], phone_frames
    assert "dm" in [json.loads(w)["type"] for w in chain["web"]["dm_off"]]


@pytest.mark.parametrize("label,toggle", [("reaction", "likes"), ("mail", "email")])
def test_the_phones_own_copy_filters_when_the_server_row_did_not(chain, phone, label, toggle):
    """The backstop: the server row is NULL (the mirror failed — the documented failure), the phone's
    DirectPushStore holds `<toggle>: false`. The frame arrives and must not be drawn — but acked."""
    frame = _one(chain, label)
    prefs = json.dumps({toggle: False})
    out = _shade(phone, label + "-local-off", frame, prefs=prefs)
    assert out["cards"] == [], f"{toggle} is off on this phone and the {label} push was drawn anyway"
    assert len(out["acks"]) == 1
    # ...and nothing else is silenced by it.
    assert len(_shade(phone, "mention-local", _one(chain, "mention"), prefs=prefs)["cards"]) == 1


def test_a_call_rings_whatever_is_switched_off(chain, phone):
    every = json.dumps({k: False for k in ("dm", "likes", "mentions", "replies", "email", "reminders")})
    out = _shade(phone, "call-all-off", _one(chain, "call"), prefs=every)
    assert len(out["cards"]) == 1


def test_the_phone_filter_knows_every_type_the_server_sends():
    """A payload `type` the phone-local filter cannot map to a toggle is never filtered by it. Read
    the types off the shipped senders and the toggle names off push_prefs — not typed here."""
    from app.services.push_prefs import PUSH_TYPES
    sent = set()
    for f in ("app/services/nostr_push_service.py", "app/services/mail_notify_service.py",
              "app/services/reminder_service.py"):
        sent |= set(re.findall(r'"type":\s*"([a-z]+)"', (ROOT / f).read_text()))
    assert {"dm", "call", "mail", "reminder", "channels"} <= sent, sent
    store = (PA.APP / "push/DirectPushStore.java").read_text()
    for t in sorted(sent - {"call"}):
        if t in PUSH_TYPES:
            continue
        assert f'"{t}"' in store, f"payload type {t!r} has no toggle mapping in DirectPushStore.allowsType"


# ---- the same events through the shipped service worker -----------------------------------------

def test_web_push_routes_every_event_like_the_phone(chain, phone):
    labels = ["mention", "reaction", "repost", "dm", "call", "mail"]
    payloads = []
    for label in labels:
        got = chain["web"][label]
        assert len(got) == 1, f"{label}: {len(got)} Web Push payloads"
        payloads.append([label, got[0]])
    env = dict(os.environ, PC_PUSH_PAYLOADS=json.dumps(payloads))
    r = subprocess.run(["node", str(ROOT / "tests/client/push_chain_sw_runtime.mjs")],
                       capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    sw = {d["label"]: d for d in (json.loads(x) for x in r.stdout.splitlines() if x.strip())}
    for label in labels:
        card = _shade(phone, label, _one(chain, label))["cards"][0]
        [shown] = sw[label]["shown"]
        [opened] = sw[label]["opened"]
        # One identity across both transports: the same tag. A card with no identity of its own
        # (Android's default "msg"/"call" keys) has none to share; a web call is keyed per caller.
        if card["tag"] not in ("msg", "call"):
            assert shown["tag"] == card["tag"], (label, shown, card)
        assert shown["title"] == card["title"] and shown["body"] == card["text"]
        route = card["landed"]
        want = ("?event=" + route[5:]) if route.startswith("post:") else ("?view=" + route)
        assert opened == "/client" + want, f"{label}: the PWA opens {opened}, the phone opens {route}"
