"""A relay that refuses `PosterChan/Server` is still read: the socket retries without it, and remembers.

Reported 2026-10-27: "concord is missing my messages recently from the Mingle room, i see the messages on desktop but
not on mobile" / "that entire conversation is missing on mobile". wss://jskitty.com/nostr -- the CORD relay carrying
the most Concord streams this node mirrors -- answers HTTP 403 to the User-Agent `PosterChan/Server` and 101 to the
library default, a browser's, or none. Every server-side socket sent it (the Concord mirror, the Concord tunnel the
phone reads through, the relay client), so the node had read nothing from that relay. A real websocket server here
refuses exactly that header the same way.
"""
import asyncio

import pytest
import websockets
from websockets.asyncio.server import serve
from websockets.http11 import Response
from websockets.datastructures import Headers

from app.services.nostr import peer_ua


def _run(coro):
    return asyncio.run(coro)


async def _with_relay(fn):
    seen = []

    def gatekeeper(conn, request):
        ua = request.headers.get("User-Agent", "")
        seen.append(ua)
        if ua == "PosterChan/Server":
            return Response(403, "Forbidden", Headers(), b"no bots")
        return None

    async def echo(ws):
        async for m in ws:
            await ws.send(m)

    async with serve(echo, "127.0.0.1", 0, process_request=gatekeeper) as srv:
        port = srv.sockets[0].getsockname()[1]
        return await fn(f"ws://127.0.0.1:{port}", seen)


@pytest.fixture(autouse=True)
def _fresh():
    peer_ua._refused.clear()
    yield
    peer_ua._refused.clear()


def test_a_refusal_is_retried_without_the_header_and_the_host_is_remembered():
    async def go(url, seen):
        ws = await peer_ua.connect(url, open_timeout=5)            # the relay client's form: awaited
        await ws.send("hi"); got = await ws.recv(); await ws.close()
        async with peer_ua.connect(url, open_timeout=5) as ws2:     # the tunnel's / mirror's form
            await ws2.send("again"); got2 = await ws2.recv()
        return got, got2, list(seen)
    got, got2, seen = _run(_with_relay(go))
    assert (got, got2) == ("hi", "again")
    assert seen[0] == "PosterChan/Server", seen          # it identifies itself first...
    assert "PosterChan/Server" not in seen[1:], seen     # ...and never again to a host that refused it
    assert len(seen) == 3, ("one refusal, one retry, then straight through", seen)


def test_the_concord_tunnel_and_mirror_paths_read_such_a_relay():
    from app.services import concord_tunnel
    from app.services.nostr_relay import concord_mirror

    async def go(url, seen):
        cm = concord_tunnel.connect(url, None)                         # what the phone's tunnel opens
        ws = await cm.__aenter__()
        await ws.send("tunnel"); a = await ws.recv(); await cm.__aexit__(None, None, None)
        peer_ua._refused.clear()
        concord_tunnel._tor_bad.clear()
        async with concord_mirror._TorThenDirect(url, "") as ws:        # what this node's mirror opens (direct hop)
            await ws.send("mirror"); b = await ws.recv()
        return a, b
    assert _run(_with_relay(go)) == ("tunnel", "mirror")


def test_only_a_refusal_drops_the_header():
    """Any other failure is the relay being down -- not a reason to stop saying who we are."""
    async def go():
        with pytest.raises(OSError):
            await peer_ua.connect("ws://127.0.0.1:9", open_timeout=2)
    _run(go())
    assert peer_ua.header("ws://127.0.0.1:9") == "PosterChan/Server"


def test_a_caller_that_names_its_own_user_agent_is_left_alone():
    async def go(url, seen):
        with pytest.raises(websockets.exceptions.InvalidStatus):
            await peer_ua.connect(url, open_timeout=5, user_agent_header="PosterChan/Server")
        return list(seen)
    seen = _run(_with_relay(go))
    assert seen == ["PosterChan/Server"], seen
    assert peer_ua.header("ws://127.0.0.1") == "PosterChan/Server"
