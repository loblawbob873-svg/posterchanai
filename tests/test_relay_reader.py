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


def _authing_relay(accept=True, send_challenge=True):
    """A relay that challenges on connect (as nostr_relay/server.py does) and records whether the REQ
    arrived AFTER the AUTH it accepted -- the order that decides whether a private repo is served."""
    seen = []

    def h(ws):
        challenge = "chal-123"
        if send_challenge:
            ws.send(json.dumps(["AUTH", challenge]))
        authed = False
        for raw in ws:
            msg = json.loads(raw)
            if msg[0] == "AUTH":
                ev = msg[1]
                from app.services.nostr.event import verify_event
                ok = accept and verify_event(ev) and ev["kind"] == 22242 and ["challenge", challenge] in ev["tags"]
                seen.append(("AUTH", ok, [t for t in ev["tags"] if t[0] == "relay"]))
                authed = authed or ok
                ws.send(json.dumps(["OK", ev["id"], ok, "" if ok else "restricted: no"]))
            elif msg[0] == "REQ":
                seen.append(("REQ", authed))
                ws.send(json.dumps(["EVENT", msg[1], {"id": "p", "kind": 30617, "tags": [["d", "x"]]}]))
                ws.send(json.dumps(["EOSE", msg[1]]))
    return h, seen


def test_with_a_key_the_socket_AUTHENTICATES_before_it_asks():
    h, seen = _authing_relay()
    srv, port = _relay(h)
    try:
        evs = rr.query([{"kinds": [30617]}], port=port, timeout=5, auth_seckey=(7).to_bytes(32, "big"))
    finally:
        srv.shutdown()
    assert [e["id"] for e in evs] == ["p"]
    assert seen[0][0] == "AUTH" and seen[0][1] is True
    assert seen[0][2] == [["relay", "ws://127.0.0.1:%d/relay" % port]]
    assert seen[1] == ("REQ", True), seen


def test_a_refused_AUTH_is_unavailable_not_a_partial_answer():
    h, _seen = _authing_relay(accept=False)
    srv, port = _relay(h)
    try:
        with pytest.raises(rr.Unavailable, match="refused AUTH"):
            rr.query([{"kinds": [30617]}], port=port, timeout=5, auth_seckey=(7).to_bytes(32, "big"))
    finally:
        srv.shutdown()


def test_a_relay_that_never_challenges_is_unavailable_when_auth_was_asked_for():
    h, _seen = _authing_relay(send_challenge=False)
    srv, port = _relay(h)
    try:
        with pytest.raises(rr.Unavailable):
            rr.query([{"kinds": [30617]}], port=port, timeout=1, auth_seckey=(7).to_bytes(32, "big"))
    finally:
        srv.shutdown()


def test_without_a_key_nothing_changes_and_the_challenge_is_ignored():
    h, seen = _authing_relay()
    srv, port = _relay(h)
    try:
        evs = rr.query([{"kinds": [30617]}], port=port, timeout=5)
    finally:
        srv.shutdown()
    assert [e["id"] for e in evs] == ["p"] and seen == [("REQ", False)]
