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
        idle = [ws for key, socks in nostr_store._QUERY_POOL.items() for ws in socks]
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
