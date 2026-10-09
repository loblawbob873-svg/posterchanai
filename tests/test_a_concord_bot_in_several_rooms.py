"""ONE BOT, SEVERAL CONCORD ROOMS.

"it does not look like our bots support multiple concord rooms" — right: `concord_invite` held one
link, so PosterChan sat in Anime through one bot and in a second room through ANOTHER bot sharing
her key, and lost the second room when that link was swapped. Now the field holds one invite per
line, every room gets its own session, and one room that cannot be read costs only itself.

Two real communities minted by the shipped protocol for the SAME bot key, each on its own relay, so
every query and every reply can be checked against the room it belongs to.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
BOTS = ROOT / "botframework"


@pytest.fixture(autouse=True)
def _quiet(tmp_path, monkeypatch):
    monkeypatch.setenv("CONCORD_HELLO_FILE", str(tmp_path / "hello.json"))
    monkeypatch.setenv("CONCORD_ANNOUNCE", "0")
    monkeypatch.setenv("CONCORD_SEEN_FILE", str(tmp_path / "seen.json"))


@pytest.fixture(scope="module")
def rooms():
    if NODE is None:
        pytest.skip("needs node")
    sk = os.urandom(32).hex()
    out = []
    for relay in ("wss://anime.example", "wss://posterchan.example"):
        done = subprocess.run([NODE, str(ROOT / "tests/mint_concord_room.mjs")], cwd=ROOT,
                              capture_output=True, text=True, timeout=180,
                              env={**os.environ, "PC_BOT_SK": sk, "PC_ROOM_RELAY": relay})
        assert done.returncode == 0, done.stderr[-2000:]
        line = [l for l in done.stdout.splitlines() if l.startswith("ROOM ")][-1]
        out.append(json.loads(line[len("ROOM "):]))
    assert out[0]["botPk"] == out[1]["botPk"], "the two rooms must be for ONE bot"
    return out


def _modules():
    if str(BOTS) not in sys.path:
        sys.path.insert(0, str(BOTS))
    import concord
    import concordListener
    return concord, concordListener


def _wire(cl, rooms, published, broken=()):
    def owner(relays):
        for r in rooms:
            if set(relays) & set(r["roomRelays"] + r["bootstrapRelays"]):
                return r
        raise AssertionError("a query went to a relay no room uses: %r" % (relays,))

    def query(relays, filters):
        r = owner(relays)
        if r["roomRelays"][0] in broken:
            raise OSError("relay unreachable")
        kinds = set(filters[0].get("kinds") or [])
        if 33301 in kinds:
            return r["bundleEvents"]
        authors = set(filters[0].get("authors") or [])
        if authors & set(r["controlPubkeys"]):
            return r["controlWraps"]
        if authors & set(r["streamPubkeys"]):
            return [r["mentionWrap"], r["chatterWrap"]]
        return []

    def publish(relays, event):
        published.append(list(relays))
        return 1

    r0 = rooms[0]
    return cl.Wire(query, publish, identity=lambda: (r0["botNpub"], r0["botPk"], ["posterchan"]),
                   generate=lambda text, msg: "hi!")


def test_every_line_of_the_field_is_a_room(rooms, monkeypatch):
    cc, _ = _modules()
    env = {"CONCORD_INVITE": rooms[0]["invite"] + "\n\n" + rooms[1]["invite"] + "\n" + rooms[0]["invite"],
           "NOSTR_NSEC": rooms[0]["botNsec"]}
    assert cc.invites_from_env(env) == [rooms[0]["invite"], rooms[1]["invite"]]
    assert len(cc.rooms_from_env(env)) == 2
    assert cc.from_env({"CONCORD_INVITE": rooms[0]["invite"], "NOSTR_NSEC": rooms[0]["botNsec"]}) is not None


def test_one_bot_answers_a_mention_in_each_room_on_that_rooms_relays(rooms, monkeypatch):
    cc, cl = _modules()
    monkeypatch.setenv("CONCORD_INVITE", rooms[0]["invite"] + "\n" + rooms[1]["invite"])
    monkeypatch.setenv("NOSTR_NSEC", rooms[0]["botNsec"])
    published = []
    state = {}
    # A fresh session refuses the backlog it just decrypted; these mentions ARE the backlog here.
    monkeypatch.setattr(cl, "COLD_START_SECONDS", 10 ** 9)
    sent = cl.process_mentions(state, _wire(cl, rooms, published))
    assert sent == 2, f"answered {sent} mention(s) across two rooms"
    assert sorted(p[0] for p in published) == sorted(r["roomRelays"][0] for r in rooms), published


def test_a_room_that_cannot_be_read_does_not_silence_the_others(rooms, monkeypatch):
    cc, cl = _modules()
    monkeypatch.setenv("CONCORD_INVITE", rooms[0]["invite"] + "\n" + rooms[1]["invite"])
    monkeypatch.setenv("NOSTR_NSEC", rooms[0]["botNsec"])
    monkeypatch.setattr(cl, "COLD_START_SECONDS", 10 ** 9)
    published = []
    sent = cl.process_mentions({}, _wire(cl, rooms, published, broken=(rooms[0]["roomRelays"][0],)))
    assert sent == 1 and published == [rooms[1]["roomRelays"]], published


def test_the_admin_form_takes_one_invite_per_line():
    html = (ROOT / "templates/admin/tabs/bots.html").read_text()
    i = html.index('id="bot_f_concord_invite"')
    tag = html[html.rindex("<", 0, i):html.index(">", i)]
    assert tag.startswith("<textarea"), "the invite field is a one-line input again: " + tag
    from app.routers import bots
    cfg = bots._vet_config({"concord_invite": "https://x.example/c/naddr1a#k1\n\nhttps://x.example/c/naddr1b#k2, https://x.example/c/naddr1a#k1"})
    assert cfg["concord_invite"] == "https://x.example/c/naddr1a#k1\nhttps://x.example/c/naddr1b#k2"
    with pytest.raises(Exception):
        bots._vet_config({"concord_invite": "https://x.example/c/naddr1a#k1\nhttps://x.example/c/naddr1b"})
