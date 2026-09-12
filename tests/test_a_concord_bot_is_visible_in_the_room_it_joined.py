"""JOINING A CONCORD ROOM PUBLISHES NOTHING, AND A MEMBER NOBODY CAN SEE IS A MEMBER NOBODY MENTIONS.

Reported over two days as "why is the posterchan bot still not joining the two concord rooms?".
Measured against the live room before a line was changed: the bot HAD joined. It opened the bundle,
decrypted the control stream and read 384 messages out of the only channel of "Lounge Chat", every
pass, for hours. It was simply not there as far as anybody in the room could tell, and it said
nothing about any of it.

TWO INDEPENDENT SILENCES, and each on its own is enough to make a working listener look dead:

  1. `main.py` calls `logging.basicConfig` nowhere, so the root logger holds no handler and Python
     falls back to `logging.lastResort` — stderr, level WARNING. `logger.warning` therefore reaches
     the journal and `logger.info` does not, and the ONE line that said the bot had joined was an
     info. The listener's whole output for a healthy day was "Starting Concord listener...".

  2. A room's roster is the CORD-02 guestbook (kind-3306 `join` wraps), plus control-plane role
     grants, plus — in this client only — anyone OBSERVED speaking. The shipped `cord-reader.js`
     exports no guestbook writer, so neither the bot nor the web client can publish a join; and
     `open_room` is a pure read. `concord.js:roomParticipants` feeds BOTH the member list and the
     @-mention picker, so a silent bot cannot be seen, cannot be tab-completed, is never mentioned,
     never speaks, and so never enters the one bucket left to it. Measured on the live room: 14
     guestbook events, 12 members, the bot in none of them.

So the bot introduces itself once per community, and this test drives that against a real minted
community and then asks the SHIPPED reader whether the bot is a member — because "we published
something" is not the claim being made.
"""
from __future__ import annotations

import json
import logging
import os
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


def _wire(cl, room, published, *, quiet=False, takes=1):
    def query(relays, filters):
        kinds = set(filters[0].get("kinds") or [])
        if 33301 in kinds:
            return room["bundleEvents"]
        authors = set(filters[0].get("authors") or [])
        if authors & set(room["controlPubkeys"]):
            return room["controlWraps"]
        if authors & set(room["streamPubkeys"]):
            return [] if quiet else [room["mentionWrap"]]
        return []

    def publish(relays, event):
        published.append((list(relays), event))
        return takes

    return cl.Wire(query, publish,
                   identity=lambda: (room["botNpub"], room["botPk"], ["PosterChan AI"]),
                   generate=lambda text, msg: "I think so too.")


def _state(cl, room, tmp_path, monkeypatch):
    import concord as cc
    monkeypatch.setenv("CONCORD_SEEN_FILE", str(tmp_path / "seen.json"))
    monkeypatch.setenv("CONCORD_HELLO_FILE", str(tmp_path / "hello.json"))
    monkeypatch.delenv("CONCORD_ANNOUNCE", raising=False)
    return {"room": cc.Room(room["invite"], room["botNsec"]), "floor_ms": 0}


def _members(room, wraps):
    """WHO THE SHIPPED READER SAYS IS IN THE ROOM, given these decrypted messages as activity.

    `inspectGuestbook` is the exact call `concord.js:roomParticipants` makes, and its `observed`
    argument is the only door a PosterChan client can walk a bot through — so the assertion is made
    against that function and not against a list this test builds itself.
    """
    script = """
      import { makeRealm, loadInto, into } from './botframework/cord_realm.mjs';
      const cord = makeRealm();
      loadInto(cord, 'static/js/client/cord-protocol.js');
      loadInto(cord, 'static/js/client/cord-reader.js');
      const toCord = into(cord);
      const room = JSON.parse(process.env.PC_ROOM);
      const opened = cord.PosterCord.openInvite(room.invite, toCord(room.bundleEvents));
      const chat = await cord.PosterCordReader.inspectChat(
        toCord(opened.bundle), toCord(room.controlWraps), room.channelId,
        toCord(JSON.parse(process.env.PC_WRAPS)));
      const observed = (chat.messages || []).map(m => ({ pubkey: m.pubkey, at: Number(m.at) }));
      const view = cord.PosterCordReader.inspectGuestbook(
        toCord(opened.bundle), toCord(room.controlWraps), toCord([]), toCord(observed));
      console.log('OUT ' + JSON.stringify({ members: view.members,
                                            texts: (chat.messages || []).map(m => m.text) }));
    """
    done = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                          capture_output=True, text=True, timeout=180,
                          env={**os.environ, "PC_ROOM": json.dumps(room),
                               "PC_WRAPS": json.dumps(wraps)})
    assert done.returncode == 0, done.stderr[-2000:]
    line = [l for l in done.stdout.splitlines() if l.startswith("OUT ")][-1]
    return json.loads(line[len("OUT "):])


def test_joining_says_so_where_an_operator_can_actually_read_it(room, tmp_path, monkeypatch, capsys):
    """The join line must be on STDOUT, not `logger.info`.

    Without this the listener's entire output for a working day is "Starting Concord listener...",
    which is exactly what a wedged one prints. The proof that info is invisible is in this test:
    the bot framework installs no handler, so `logging.lastResort` (WARNING) is what records go to.
    """
    cl = _listener()
    # MEASURED IN A CLEAN INTERPRETER, because pytest installs its own root handlers and would
    # therefore agree that logger.info is visible — the fixture would confirm the bug.
    probe = subprocess.run(
        [sys.executable, "-c",
         "import logging, concordListener;"
         "print(len(logging.getLogger().handlers), logging.lastResort.level)"],
        cwd=str(BOTS), capture_output=True, text=True, timeout=120)
    assert probe.returncode == 0, probe.stderr[-2000:]
    handlers, last_resort = probe.stdout.split()
    assert handlers == "0" and last_resort == str(logging.WARNING), (
        "the bot framework now configures logging (%s handlers, lastResort level %s) — if that is "
        "deliberate the join line may go back to logger.info, but until then info() reaches nobody"
        % (handlers, last_resort))

    published = []
    st = _state(cl, room, tmp_path, monkeypatch)
    cl.process_mentions(st, _wire(cl, room, published, quiet=True))

    out = capsys.readouterr().out
    assert "joined" in out and "#" + room["channelName"] in out, (
        "joining a room printed nothing an operator can see: %r" % out)
    assert "message(s) readable" in out, (
        "a pass reports nothing it saw, so a quiet room and a dead listener still look the same")


def test_a_joined_bot_introduces_itself_so_the_room_can_see_and_mention_it(room, tmp_path,
                                                                          monkeypatch):
    """THE ASSERTION IS THE ROSTER, NOT THE PUBLISH.

    An announcement that nobody's client counts as membership would be a new message and the same
    bug. So the wrap the bot published is fed back through the SHIPPED `inspectGuestbook` — the
    call `roomParticipants` makes for both the member list and the @ picker — and the bot must be
    in the answer. It must not be there without the announcement, which is the second half.
    """
    cl = _listener()
    published = []
    st = _state(cl, room, tmp_path, monkeypatch)
    cl.process_mentions(st, _wire(cl, room, published, quiet=True))

    assert len(published) == 1, (
        "a bot that joined a room published %d events — joining is a pure read, so the only way "
        "into anyone's member list is to say something" % len(published))
    relays, wrap = published[0]
    assert relays == room["roomRelays"], "the introduction went somewhere other than the room"
    assert wrap.get("kind") == 1059

    view = _members(room, [wrap])
    assert room["botPk"] in view["members"], (
        "the room's own reader does not count the bot as a member after it introduced itself, so "
        "it stays out of the member list and out of the @ picker: %r" % (view["members"],))
    assert "@PosterChan_AI" in " ".join(view["texts"]), (
        "the introduction does not say how to address the bot — the handle is the whole point, "
        "since a display name with a space in it is not something anybody can type after an @")

    assert room["botPk"] not in _members(room, [])["members"], (
        "the bot is already a member with nothing published, so this test could not fail")


def test_it_introduces_itself_once_and_never_again(room, tmp_path, monkeypatch):
    """Once per community, ever — across restarts, which is when it would be most annoying. The
    marker is in its OWN file because `_Seen` evicts oldest-first and the announcement would be one
    of the first ids dropped out of a busy room's message set."""
    cl = _listener()
    published = []
    for _ in range(3):
        st = _state(cl, room, tmp_path, monkeypatch)      # a fresh state is a restart
        cl.process_mentions(st, _wire(cl, room, published, quiet=True))
    assert len(published) == 1, (
        "the bot re-introduced itself %d times — every restart would announce again, and a deploy "
        "restarts every bot" % len(published))


def test_an_introduction_no_relay_accepted_is_retried_rather_than_marked(room, tmp_path,
                                                                        monkeypatch):
    """The latch-before-the-attempt shape this repo keeps rediscovering. `relay.publish` answers
    with the number of relays that took the event; zero means the room never saw it, so nothing may
    be recorded as done."""
    cl = _listener()
    refused, accepted = [], []
    st = _state(cl, room, tmp_path, monkeypatch)
    cl.process_mentions(st, _wire(cl, room, refused, quiet=True, takes=0))
    assert len(refused) == 1

    st = _state(cl, room, tmp_path, monkeypatch)
    cl.process_mentions(st, _wire(cl, room, accepted, quiet=True, takes=1))
    assert len(accepted) == 1, (
        "an introduction no relay accepted was marked as delivered, so the bot stays invisible for "
        "the life of the room with nothing to say why")


def test_an_operator_can_turn_the_introduction_off(room, tmp_path, monkeypatch):
    """A bot sitting in somebody else's community is a guest. CONCORD_ANNOUNCE=0 keeps it silent,
    at the cost of it being unmentionable — which is the operator's call to make, not ours."""
    cl = _listener()
    published = []
    st = _state(cl, room, tmp_path, monkeypatch)
    monkeypatch.setenv("CONCORD_ANNOUNCE", "0")
    cl.process_mentions(st, _wire(cl, room, published, quiet=True))
    assert published == []


def test_a_reply_no_relay_took_says_so(room, tmp_path, monkeypatch, caplog):
    """`relay.publish`'s return was thrown away, so a room whose relays refuse our writes — an
    auth-required plane the bot holds no key for is the live case — looked exactly like a room the
    bot was answering happily."""
    import logging
    cl = _listener()
    published = []
    st = _state(cl, room, tmp_path, monkeypatch)
    monkeypatch.setenv("CONCORD_ANNOUNCE", "0")
    with caplog.at_level(logging.WARNING):
        cl.process_mentions(st, _wire(cl, room, published, takes=0))
    assert any("accepted by NO relay" in r.message for r in caplog.records), (
        "a reply no relay took was counted as sent: %r" % ([r.message for r in caplog.records],))
