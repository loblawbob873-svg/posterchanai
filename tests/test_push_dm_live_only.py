"""A DM notification must fire on ARRIVAL, never on the stored backlog.

Two failure modes sit either side of this, and both are silent:

* Without the EOSE gate, every reconnect replays the whole mailbox and each stored gift wrap becomes
  an OS notification. A phone that drops its connection on the train buzzes once per historical
  message when it comes back.
* With `since_now` instead — the obvious alternative, and what the call subscription uses — real
  messages vanish. NIP-59 backdates a gift wrap's `created_at` by up to two days on purpose, so
  `since=now` discards a message that genuinely just arrived. Nothing errors; the DM simply never
  notifies.

So the gate keys on arrival order, and these tests drive the real `relay.subscribe` against a stub
relay that speaks the protocol, rather than asserting on its internals.
"""
import asyncio
import contextlib
import json

import websockets

from app.services.nostr import relay as relay_mod


async def _serve(handler):
    """Start a stub relay on an ephemeral port; returns (url, server)."""
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return f"ws://127.0.0.1:{port}", server


def _ev(eid, created_at, kind=1059):
    return {"id": eid, "kind": kind, "pubkey": "e" * 64, "created_at": created_at,
            "tags": [["p", "a" * 64]], "content": "", "sig": ""}


async def _run(handler, stop_after, timeout=10):
    """Subscribe with live_only until `stop_after(seen)` is true; return the ids handled."""
    url, server = await _serve(handler)
    seen, stop = [], asyncio.Event()

    async def on_ev(ev):
        seen.append(ev.get("id"))
        if stop_after(seen):
            stop.set()

    task = asyncio.create_task(
        relay_mod.subscribe(url, [{"kinds": [1059]}], on_ev, stop, live_only=True))
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    stop.set()
    task.cancel()
    # Await the cancellation: an un-awaited cancel leaves "Task was destroyed but it is pending" and
    # a leaked socket per test, which is how a suite starts failing only when run in a certain order.
    with contextlib.suppress(asyncio.CancelledError):
        await task
    server.close()
    # Bounded: a stub that misbehaves must fail this test, not hang CI forever.
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(server.wait_closed(), 5)
    return seen


def test_backlog_is_dropped_and_live_events_are_handled():
    async def go():
        async def handler(ws):
            req = json.loads(await ws.recv())
            sub = req[1]
            for i in range(3):                                  # stored backlog
                await ws.send(json.dumps(["EVENT", sub, _ev(f"old{i}", 1)]))
            await ws.send(json.dumps(["EOSE", sub]))
            await ws.send(json.dumps(["EVENT", sub, _ev("live", 2)]))
            await ws.wait_closed()

        seen = await _run(handler, lambda s: "live" in s)
        assert seen == ["live"], f"backlog leaked into notifications: {seen}"
    asyncio.run(go())


def test_a_backdated_gift_wrap_still_notifies():
    async def go():
        """The whole reason this isn't `since_now`: NIP-59 stamps wraps up to two days in the past."""
        async def handler(ws):
            req = json.loads(await ws.recv())
            sub = req[1]
            # A real relay applies the filter itself; assert we did not ask it to exclude old stamps.
            assert "since" not in (req[2] or {}), f"live_only must not send `since`: {req[2]}"
            await ws.send(json.dumps(["EOSE", sub]))
            await ws.send(json.dumps(["EVENT", sub, _ev("backdated", 1)]))   # created_at ~1970
            await ws.wait_closed()

        assert await _run(handler, lambda s: s) == ["backdated"]
    asyncio.run(go())


def test_no_eose_opens_the_gate_rather_than_staying_silent():
    async def go():
        """A relay that never EOSEs must not mean 'no DM notifications, forever, with no error'."""
        orig = relay_mod._EOSE_FALLBACK
        relay_mod._EOSE_FALLBACK = 0.5
        try:
            async def handler(ws):
                req = json.loads(await ws.recv())
                sub = req[1]
                await ws.send(json.dumps(["EVENT", sub, _ev("first", 1)]))   # gated, and no EOSE follows
                await asyncio.sleep(1.0)
                await ws.send(json.dumps(["EVENT", sub, _ev("after", 2)]))
                await ws.wait_closed()

            seen = await _run(handler, lambda s: "after" in s)
            # Stronger than "after in seen": also proves the gated "first" never got through.
            assert seen == ["after"], seen
        finally:
            relay_mod._EOSE_FALLBACK = orig
    asyncio.run(go())


def test_gate_recloses_on_reconnect():
    async def go():
        """A reconnect re-REQs and the relay replays its backlog again, so the gate must close again —
        otherwise one dropped connection turns the entire mailbox into notifications."""
        conns = {"n": 0}

        async def handler(ws):
            conns["n"] += 1
            req = json.loads(await ws.recv())
            sub = req[1]
            if conns["n"] == 1:
                await ws.send(json.dumps(["EOSE", sub]))
                await ws.send(json.dumps(["EVENT", sub, _ev("live1", 2)]))
                await asyncio.sleep(0.2)
                await ws.close()                                  # drop → client reconnects
                return
            await ws.send(json.dumps(["EVENT", sub, _ev("replayed", 1)]))   # backlog again
            await ws.send(json.dumps(["EOSE", sub]))
            await ws.send(json.dumps(["EVENT", sub, _ev("live2", 3)]))
            await ws.wait_closed()

        seen = await _run(handler, lambda s: "live2" in s, timeout=15)
        assert "replayed" not in seen, f"backlog replayed as notifications after reconnect: {seen}"
        assert seen == ["live1", "live2"], seen
    asyncio.run(go())
