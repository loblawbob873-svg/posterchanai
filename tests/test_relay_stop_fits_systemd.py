"""The relay's stop finishes inside systemd's TimeoutStopSec=10 -- or nothing after the websockets ever runs.

Measured 2026-10-09: EVERY relay stop that day (five) ended "State 'stop-sigterm' timed out. Killing." The stop
waited, unbounded, for each client's closing handshake, and a client that vanished without one (a phone, Tor:
code 1006) costs websockets' own 10 s close timeout. SIGKILL then skipped the PosterChanDB mirror's clean close
(no CLEAN -> a full copy from Postgres at every restart), the store close and the status-file cleanup.

Run against the real websockets server: a client that never answers the close must not hold the stop past the
bound, and the bound must leave room inside systemd's 10 s for the rest of the shutdown.
"""
import asyncio
import socket
import time

import websockets

from app.services.nostr_relay import thread


def test_the_close_wait_leaves_room_inside_systemds_ten_seconds():
    assert 0 < thread._WS_CLOSE_WAIT <= 4, "the mirror drain, store close and status cleanup need the rest"


def test_a_client_that_never_answers_the_close_cannot_hold_the_stop():
    async def main():
        async def handler(conn):
            await asyncio.sleep(3600)
        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        raw = socket.create_connection(("127.0.0.1", port))      # a client that will never answer a close frame
        raw.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
        await asyncio.sleep(0.3)
        t = time.monotonic()
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), thread._WS_CLOSE_WAIT)   # what the relay's stop does
        except Exception:
            pass
        took = time.monotonic() - t
        raw.close()
        return took
    took = asyncio.run(main())
    assert took < thread._WS_CLOSE_WAIT + 1.5, "a silent client held the stop for %.1fs" % took
