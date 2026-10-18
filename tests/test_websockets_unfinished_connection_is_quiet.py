"""A websocket whose TLS handshake failed is cleaned up quietly.

2026-10-18: ~130 tracebacks an hour in the relay's log -- websockets 17 calls connection_lost() on a connection whose
connection_made() never ran (a TLS handshake through the Tor proxy failed), and `self.recv_messages` does not exist.
"""
import asyncio

from app.services.nostr import relay  # noqa: F401  (applies the shim)
from websockets.asyncio.client import ClientConnection
from websockets.client import ClientProtocol
from websockets.uri import parse_uri


def test_connection_lost_before_connection_made_does_not_raise():
    async def go():
        conn = ClientConnection(ClientProtocol(parse_uri("wss://relay.example/")))
        assert not hasattr(conn, "recv_messages")          # connection_made never ran
        conn.connection_lost(OSError("TLS handshake failed"))
        return conn.connection_lost_waiter.done()
    assert asyncio.run(go()), "the connection's close was never settled"
