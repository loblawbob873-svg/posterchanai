"""A CONCORD BOT JOINS A ROOM FROM ITS INVITE LINK AND ANSWERS WHEN MENTIONED.

Reported twice — "we need our bot framework to work with concord rooms", then "i still do not see
a way to make a concord bot respond when mentioned". The first round shipped a BRIDGE: it could
open a bundle, list channels, read a channel and build a message. Everything except the part an
operator wanted, which is a bot that sits in a room and replies. Half a feature is reported as none.

THIS TEST EXISTS BECAUSE THE FIRST ROUND'S TEST COULD NOT HAVE CAUGHT THE BUG IT SHIPPED.
It went straight to `inspect` with a bundle already in hand, so it never touched `inviteDetails` or
`openInvite` — the two ops a bot uses to JOIN. Those were broken: the vm realm had no `URL`, and
cord-protocol's invite parser is written

    let u; try { u = new URL(t) } catch { return }

so the throw was swallowed, the parse returned undefined, and every real invite link came back
"invalid CORD invite". The bare `naddr1…#secret` form (which never reaches that branch) worked
perfectly, which is why nothing looked wrong. A bot could not have joined a room from a pasted
link at all.

So this drives the REAL path, from the link inwards, against a community minted by the shipped
protocol (`tests/mint_concord_room.mjs`) — including a message that names the bot and one that does
not, because a bot that answers everything in somebody's community gets removed from it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
BOTS = ROOT / "botframework"


@pytest.fixture(scope="module")
def room():
    """A real community, minted once for every test here."""
    if NODE is None:
        pytest.skip("needs node")
    done = subprocess.run([NODE, str(ROOT / "tests/mint_concord_room.mjs")], cwd=ROOT,
                          capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    line = [l for l in done.stdout.splitlines() if l.startswith("ROOM ")][-1]
    return json.loads(line[len("ROOM "):])


def _listener():
    if str(BOTS) not in sys.path:
        sys.path.insert(0, str(BOTS))
    import concordListener
    return concordListener


def test_a_bot_joins_from_the_invite_link_alone(room, tmp_path):
    """THE OP THAT WAS BROKEN. Everything else is downstream of this working."""
    cl = _listener()
    import concord as cc

    served = {
        tuple(sorted(room["bootstrapRelays"])): room["bundleEvents"],
    }

    def query(relays, filters):
        kinds = set(filters[0].get("kinds") or [])
        if 33301 in kinds:
            return room["bundleEvents"]
        if 1059 in kinds:
            return room["controlWraps"]
        return []

    session = cl.open_room(cc.Room(room["invite"], room["botNsec"]), query)
    assert session.relays, "the room named no relays to speak on"
    names = [c["name"] for c in session.channels]
    assert names == [room["channelName"]], names
    assert session.control_pubkeys, (
        "the seed pass returned no control publishers, so the control stream can never be fetched")


def test_a_link_that_lost_its_fragment_says_so_rather_than_failing_later(room):
    """The `#` half IS the room key, and chat apps truncate links. Refusing at the door beats a
    decrypt failure three calls deep, which reads as a broken room."""
    cl = _listener()
    import concord as cc
    with pytest.raises(cc.ConcordError) as e:
        cc.Room(room["invite"].split("#", 1)[0], room["botNsec"])
    assert "#" in str(e.value)


def test_it_answers_a_mention_and_ignores_everything_else(room, tmp_path, monkeypatch):
    """THE WHOLE FEATURE, end to end, with the relay replaced and nothing else.

    Two messages are in the room: one naming the bot by npub, one not. Exactly one reply must go
    out, it must be a CORD gift wrap the shipped reader can decrypt, and it must go to the ROOM's
    relays — not to the local relay every other bot publishes through, whose outbox would never
    deliver it to the room's members.
    """
    cl = _listener()
    import concord as cc

    wraps = [room["mentionWrap"], room["chatterWrap"]]

    def query(relays, filters):
        kinds = set(filters[0].get("kinds") or [])
        if 33301 in kinds:
            return room["bundleEvents"]
        authors = set(filters[0].get("authors") or [])
        if authors & set(room["controlPubkeys"]):
            return room["controlWraps"]
        if authors & set(room["streamPubkeys"]):
            return wraps
        return []

    published = []

    def publish(relays, event):
        published.append((list(relays), event))
        return 1

    asked = []

    def generate(text, msg):
        asked.append(text)
        return "I think so too."

    wire = cl.Wire(query, publish,
                   identity=lambda: (room["botNpub"], room["botPk"], ["botname"]),
                   generate=generate)
    state = {"room": cc.Room(room["invite"], room["botNsec"]),
             "seen": cl._Seen(str(tmp_path / "seen.json")),
             "floor_ms": 0}

    sent = cl.process_mentions(state, wire)

    assert asked == ["hey " + room["botNpub"] + " what do you think?"], (
        "the bot answered the wrong messages (or none): %r" % (asked,))
    assert sent == 1, "expected exactly one reply, got %d" % sent
    assert len(published) == 1
    relays, event = published[0]
    assert relays == room["roomRelays"], (
        "a room's message went somewhere other than the room's own relays (%r) — the local relay's "
        "outbox federates kind-1/profiles/DMs, so a gift wrap handed to it reaches nobody" % relays)
    assert event.get("kind") == 1059, "a Concord message must go out as a CORD gift wrap"

    # And it is READABLE — by the shipped reader, which is what every other member runs.
    back = _read_back(room, event)
    assert "I think so too." in back, (
        "the reply the bot published cannot be decrypted by the shipped reader, so nobody in the "
        "room would see it: %r" % (back,))


def test_the_same_mention_is_never_answered_twice(room, tmp_path):
    """A restart must not re-answer the room. The mark is written BEFORE the reply goes out, so a
    crash mid-reply costs a dropped answer rather than a duplicate on every boot afterwards."""
    cl = _listener()
    import concord as cc

    def query(relays, filters):
        kinds = set(filters[0].get("kinds") or [])
        if 33301 in kinds:
            return room["bundleEvents"]
        authors = set(filters[0].get("authors") or [])
        if authors & set(room["controlPubkeys"]):
            return room["controlWraps"]
        if authors & set(room["streamPubkeys"]):
            return [room["mentionWrap"]]
        return []

    sent_total = []
    seen_file = str(tmp_path / "seen.json")

    def run():
        state = {"room": cc.Room(room["invite"], room["botNsec"]),
                 "seen": cl._Seen(seen_file), "floor_ms": 0}
        wire = cl.Wire(query, lambda r, e: sent_total.append(e),
                       identity=lambda: (room["botNpub"], room["botPk"], []),
                       generate=lambda t, m: "hi")
        return cl.process_mentions(state, wire)

    assert run() == 1
    assert run() == 0, "the bot answered the same message again after a restart"
    assert len(sent_total) == 1


def test_a_cold_start_does_not_answer_the_backlog(room, tmp_path):
    """The floor. A bot restarting into a busy community must not reply to yesterday — a burst of
    late answers is the most visible way to be a nuisance in somebody else's room."""
    cl = _listener()
    import concord as cc
    import time

    def query(relays, filters):
        kinds = set(filters[0].get("kinds") or [])
        if 33301 in kinds:
            return room["bundleEvents"]
        authors = set(filters[0].get("authors") or [])
        if authors & set(room["controlPubkeys"]):
            return room["controlWraps"]
        if authors & set(room["streamPubkeys"]):
            return [room["mentionWrap"]]
        return []

    state = {"room": cc.Room(room["invite"], room["botNsec"]),
             "seen": cl._Seen(str(tmp_path / "seen.json")),
             # The message was minted seconds ago; a floor an hour in the future stands in for a
             # bot whose session began after it.
             "floor_ms": int((time.time() + 3600) * 1000)}
    wire = cl.Wire(query, lambda r, e: pytest.fail("replied to a message from before it started"),
                   identity=lambda: (room["botNpub"], room["botPk"], []),
                   generate=lambda t, m: "hi")
    assert cl.process_mentions(state, wire) == 0


def test_it_can_authenticate_to_a_relay_that_demands_the_plane(room):
    """NIP-42 FOR A CORD RELAY, which Armada-hosted rooms require before they will carry traffic.

    This op had never been executed by anything, and it was wrong in four ways at once: it passed
    `{challenge, relay}` where `createPlaneAuth` wants an author pubkey, handed it the BOT'S signer
    (a relay authenticating the room does not accept a bot's key), returned the signer OBJECT
    instead of an event — a function, which cannot cross a JSON bridge — and built the template in
    the wrong realm, where the signer's own `Array.isArray` check would reject it.

    Found by test_every_bridge_op_is_exercised.py asking "is every door opened?", not by a user.
    """
    cl = _listener()
    import concord as cc

    def query(relays, filters):
        kinds = set(filters[0].get("kinds") or [])
        if 33301 in kinds:
            return room["bundleEvents"]
        return room["controlWraps"]

    session = cl.open_room(cc.Room(room["invite"], room["botNsec"]), query)
    relay = session.relays[0]
    event = session.room.plane_auth("a-challenge-from-the-relay", relay)

    assert event.get("kind") == 22242, f"a NIP-42 auth event is kind 22242, got {event.get('kind')}"
    assert event.get("content") == ""
    tags = {t[0]: t[1] for t in event.get("tags") or []}
    assert tags.get("challenge") == "a-challenge-from-the-relay", tags
    assert tags.get("relay", "").rstrip("/") == relay.rstrip("/"), tags
    assert re.fullmatch(r"[0-9a-f]{128}", event.get("sig") or ""), "the event is not signed"
    assert event.get("pubkey") != room["botPk"], (
        "it signed with the BOT's key; a CORD relay authenticates the ROOM's plane, and a bot key "
        "is not one it accepts")
    assert event["pubkey"] in set(room["controlPubkeys"]) | set(room["streamPubkeys"]), (
        "the signing key is not one this membership holds: %s" % event.get("pubkey"))


def test_a_relay_the_room_does_not_use_is_refused(room):
    """The signer validates what it is asked to sign. A challenge from somewhere else must not come
    back signed with this room's key — that is the room vouching for a relay it never joined."""
    cl = _listener()
    import concord as cc

    def query(relays, filters):
        return room["bundleEvents"] if 33301 in set(filters[0].get("kinds") or []) \
            else room["controlWraps"]

    session = cl.open_room(cc.Room(room["invite"], room["botNsec"]), query)
    with pytest.raises(Exception):
        session.room.plane_auth("challenge", "wss://somewhere-else.example")


def _read_back(room, wrap):
    """Decrypt a wrap with the SHIPPED reader — the same code every other member runs."""
    script = """
      import { makeRealm, loadInto, into } from './botframework/cord_realm.mjs';
      const cord = makeRealm();
      loadInto(cord, 'static/js/client/cord-protocol.js');
      loadInto(cord, 'static/js/client/cord-reader.js');
      const toCord = into(cord);
      const room = JSON.parse(process.env.PC_ROOM);
      const opened = cord.PosterCord.openInvite(room.invite, toCord(room.bundleEvents));
      const out = await cord.PosterCordReader.inspectChat(
        toCord(opened.bundle), toCord(room.controlWraps), room.channelId,
        toCord([JSON.parse(process.env.PC_WRAP)]));
      console.log('TEXTS ' + JSON.stringify(out.messages.map(m => m.text)));
    """
    done = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                          capture_output=True, text=True, timeout=120,
                          env={**os.environ, "PC_ROOM": json.dumps(room),
                               "PC_WRAP": json.dumps(wrap)})
    assert done.returncode == 0, done.stderr[-2000:]
    line = [l for l in done.stdout.splitlines() if l.startswith("TEXTS ")][-1]
    return json.loads(line[len("TEXTS "):])
