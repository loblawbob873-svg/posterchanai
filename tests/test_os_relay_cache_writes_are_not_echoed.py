"""PosterChanOS: the desktop's own cache writes are stored by the local relay but not fanned back out to it.

store.js writes every event the app receives into the local relay; the same app also subscribes there. Each write
came straight back to the app over its live subscriptions -- measured on the laptop 2026-10-13: the relay's event
loop at ~100% of a core, py-spy ~56% sending frames and ~28% _on_event/fanout. The store's socket says what it is
(`?pc-cache=1`); the relay stores what it sends and fans out only what reaches it any other way.

Runs the SHIPPED `relay_main.py --os-mode`.
Run: venv-unified/bin/python -m pytest tests/test_os_relay_cache_writes_are_not_echoed.py -q
"""
import asyncio
import json
import shutil

import websockets

from tests import os_relay_harness as H


async def _live(url, sid, filt, seen, ready):
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps(["REQ", sid, filt]))
        async for raw in ws:
            m = json.loads(raw)
            if m[0] == "EOSE":
                ready.set()
            elif m[0] == "EVENT" and m[1] == sid:
                seen.append(m[2]["content"])


def test_a_cache_write_is_stored_and_not_echoed_but_any_other_write_is():
    tmp = H.tmpdir()
    relay = H.LocalRelay(tmp)
    try:
        relay.start()

        async def go():
            seen, ready = [], asyncio.Event()
            t = asyncio.create_task(_live(relay.url, "live", {"kinds": [1]}, seen, ready))
            await asyncio.wait_for(ready.wait(), 10)
            sk = bytes([7] * 32)
            async with H.Client(relay.url.rstrip("/") + "/?pc-cache=1") as cache:
                ok, why = await cache.publish(H.ev(sk, 1, "a cached copy"))
                assert ok, why
            async with H.Client(relay.url) as plain:
                ok, why = await plain.publish(H.ev(sk, 1, "a new event"))
                assert ok, why
                await asyncio.sleep(1.0)
                stored = [e["content"] for e in await plain.query({"kinds": [1]})]
            t.cancel()
            return seen, stored

        seen, stored = asyncio.run(go())
    finally:
        relay.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    assert "a new event" in seen, ("an ordinary write was not delivered live", seen)
    assert "a cached copy" not in seen, ("the app's own cache write was echoed back to it", seen)
    assert "a cached copy" in stored and "a new event" in stored, stored
