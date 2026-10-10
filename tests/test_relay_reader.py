"""relay_reader: the one synchronous door to this node's relay for code that used to SELECT from Postgres (#161).
"Could not ask" must be an exception, never an empty answer -- an auth decision on an empty answer refuses a
maintainer, and a writer acting on one replaces real data with nothing."""
import json
import socket
import threading

import pytest
from websockets.sync.server import serve

from app.services import relay_reader as rr


def _relay(handler):
    srv = serve(handler, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.socket.getsockname()[1]


def test_events_are_returned_until_eose():
    def h(ws):
        req = json.loads(ws.recv())
        sub = req[1]
        for i in range(3):
            ws.send(json.dumps(["EVENT", sub, {"id": str(i), "kind": 1, "tags": [["d", "x"]]}]))
        ws.send(json.dumps(["EOSE", sub]))
        try:
            ws.recv(timeout=2)
        except Exception:
            pass
    srv, port = _relay(h)
    try:
        evs = rr.query([{"kinds": [1]}], port=port, timeout=5)
        assert [e["id"] for e in evs] == ["0", "1", "2"] and rr.d_tag(evs[0]) == "x"
    finally:
        srv.shutdown()


def test_a_relay_that_cannot_be_reached_is_unavailable_not_empty():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    with pytest.raises(rr.Unavailable):
        rr.query([{"kinds": [1]}], port=port, timeout=2)


def test_a_relay_that_never_finishes_is_unavailable_not_a_partial_answer():
    def h(ws):
        sub = json.loads(ws.recv())[1]
        ws.send(json.dumps(["EVENT", sub, {"id": "a"}]))
        try:
            ws.recv(timeout=5)
        except Exception:
            pass
    srv, port = _relay(h)
    try:
        with pytest.raises(rr.Unavailable):
            rr.query([{"kinds": [1]}], port=port, timeout=1)
    finally:
        srv.shutdown()


def test_a_closed_subscription_is_unavailable():
    def h(ws):
        sub = json.loads(ws.recv())[1]
        ws.send(json.dumps(["CLOSED", sub, "error: too busy"]))
    srv, port = _relay(h)
    try:
        with pytest.raises(rr.Unavailable, match="too busy"):
            rr.query([{"kinds": [1]}], port=port, timeout=3)
    finally:
        srv.shutdown()
