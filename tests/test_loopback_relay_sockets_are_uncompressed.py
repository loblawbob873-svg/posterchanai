"""Sockets to this machine's own relay do not compress.

The relay offers permessage-deflate for its real clients (phones, browsers), and every app process connecting over
127.0.0.1 -- table streams, point reads, table loads, the AP DM listener -- accepted it, so both ends spent CPU
compressing bytes that never left the host (2026-10-10).
"""
import asyncio
import os
import re
import threading

from app.services.relay_reader import loopback_kw


def test_the_rule():
    assert loopback_kw("ws://127.0.0.1:3052/relay") == {"compression": None}
    assert loopback_kw("ws://localhost:3052/relay") == {"compression": None}
    assert loopback_kw("ws://[::1]:3052/relay") == {"compression": None}
    assert loopback_kw("wss://relay.poster.place") == {}
    assert loopback_kw("wss://127.0.0.1.evil.example/relay") == {}


def test_a_loopback_handshake_negotiates_no_deflate():
    import websockets
    from websockets.extensions.permessage_deflate import ServerPerMessageDeflateFactory
    from websockets.sync.client import connect
    ready, box = threading.Event(), {}

    def run():
        loop = asyncio.new_event_loop()

        async def main():
            async def echo(ws):
                async for m in ws:
                    await ws.send(m)
            srv = await websockets.serve(echo, "127.0.0.1", 0, extensions=[ServerPerMessageDeflateFactory()])
            box["port"] = srv.sockets[0].getsockname()[1]
            box["stop"] = asyncio.Event()
            ready.set()
            await box["stop"].wait()
            srv.close()
        box["loop"] = loop
        loop.run_until_complete(main())
    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(10)
    url = "ws://127.0.0.1:%d/relay" % box["port"]
    try:
        with connect(url) as ws:
            assert ws.protocol.extensions, "the server did not offer deflate (the test proves nothing)"
        with connect(url, **loopback_kw(url)) as ws:
            assert ws.protocol.extensions == []
    finally:
        box["loop"].call_soon_threadsafe(box["stop"].set)
        t.join(5)


def test_every_loopback_connect_turns_compression_off():
    files = ["app/services/doc_table.py", "app/services/doc_table_bulk.py", "app/services/fedi_bridge_identity.py",
             "app/services/activitypub/dm.py", "app/services/nostr_store.py", "app/services/relay_reader.py"]
    for f in files:
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), f),
                   encoding="utf-8").read()
        for m in re.finditer(r"\bconnect\((.*?)\)\s*(as ws|$)", src, re.S | re.M):
            call = m.group(1)
            if "open_timeout" not in call:
                continue
            assert "compression=None" in call or "loopback_kw(" in call, "%s: %s" % (f, call[:120])
