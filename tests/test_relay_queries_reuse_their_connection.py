"""Server-side relay reads reuse an authenticated connection instead of opening one per query.

server1, 2026-10-17: every nostr_store read opened a websocket, answered the relay's NIP-42 challenge (a Schnorr
signature for the relay to verify), sent one REQ and closed -- ~4 new loopback connections a second, 242 sockets in
TIME-WAIT; Folder Sync's per-batch record reads alone were over 1,000 per 5 minutes. Runs against a REAL relay.
"""
import asyncio

import pytest

from app.services import nostr_store
from tests.test_doc_table import SK, relay  # noqa: F401  (fixture)


def test_twenty_authenticated_reads_use_one_connection_and_close_their_subscription(relay, monkeypatch):  # noqa: F811
    import websockets
    opened = []
    real = websockets.connect

    def counting(*a, **k):
        opened.append(a[0] if a else k.get("uri"))
        return real(*a, **k)
    monkeypatch.setattr(websockets, "connect", counting)

    async def go():
        assert await nostr_store.put_doc(relay.port, SK, "pcai:t:pooltest:a", {"v": 1})
        got = []
        for _ in range(20):
            docs = await nostr_store.get_docs(relay.port, ["pcai:t:pooltest:a"], seckey=SK, strict=True)
            got.append(docs.get("pcai:t:pooltest:a"))
        # a pooled socket hears nothing more once its query is done: the subscription was CLOSEd
        me = asyncio.get_running_loop()
        idle = [ws for socks in nostr_store._QUERY_POOL.get(me, {}).values() for ws in socks]
        assert idle, "nothing was pooled"
        assert await nostr_store.put_doc(relay.port, SK, "pcai:t:pooltest:a", {"v": 2})
        await asyncio.sleep(0.5)
        leaked = []
        for ws in idle:
            try:
                leaked.append(await asyncio.wait_for(ws.recv(), timeout=0.3))
            except asyncio.TimeoutError:
                pass
        # the pooled socket dies (a relay restart); the next read still answers, on a fresh connection
        for ws in idle:
            await ws.close()
        after = await nostr_store.get_docs(relay.port, ["pcai:t:pooltest:a"], seckey=SK, strict=True)
        return got, leaked, after
    got, leaked, after = asyncio.run(go())
    queries = [u for u in opened if u]
    assert all(g == {"v": 1} for g in got), got
    assert not leaked, ("a pooled connection kept receiving events after its query", leaked)
    assert after.get("pcai:t:pooltest:a") == {"v": 2}, after
    # put_doc pools its own publish connection; reads: one, plus the one fresh reconnect after the forced close
    assert len(queries) <= 4, ("each read opened its own connection", len(queries))


def _socket_open(ws) -> bool:
    sock = getattr(getattr(ws, "transport", None), "_sock", None)
    return sock is not None and sock.fileno() != -1


def test_a_loop_that_ends_does_not_leave_its_connections_open(relay, monkeypatch):  # noqa: F811
    """Most sync helpers read under asyncio.run -- a loop for one call. Deploy 272 parked each such loop's socket in
    the pool and nothing ever closed it: server1's worker held 2,578 idle connections to its relay 35 minutes in."""
    import websockets
    made = []
    real = websockets.connect

    async def recording(*a, **k):
        ws = await real(*a, **k)
        made.append(ws)
        return ws
    monkeypatch.setattr(websockets, "connect", recording)

    async def one_read():
        return await nostr_store.get_docs(relay.port, ["pcai:t:pooltest:b"], seckey=SK, strict=True)
    for _ in range(30):
        asyncio.run(one_read())
    still_open = [ws for ws in made if _socket_open(ws)]
    assert len(made) >= 30, "each throwaway loop needs its own socket"
    assert len(still_open) <= nostr_store._QUERY_POOL_IDLE, (
        "sockets from loops that already ended are still open", len(still_open), len(made))


def test_two_live_loops_keep_their_own_pools(relay, monkeypatch):  # noqa: F811
    """Two long-lived loops taking turns used to throw each other's pool away (unclosed) on every switch."""
    import threading
    import websockets
    made = []
    real = websockets.connect

    async def recording(*a, **k):
        ws = await real(*a, **k)
        made.append(ws)
        return ws
    monkeypatch.setattr(websockets, "connect", recording)
    loops = [asyncio.new_event_loop() for _ in range(2)]
    threads = [threading.Thread(target=lp.run_forever, daemon=True) for lp in loops]
    [t.start() for t in threads]
    try:
        for _ in range(10):
            for lp in loops:
                asyncio.run_coroutine_threadsafe(
                    nostr_store.get_docs(relay.port, ["pcai:t:pooltest:b"], seckey=SK, strict=True), lp
                ).result(10)
        assert len(made) == 2, ("loops taking turns reconnected", len(made))
    finally:
        for lp in loops:
            lp.call_soon_threadsafe(lp.stop)
        [t.join(5) for t in threads]
