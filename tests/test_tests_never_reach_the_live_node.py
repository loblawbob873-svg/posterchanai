"""No test can open a socket to THIS machine's running app (3051) or relay (3052).

2026-10-16: a deploy-gate shard on server1 held a live DocTable subscription on the PRODUCTION relay -- modules
default to ws://127.0.0.1:3052/relay when nothing set a port, and one test forgot to. conftest refuses both ports.
On server1 this fails without the guard (the live relay accepts); elsewhere nothing listens and it passes anyway.
"""
import socket

import pytest

from app.services import relay_reader


@pytest.mark.parametrize("port", [3051, 3052])
def test_the_live_ports_are_refused(port):
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()


def test_the_default_relay_url_points_nowhere():
    assert relay_reader.relay_port() not in (3051, 3052)
