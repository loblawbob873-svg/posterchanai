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
