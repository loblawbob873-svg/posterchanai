"""End to end, through the REAL relay server on a real socket: with "Allow PosterChan clients only" ON, another
app (Damus-like User-Agent, a public address) can still reach a member who uses only this relay.

"make sure other clients can interact with posterchan users like me, I only use relay.poster.place" / "are you
sure it will work then?" (2026-10-16). The rule's unit tests are not enough: a reply also passes the relay's
write path, and a read its subscription path. This drives both, and the refusals that must stay.
"""
import asyncio
import json
import time

import pytest
import websockets

from app.services.nostr.event import build_event
from app.services.nostr import bip340
from tests.test_concord_mirror import Node

ME_SK = bytes.fromhex("41" * 32)
ME = bip340.pubkey_from_seckey(ME_SK).hex()
STRANGER_SK = bytes.fromhex("42" * 32)
STREAM_SK = bytes.fromhex("43" * 32)
STREAM = bip340.pubkey_from_seckey(STREAM_SK).hex()
DAMUS = {"User-Agent": "Damus/1.6", "X-Real-IP": "93.184.216.34"}
POSTERCHAN = {"User-Agent": "PosterChan/1.0", "X-Real-IP": "93.184.216.35"}


async def talk(url, headers, msgs, until, seconds=8):
    async with websockets.connect(url, additional_headers=headers) as c:
        for m in msgs:
            await c.send(json.dumps(m))
        got, deadline = [], time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                m = json.loads(await asyncio.wait_for(c.recv(), timeout=deadline - time.monotonic()))
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            if m[0] == "AUTH":
                continue
            got.append(m)
            if until(got):
                break
        return got


def test_another_app_reaches_a_member_and_nothing_else():
    async def run():
        node = await Node({ME}, cfg={"posterchan_clients_only": True}).start()
        try:
            mine = build_event(ME_SK, 1, "hello from poster.place", [])
            got = await talk(node.url, POSTERCHAN, [["EVENT", mine]], lambda g: any(m[0] == "OK" for m in g))
            assert ["OK", mine["id"], True] == [m for m in got if m[0] == "OK"][0][:3], got

            reply = build_event(STRANGER_SK, 1, "nice", [["e", mine["id"]], ["p", ME]])
            got = await talk(node.url, DAMUS, [["EVENT", reply]], lambda g: any(m[0] == "OK" for m in g))
            ok = [m for m in got if m[0] == "OK"]
            assert ok and ok[0][2] is True, ("a reply to a member from another app was refused", got)

            got = await talk(node.url, DAMUS, [["REQ", "r", {"authors": [ME], "kinds": [1]}]],
                             lambda g: any(m[0] in ("EOSE", "CLOSED") for m in g))
            assert any(m[0] == "EVENT" and m[2]["id"] == mine["id"] for m in got), ("the member's post is not readable", got)

            wrap = build_event(STREAM_SK, 1059, "ciphertext", [])
            got = await talk(node.url, DAMUS, [["EVENT", wrap]], lambda g: any(m[0] == "OK" for m in g))
            assert [m for m in got if m[0] == "OK"][0][2] is True, got
            got = await talk(node.url, DAMUS, [["REQ", "c", {"kinds": [1059], "authors": [STREAM]}]],
                             lambda g: any(m[0] in ("EOSE", "CLOSED") for m in g))
            assert any(m[0] == "EVENT" and m[2]["id"] == wrap["id"] for m in got), ("a Concord room read failed", got)

            # what must stay refused
            got = await talk(node.url, DAMUS, [["REQ", "g", {"kinds": [1], "limit": 20}]],
                             lambda g: any(m[0] in ("EOSE", "CLOSED", "NOTICE") for m in g), seconds=4)
            assert not any(m[0] == "EVENT" for m in got), ("a stranger read the global feed", got)
            lone = build_event(STRANGER_SK, 1, "spam for nobody here", [])
            got = await talk(node.url, DAMUS, [["EVENT", lone]], lambda g: any(m[0] in ("OK", "NOTICE") for m in g), seconds=4)
            assert not any(m[0] == "OK" and m[2] is True for m in got), ("a stranger used the relay as its own", got)
        finally:
            await node.close()
    asyncio.run(run())


ARMADA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/130", "Origin": "https://armada.buzz",
          "X-Real-IP": "93.184.216.36"}
ARMADA_2 = {**ARMADA, "X-Real-IP": "93.184.216.37"}
LINK_SK = bytes.fromhex("44" * 32)
LINK = bip340.pubkey_from_seckey(LINK_SK).hex()


async def _listen(url, headers, sub, filt, seconds, ready):
    """A second Armada member's open subscription: everything it receives within `seconds`."""
    async with websockets.connect(url, additional_headers=headers) as c:
        await c.send(json.dumps(["REQ", sub, filt]))
        got, deadline = [], time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                m = json.loads(await asyncio.wait_for(c.recv(), timeout=deadline - time.monotonic()))
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            got.append(m)
            if m[0] in ("EOSE", "CLOSED"):
                ready.set()
        return got


def test_an_armada_community_that_lists_this_relay_works_with_clients_only_on():
    """Vyram listed relay.poster.place as a relay of his Armada community (2026-10-24). Its members read and write
    it from ARMADA -- not a PosterChan client, not a member of this instance, a public address. With "PosterChan
    clients only" ON, every part of a CORD community that lives on its relays must still work for them: the room's
    envelopes (1059) both ways, the live typing/voice presence (21059) from one member to another, and the
    community's invite link (33301) for somebody joining. A DM read by recipient stays refused."""
    async def run():
        node = await Node({ME}, cfg={"posterchan_clients_only": True}).start()
        try:
            # 1059: a member posts an envelope; another reads the room by stream key.
            wrap = build_event(STREAM_SK, 1059, "ciphertext", [["p", "ab" * 32]])
            got = await talk(node.url, ARMADA, [["EVENT", wrap]], lambda g: any(m[0] == "OK" for m in g))
            assert [m for m in got if m[0] == "OK"][0][2] is True, ("an Armada member's room envelope was refused", got)
            got = await talk(node.url, ARMADA_2, [["REQ", "c", {"kinds": [1059], "authors": [STREAM]}]],
                             lambda g: any(m[0] in ("EOSE", "CLOSED") for m in g))
            assert any(m[0] == "EVENT" and m[2]["id"] == wrap["id"] for m in got), ("the room is not readable", got)

            # 21059: typing / voice presence reaches the other member LIVE (ephemeral: never stored).
            ready = asyncio.Event()
            listener = asyncio.create_task(_listen(node.url, ARMADA_2, "p",
                                                   {"kinds": [21059], "authors": [STREAM], "since": int(time.time()) - 5},
                                                   4, ready))
            await asyncio.wait_for(ready.wait(), 4)
            presence = build_event(STREAM_SK, 21059, "typing", [["p", "cd" * 32]])
            got = await talk(node.url, ARMADA, [["EVENT", presence]], lambda g: any(m[0] == "OK" for m in g))
            assert [m for m in got if m[0] == "OK"][0][2] is True, ("an Armada member's presence was refused", got)
            heard = await listener
            assert not any(m[0] == "CLOSED" for m in heard), ("subscribing to presence was refused", heard)
            assert any(m[0] == "EVENT" and m[2]["id"] == presence["id"] for m in heard), ("presence never arrived", heard)

            # Both in one filter, as a client may ask.
            got = await talk(node.url, ARMADA_2, [["REQ", "b", {"kinds": [1059, 21059], "authors": [STREAM]}]],
                             lambda g: any(m[0] in ("EOSE", "CLOSED") for m in g))
            assert not any(m[0] == "CLOSED" for m in got), ("a room + presence subscription was refused", got)

            # 33301: the community's invite link, read by somebody opening it.
            link = build_event(LINK_SK, 33301, "sealed-invite", [["d", ""]])
            got = await talk(node.url, ARMADA, [["EVENT", link]], lambda g: any(m[0] == "OK" for m in g))
            assert [m for m in got if m[0] == "OK"][0][2] is True, ("the community's invite link was refused", got)
            got = await talk(node.url, ARMADA_2, [["REQ", "i", {"kinds": [33301], "authors": [LINK], "#d": [""]}]],
                             lambda g: any(m[0] in ("EOSE", "CLOSED") for m in g))
            assert any(m[0] == "EVENT" and m[2]["id"] == link["id"] for m in got), ("the invite link is not readable", got)

            # What stays refused: DMs by recipient, and the relay as a general one.
            got = await talk(node.url, ARMADA_2, [["REQ", "x", {"kinds": [1059], "#p": ["ab" * 32]}]],
                             lambda g: any(m[0] in ("EOSE", "CLOSED") for m in g), seconds=4)
            assert not any(m[0] == "EVENT" for m in got), ("a stranger listed somebody's DMs by recipient", got)
            got = await talk(node.url, ARMADA_2, [["REQ", "g", {"kinds": [1], "limit": 20}]],
                             lambda g: any(m[0] in ("EOSE", "CLOSED", "NOTICE") for m in g), seconds=4)
            assert not any(m[0] == "EVENT" for m in got), ("a stranger read the global feed", got)
        finally:
            await node.close()
    asyncio.run(run())


def test_with_the_web_of_trust_on_a_communitys_envelopes_presence_and_invite_link_are_still_accepted():
    """Production runs the WoT gate. A CORD event's author is a DERIVED key -- a room's stream key, an invite's link
    key -- that no web of trust can contain, so each must be accepted on its own shape: 1059/21059 as an opaque
    envelope, 33301 as ONE invite link per link key (`d` = ""). A note by a stranger stays refused."""
    async def run():
        node = await Node({ME}, cfg={"posterchan_clients_only": True, "wot_enabled": True}).start()
        node.server.gate.is_member = lambda pk: pk == ME     # the fixture's Gate trusts everybody
        try:
            for ev, what in ((build_event(STREAM_SK, 1059, "ciphertext", [["p", "ab" * 32]]), "room envelope"),
                             (build_event(STREAM_SK, 21059, "typing", [["p", "cd" * 32]]), "presence"),
                             (build_event(LINK_SK, 33301, "sealed-invite", [["d", ""]]), "invite link")):
                got = await talk(node.url, ARMADA, [["EVENT", ev]], lambda g: any(m[0] == "OK" for m in g))
                assert [m for m in got if m[0] == "OK"][0][2] is True, ("refused with the WoT on: " + what, got)
            got = await talk(node.url, ARMADA_2, [["REQ", "i", {"kinds": [33301], "authors": [LINK], "#d": [""]}]],
                             lambda g: any(m[0] in ("EOSE", "CLOSED") for m in g))
            assert any(m[0] == "EVENT" and m[2]["kind"] == 33301 for m in got), got
            for ev, what in ((build_event(LINK_SK, 33301, "x", [["d", "other"]]), "a second address per link key"),
                             (build_event(LINK_SK, 33301, "x" * 70000, [["d", ""]]), "an oversized invite"),
                             (build_event(LINK_SK, 1, "hello", []), "a stranger's note")):
                got = await talk(node.url, ARMADA, [["EVENT", ev]], lambda g: any(m[0] == "OK" for m in g))
                assert [m for m in got if m[0] == "OK"][0][2] is False, ("accepted: " + what, got)
        finally:
            await node.close()
    asyncio.run(run())
