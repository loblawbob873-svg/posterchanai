"""A proxied relay connect that times out must not log an InvalidStateError traceback.

Measured on server1 (~720 in three hours): our 8s open_timeout CANCELS the proxy handshake, the transport
is aborted, and websockets 17.1's connection_lost() then parses the (empty) reply and calls set_exception
on the already-cancelled future -- InvalidStateError inside an event-loop callback, which asyncio logs as
a full traceback for every relay connect that falls back from Tor to direct. Drives the library's real
HTTPProxyConnection in exactly that order.
"""
import asyncio

import pytest
from websockets.asyncio.client import HTTPProxyConnection
from websockets.proxy import parse_proxy
from websockets.uri import parse_uri


def _timed_out_then_lost(run_parser):
    async def scenario():
        conn = HTTPProxyConnection(parse_uri("wss://relay.example/"), parse_proxy("http://127.0.0.1:8118"), None)
        conn.response.cancel()              # open_timeout fired: the handshake was cancelled
        conn.reader.feed_eof()              # ...the aborted transport reports the connection lost
        run_parser(conn)
    asyncio.run(scenario())


def test_a_cancelled_handshake_then_connection_lost_raises_nothing():
    import app.services.nostr.relay  # noqa: F401  (installs the guard)
    _timed_out_then_lost(lambda conn: conn.run_parser())


def test_without_the_guard_the_library_raises():
    """Proves the test can fail: the library's own run_parser, unguarded."""
    import app.services.nostr.relay  # noqa: F401
    raw = HTTPProxyConnection.run_parser.__closure__[0].cell_contents
    with pytest.raises(asyncio.InvalidStateError):
        _timed_out_then_lost(raw)
